from __future__ import annotations

import logging
import uuid
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from document_engine.adapters.database.models import MigrationBatch as MigrationBatchModel
from document_engine.adapters.database.models import MigrationItem as MigrationItemModel
from document_engine.adapters.filesystem.temp_storage import TempFileStorage
from document_engine.application.migration_service import Builder
from document_engine.domain.enums import MigrationItemState
from document_engine.ports.destination_repository import DestinationRepositoryPort
from document_engine.ports.source_repository import SourceRepositoryPort
from document_engine.worker.lease_manager import claim_next_item

logger = logging.getLogger(__name__)

_active_runs: set[str] = set()

_TERMINAL_STATES = (
    MigrationItemState.COMPLETED.value,
    MigrationItemState.FAILED.value,
    MigrationItemState.BLOCKED.value,
    MigrationItemState.CANCELLED.value,
    MigrationItemState.SKIPPED.value,
)


def is_running(batch_id: str) -> bool:
    return batch_id in _active_runs


def run_batch_in_background(
    batch_id: str,
    *,
    session_factory: Callable[[], Session],
    source: SourceRepositoryPort,
    destination: DestinationRepositoryPort,
    temp_storage: TempFileStorage,
) -> None:
    """Corre como `BackgroundTask` de FastAPI: sigue procesando elementos
    del lote hasta agotarlos, sin depender de que el request HTTP original
    siga abierto. Vive en el proceso del servidor, así que sobrevive a que
    se cierre la pestaña del navegador o el frontend entero — solo se
    detiene al terminar, o si el lote se pausa/cancela desde otro request
    (chequeo cooperativo en cada vuelta), o si el propio servidor se apaga.

    `session_factory` se recibe por parámetro (en vez de resolverse aquí
    dentro) para que sea la misma que usó el request que disparó la tarea
    — necesario para que los tests puedan aislar su base de datos, ya que
    una `BackgroundTask` vive fuera del ciclo normal de dependencias."""
    if batch_id in _active_runs:
        return
    _active_runs.add(batch_id)
    db = session_factory()
    worker_owner = f"bg-{uuid.uuid4().hex[:8]}"
    try:
        builder = Builder(db, source, destination, temp_storage)
        while True:
            batch = db.get(MigrationBatchModel, batch_id)
            if batch is None or batch.status != "RUNNING":
                break
            item = claim_next_item(db, batch_id, worker_owner=worker_owner)
            if item is None:
                break
            try:
                builder.process_item(item.id)
            except Exception:  # noqa: BLE001 - no debe tumbar el hilo de fondo
                logger.exception("Error procesando %s en el lote %s", item.id, batch_id)

        batch = db.get(MigrationBatchModel, batch_id)
        if batch is not None and batch.status == "RUNNING":
            # No solo "¿hay algo para reclamar?": si algo quedó atascado en
            # un estado intermedio no terminal (p. ej. por un error no
            # traducido durante la descarga/subida), tampoco se marca como
            # completado — mejor reportar "incompleto" que mentir.
            not_done = (
                db.execute(
                    select(MigrationItemModel.id)
                    .where(MigrationItemModel.batch_id == batch_id)
                    .where(MigrationItemModel.state.notin_(_TERMINAL_STATES))
                )
                .scalars()
                .first()
            )
            batch.status = "PLANNED" if not_done is not None else "COMPLETED"
            db.commit()
    finally:
        _active_runs.discard(batch_id)
        db.close()
