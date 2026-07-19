from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from document_engine.adapters.database.models import MigrationItem as MigrationItemModel
from document_engine.domain.enums import MigrationItemState


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def claim_next_item(
    db: Session, batch_id: str, *, worker_owner: str, lease_seconds: int = 120
) -> MigrationItemModel | None:
    """Reclama el siguiente elemento pendiente de un lote mediante un lease
    temporal (sección 4.5, punto 1 y sección 9.4).

    Selecciona entre `READY` y `RETRY_PENDING` cuyo lease esté vencido o
    ausente, ordenando por mayor prioridad y luego por ruta destino (DFS
    estable, igual que en Planning). No usa `SELECT ... FOR UPDATE` porque
    SQLite no lo soporta bien; en producción con PostgreSQL y múltiples
    workers se recomienda añadirlo para evitar una condición de carrera
    entre el `SELECT` y el `UPDATE`.
    """
    now = _utcnow()
    stmt = (
        select(MigrationItemModel)
        .where(MigrationItemModel.batch_id == batch_id)
        .where(
            MigrationItemModel.state.in_(
                [MigrationItemState.READY.value, MigrationItemState.RETRY_PENDING.value]
            )
        )
        .where(or_(MigrationItemModel.lease_expires_at.is_(None), MigrationItemModel.lease_expires_at < now))
        .order_by(MigrationItemModel.priority.desc(), MigrationItemModel.planned_destination_path)
    )
    item = db.execute(stmt).scalars().first()
    if item is None:
        return None

    item.lease_owner = worker_owner
    item.lease_expires_at = now + timedelta(seconds=lease_seconds)
    db.commit()
    return item


def heartbeat(db: Session, migration_item_id: str, *, worker_owner: str, lease_seconds: int = 120) -> None:
    """Extiende el lease de un elemento en proceso (sección 9.4)."""
    item = db.get(MigrationItemModel, migration_item_id)
    if item is None or item.lease_owner != worker_owner:
        return
    item.lease_expires_at = _utcnow() + timedelta(seconds=lease_seconds)
    db.commit()


def release_lease(db: Session, migration_item_id: str) -> None:
    item = db.get(MigrationItemModel, migration_item_id)
    if item is None:
        return
    item.lease_owner = None
    item.lease_expires_at = None
    db.commit()
