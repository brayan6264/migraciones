"""Herencia de nombres ya decididos para el mismo elemento de origen.

Regla de negocio: un archivo o carpeta que ya fue nombrado en una migración
anterior NO vuelve a pasar por IA. Se reutiliza tal cual el nombre que ya
quedó (o va a quedar) en el destino, porque volver a preguntarle al modelo
produce una respuesta distinta cada vez — el mismo `F0E0. Comités de
Seguimiento` terminó como `F0E0_COMITES_SEGUIMIENT`, `F0E0_COMITES_DE_SEGUIMIEN`
y `F0E0_COMITE_SEGUIMIENTO` en tres lotes — y eso crea carpetas duplicadas en
el FTP para una sola carpeta de Drive.

Este módulo es el único lugar que decide "¿este origen ya tiene nombre?", y
lo usan tanto la planificación (para no marcar el elemento como pendiente de
revisión) como el asistente de IA (para no gastar una llamada al modelo).
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from document_engine.adapters.database.models import MigrationItem as MigrationItemModel
from document_engine.adapters.database.models import NameDecision
from document_engine.domain.enums import MigrationItemState, RenameMethod

# Estados que solo se alcanzan DESPUÉS de que el nombre quedó aprobado
# (la máquina de estados obliga a pasar por READY antes de cualquier
# transferencia), así que su `planned_destination_name` es una decisión
# firme y no un marcador de posición.
DECIDED_STATES: frozenset[str] = frozenset(
    {
        MigrationItemState.READY.value,
        MigrationItemState.CREATING_DIRECTORIES.value,
        MigrationItemState.DOWNLOADING.value,
        MigrationItemState.DOWNLOADED.value,
        MigrationItemState.UPLOADING.value,
        MigrationItemState.UPLOADED_TEMP.value,
        MigrationItemState.VALIDATING.value,
        MigrationItemState.COMPLETED.value,
        MigrationItemState.RETRY_PENDING.value,
        MigrationItemState.FAILED.value,
        MigrationItemState.SKIPPED.value,
    }
)

# WAITING_REVIEW es ambiguo y por eso se trata aparte: un elemento cuyo nombre
# supera 25 caracteres entra en ese estado en la planificación con un nombre
# TRUNCADO (`normalized.base[:25]`) que nadie decidió todavía. Heredar esa
# truncadura sería propagar basura. Solo cuenta si además existe una
# `NameDecision` real: de la IA (única que graba `input_fingerprint`) o de una
# sobrescritura manual.
_HUMAN_DECISION_METHODS = frozenset({RenameMethod.MANUAL_OVERRIDE.value})

# Postgres limita los parámetros por sentencia; los lotes grandes superan los
# 20.000 elementos, así que el `IN (...)` se parte en trozos.
_CHUNK_SIZE = 2000


def _chunks(values: list[str], size: int = _CHUNK_SIZE) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _strip_extension(name: str, extension: str | None) -> str:
    if extension and name.endswith(f".{extension}"):
        return name[: -(len(extension) + 1)]
    return name


def _items_with_real_decision(db: Session, item_ids: list[str]) -> set[str]:
    """De los `migration_items` dados, cuáles tienen una decisión de nombre
    efectiva (IA resuelta o sobrescritura manual) registrada en `NameDecision`."""
    if not item_ids:
        return set()
    resolved: set[str] = set()
    for chunk in _chunks(item_ids):
        rows = db.execute(
            select(NameDecision.migration_item_id, NameDecision.input_fingerprint, NameDecision.method).where(
                NameDecision.migration_item_id.in_(chunk)
            )
        ).all()
        for migration_item_id, fingerprint, method in rows:
            if fingerprint is not None or method in _HUMAN_DECISION_METHODS:
                resolved.add(migration_item_id)
    return resolved


def inherited_bases(
    db: Session,
    source_item_ids: Iterable[str],
    *,
    exclude_batch_id: str | None = None,
) -> dict[str, str]:
    """Devuelve `{source_item_id: base_del_nombre_ya_decidido}` (sin extensión).

    Se prefiere el nombre que efectivamente llegó al destino: primero el más
    recientemente completado y, si ninguno lo está, la decisión más reciente.
    """
    ids = [i for i in dict.fromkeys(source_item_ids) if i]
    if not ids:
        return {}

    candidates: list[MigrationItemModel] = []
    for chunk in _chunks(ids):
        stmt = select(MigrationItemModel).where(
            MigrationItemModel.source_item_id.in_(chunk),
            MigrationItemModel.planned_destination_name.isnot(None),
            MigrationItemModel.state.in_(list(DECIDED_STATES) + [MigrationItemState.WAITING_REVIEW.value]),
        )
        if exclude_batch_id is not None:
            stmt = stmt.where(MigrationItemModel.batch_id != exclude_batch_id)
        candidates.extend(db.execute(stmt).scalars().all())

    if not candidates:
        return {}

    # Los WAITING_REVIEW solo entran si detrás hay una decisión real.
    pending_ids = [c.id for c in candidates if c.state == MigrationItemState.WAITING_REVIEW.value]
    decided_review_ids = _items_with_real_decision(db, pending_ids)

    usable = [
        c
        for c in candidates
        if c.state in DECIDED_STATES or c.id in decided_review_ids
    ]
    if not usable:
        return {}

    # Orden en Python (no en SQL) porque los candidatos vienen de varios
    # trozos: completados primero y, dentro de cada grupo, lo más reciente.
    usable.sort(
        key=lambda c: (
            c.completed_at is None,
            -(c.completed_at.timestamp() if c.completed_at else 0.0),
            -(c.updated_at.timestamp() if c.updated_at else 0.0),
        )
    )

    result: dict[str, str] = {}
    for candidate in usable:
        if candidate.source_item_id in result:
            continue
        base = _strip_extension(candidate.planned_destination_name or "", candidate.extension)
        if base:
            result[candidate.source_item_id] = base
    return result


def inherited_base_for_item(db: Session, item: MigrationItemModel) -> str | None:
    """Versión de un solo elemento: el nombre ya decidido para su mismo
    origen en CUALQUIER otro lote, o `None` si es la primera vez que se ve."""
    return inherited_bases(db, [item.source_item_id], exclude_batch_id=item.batch_id).get(
        item.source_item_id
    )
