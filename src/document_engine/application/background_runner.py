from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from document_engine.adapters.database.models import JournalEvent
from document_engine.adapters.database.models import MigrationBatch as MigrationBatchModel
from document_engine.adapters.database.models import MigrationItem as MigrationItemModel
from document_engine.adapters.filesystem.temp_storage import TempFileStorage
from document_engine.application.migration_service import Builder
from document_engine.application.naming_service import NamingAssistantService
from document_engine.domain.enums import MigrationItemState
from document_engine.domain.naming_rules import NamingRulesEngine
from document_engine.ports.ai_naming_provider import AINamingProviderPort
from document_engine.ports.destination_repository import DestinationRepositoryPort
from document_engine.ports.source_repository import SourceRepositoryPort
from document_engine.worker.lease_manager import claim_next_item

logger = logging.getLogger(__name__)

_active_runs: set[str] = set()
_active_rename_runs: set[str] = set()

_TERMINAL_STATES = (
    MigrationItemState.COMPLETED.value,
    MigrationItemState.FAILED.value,
    MigrationItemState.BLOCKED.value,
    MigrationItemState.CANCELLED.value,
    MigrationItemState.SKIPPED.value,
)

# Límite de tiempo externo por elemento, como ÚLTIMO recurso: garantiza
# que el lote nunca quede colgado para siempre, sin importar dónde ocurra
# el bloqueo (ni el timeout de httplib2 ni `socket.setdefaulttimeout()`
# cortan un socket SSL colgado; solo abandonar el hilo desde afuera lo
# hace). Se fija generoso a propósito: la causa real de los cuelgues era el
# reuso de conexiones TCP medio-muertas, ya resuelto en
# `GoogleDriveRepository._reset_connection`; con eso, este backstop casi
# nunca debería dispararse. Un valor amplio evita, además, matar por error
# una descarga/subida legítima pero lenta de un archivo de decenas de GB.
# Configurable vía `WORKER_ITEM_TIMEOUT_SECONDS`; este es solo el default
# si no se pasa explícitamente.
_DEFAULT_ITEM_HARD_TIMEOUT_SECONDS = 14400


def is_running(batch_id: str) -> bool:
    return batch_id in _active_runs


def is_rename_running(batch_id: str) -> bool:
    return batch_id in _active_rename_runs


def _mark_item_crashed(
    item_id: str,
    batch_id: str,
    exc: Exception,
    *,
    session_factory: Callable[[], Session],
    max_item_retries: int,
    retry_base_seconds: int,
) -> None:
    """Libera el lease y deja constancia de un fallo que `Builder.process_item`
    NO tradujo a `TransientError`/`PermanentError` (p. ej. un corte de la
    conexión a la base de datos a mitad de un `commit`).

    Sin esto, el elemento queda con el estado que tenía al reclamarse
    (`READY`/`RETRY_PENDING`, porque `process_item` no llegó a comitear
    nada) pero con el lease largo del `claim` original todavía vigente —
    invisible para `claim_next_item` durante horas, sin ningún rastro en la
    BD (el `JournalEvent` que armó `process_item` se perdió junto con la
    sesión rota) y sin nada visible salvo el `logger.exception` de arriba.

    Usa una sesión NUEVA a propósito: la que traía `Builder` puede estar en
    el estado roto que causó la excepción."""
    try:
        db = session_factory()
    except Exception:  # noqa: BLE001 - no podemos ni abrir sesión: solo registrar
        logger.exception(
            "No se pudo abrir sesión de recuperación para %s en el lote %s", item_id, batch_id
        )
        return
    try:
        item = db.get(MigrationItemModel, item_id)
        if item is None or item.state in _TERMINAL_STATES:
            return
        item.last_error_code = "UNEXPECTED_ERROR"
        item.last_error_message = str(exc)[:2000]
        item.attempt_count += 1

        # Mismo límite de reintentos que `Builder._fail`: sin él, un fallo
        # que se repite siempre (p. ej. un bug real) reintentaría para
        # siempre sin nunca llegar a FAILED.
        target_state = MigrationItemState.RETRY_PENDING
        if item.attempt_count >= max_item_retries:
            target_state = MigrationItemState.FAILED

        # No se usa `transition()` aquí: el estado persistido puede ser
        # cualquiera de los previos a READY/RETRY_PENDING (la excepción
        # pudo ocurrir antes de que `process_item` avanzara nada), y este
        # es un camino de recuperación excepcional, no un paso normal del
        # flujo.
        item.state = target_state.value
        item.lease_owner = None
        if target_state == MigrationItemState.RETRY_PENDING:
            backoff_seconds = min(retry_base_seconds * (2**item.attempt_count), 900)
            item.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)
        else:
            item.lease_expires_at = None

        db.add(
            JournalEvent(
                batch_id=batch_id,
                migration_item_id=item.id,
                event_type="ITEM_FAILED",
                previous_state=None,
                new_state=item.state,
                operation="TRANSFER",
                result="ERROR",
                attempt_number=item.attempt_count,
                error_code=item.last_error_code,
                final_name=item.planned_destination_name,
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001 - la recuperación misma nunca debe tumbar al worker
        logger.exception("Fallo registrando el crash de %s en el lote %s", item_id, batch_id)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()


def _process_item_with_hard_timeout(
    item_id: str,
    batch_id: str,
    *,
    session_factory: Callable[[], Session],
    source_factory: Callable[[], SourceRepositoryPort],
    destination_factory: Callable[[], DestinationRepositoryPort],
    temp_storage: TempFileStorage,
    max_item_retries: int,
    retry_base_seconds: int,
    item_timeout_seconds: int,
) -> None:
    """Procesa UN elemento en su propio sub-hilo con conexiones frescas
    (Drive + FTP + sesión de BD propias) y le impone un límite de tiempo
    externo con `join(timeout=...)`.

    Se comprobó en vivo (con `sys._current_frames()`) que un socket colgado
    puede ignorar todo timeout de librería; abandonar el hilo desde afuera
    es la única forma confiable de que un elemento no bloquee su slot para
    siempre. Al usar conexiones frescas por elemento, abandonar un hilo
    colgado no daña al worker: el siguiente elemento arranca limpio."""
    item_db = session_factory()
    source = source_factory()
    destination = destination_factory()
    builder = Builder(
        item_db,
        source,
        destination,
        temp_storage,
        max_item_retries=max_item_retries,
        retry_base_seconds=retry_base_seconds,
    )

    def _process() -> None:
        try:
            builder.process_item(item_id)
        except Exception as exc:  # noqa: BLE001 - no debe tumbar el pool de fondo
            logger.exception("Error procesando %s en el lote %s", item_id, batch_id)
            _mark_item_crashed(
                item_id,
                batch_id,
                exc,
                session_factory=session_factory,
                max_item_retries=max_item_retries,
                retry_base_seconds=retry_base_seconds,
            )

    thread = threading.Thread(target=_process, daemon=True)
    thread.start()
    thread.join(timeout=item_timeout_seconds)
    if thread.is_alive():
        logger.error(
            "Elemento %s superó el timeout duro de %ss sin responder; se abandona ese hilo "
            "(sigue vivo en segundo plano) y el worker continúa con el resto",
            item_id,
            item_timeout_seconds,
        )
        # El hilo huérfano conserva sus propias conexiones/sesión; no las
        # cerramos aquí para no cortárselas si sigue vivo. Su lease vence solo.
        return
    _close_quietly(item_db, destination)


def _close_quietly(db: Session, destination: DestinationRepositoryPort) -> None:
    disconnect = getattr(destination, "disconnect", None)
    if callable(disconnect):
        try:
            disconnect()
        except Exception:  # noqa: BLE001
            pass
    try:
        db.close()
    except Exception:  # noqa: BLE001
        pass


def run_batch_in_background(
    batch_id: str,
    *,
    session_factory: Callable[[], Session],
    source_factory: Callable[[], SourceRepositoryPort],
    destination_factory: Callable[[], DestinationRepositoryPort],
    temp_storage: TempFileStorage,
    max_item_retries: int = 5,
    retry_base_seconds: int = 2,
    worker_concurrency: int = 3,
    item_timeout_seconds: int = _DEFAULT_ITEM_HARD_TIMEOUT_SECONDS,
) -> None:
    """Corre como `BackgroundTask` de FastAPI: procesa el lote con hasta
    `worker_concurrency` elementos en paralelo, sin depender de que el
    request HTTP original siga abierto. Vive en el proceso del servidor, así
    que sobrevive a que se cierre la pestaña o el frontend entero — solo se
    detiene al terminar, o si el lote se pausa/cancela desde otro request
    (chequeo cooperativo en cada vuelta), o si el propio servidor se apaga.

    Cada worker reclama elementos de una cola compartida (los `lease` de la
    BD ya evitan que dos workers tomen el mismo). El `claim` va bajo un lock
    de proceso porque SQLite no soporta bien `SELECT ... FOR UPDATE`: sin él,
    dos workers podrían leer el mismo "siguiente" antes de que el primero
    escriba su lease. El lease se toma largo (= timeout duro) para que un
    elemento legítimamente lento — p. ej. subir varios GB — no expire y sea
    reclamado por otro worker en paralelo (lo que provocaría subir el mismo
    archivo dos veces).

    `session_factory`/`source_factory`/`destination_factory` se reciben por
    parámetro para que los tests puedan aislar su BD y usar fakes, y para
    que cada worker construya sus PROPIAS conexiones (una conexión ftplib o
    el pool de httplib2 no son seguros de compartir entre hilos)."""
    if batch_id in _active_runs:
        return
    _active_runs.add(batch_id)
    claim_lock = threading.Lock()

    def worker_loop(index: int) -> None:
        claim_db = session_factory()
        worker_owner = f"bg-{uuid.uuid4().hex[:6]}-{index}"
        try:
            while True:
                batch = claim_db.get(MigrationBatchModel, batch_id)
                if batch is None or batch.status != "RUNNING":
                    break
                with claim_lock:
                    item = claim_next_item(
                        claim_db,
                        batch_id,
                        worker_owner=worker_owner,
                        lease_seconds=item_timeout_seconds,
                    )
                if item is None:
                    break
                _process_item_with_hard_timeout(
                    item.id,
                    batch_id,
                    session_factory=session_factory,
                    source_factory=source_factory,
                    destination_factory=destination_factory,
                    temp_storage=temp_storage,
                    max_item_retries=max_item_retries,
                    retry_base_seconds=retry_base_seconds,
                    item_timeout_seconds=item_timeout_seconds,
                )
        except Exception:  # noqa: BLE001 - un worker no debe tumbar a los demás
            logger.exception("Worker %s del lote %s terminó con error", index, batch_id)
        finally:
            claim_db.close()

    control_db = session_factory()
    try:
        n_workers = max(1, worker_concurrency)
        workers = [
            threading.Thread(target=worker_loop, args=(i,), daemon=True, name=f"migrate-{batch_id[:8]}-{i}")
            for i in range(n_workers)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        batch = control_db.get(MigrationBatchModel, batch_id)
        if batch is not None and batch.status == "RUNNING":
            # No solo "¿hay algo para reclamar?": si algo quedó atascado en
            # un estado intermedio no terminal (p. ej. un elemento abandonado
            # por timeout duro, cuyo lease aún no vence), tampoco se marca
            # como completado — mejor reportar "incompleto" que mentir.
            not_done = (
                control_db.execute(
                    select(MigrationItemModel.id)
                    .where(MigrationItemModel.batch_id == batch_id)
                    .where(MigrationItemModel.state.notin_(_TERMINAL_STATES))
                )
                .scalars()
                .first()
            )
            batch.status = "PLANNED" if not_done is not None else "COMPLETED"
            control_db.commit()
    finally:
        _active_runs.discard(batch_id)
        control_db.close()


def run_ai_rename_in_background(
    batch_id: str,
    *,
    session_factory: Callable[[], Session],
    ai_provider: AINamingProviderPort,
    naming_engine: NamingRulesEngine,
    ai_model_name: str,
) -> None:
    """Igual que `run_batch_in_background`, pero para el renombrado asistido
    por IA: corre en el proceso del servidor y sobrevive a que se cierre la
    pestaña o el frontend entero. Antes esto se hacía con un bucle en el
    cliente (un POST por elemento) que perdía todo el progreso pendiente si
    el navegador se cerraba a mitad de camino.

    Toma una foto fija de los elementos pendientes al arrancar en vez de
    volver a consultar en cada vuelta: un elemento que `resolve_item` deja
    en WAITING_REVIEW (porque la IA necesita revisión humana) seguiría
    apareciendo en una consulta en vivo, causando un bucle infinito que lo
    reprocesa una y otra vez."""
    if batch_id in _active_rename_runs:
        return
    _active_rename_runs.add(batch_id)
    db = session_factory()
    try:
        pending_ids = (
            db.execute(
                select(MigrationItemModel.id)
                .where(MigrationItemModel.batch_id == batch_id)
                .where(MigrationItemModel.state == MigrationItemState.WAITING_REVIEW.value)
            )
            .scalars()
            .all()
        )
        service = NamingAssistantService(db, ai_provider, naming_engine, ai_model_name=ai_model_name)
        for item_id in pending_ids:
            try:
                # `force=True` solo para poder tocar elementos de cualquier
                # método de renombrado. La caché y el nombre heredado de una
                # migración anterior SÍ se respetan: el renombrado masivo no
                # debe volver a pedirle a la IA un nombre ya decidido.
                service.resolve_item(item_id, force=True)
            except Exception:  # noqa: BLE001 - no debe tumbar el hilo de fondo
                logger.exception("Error generando nombre IA para %s en el lote %s", item_id, batch_id)
    finally:
        _active_rename_runs.discard(batch_id)
        db.close()
