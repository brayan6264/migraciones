from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from document_engine.adapters.database.models import JournalEvent
from document_engine.adapters.database.models import MigrationBatch as MigrationBatchModel
from document_engine.adapters.database.models import MigrationItem as MigrationItemModel
from document_engine.api.dependencies import (
    get_db,
    get_db_session_factory,
    get_destination_repository,
    get_source_repository,
    get_temp_storage,
    require_api_key,
)
from document_engine.api.schemas import BatchReportOut
from document_engine.application.background_runner import is_running, run_batch_in_background
from document_engine.application.migration_service import Builder
from document_engine.application.recovery_service import RecoveryService
from document_engine.application.validation_service import generate_batch_report
from document_engine.domain.enums import MigrationItemState
from document_engine.domain.state_machine import transition
from document_engine.ports.destination_repository import DestinationRepositoryPort
from document_engine.ports.source_repository import SourceRepositoryPort
from document_engine.adapters.filesystem.temp_storage import TempFileStorage

router = APIRouter(tags=["execution"], dependencies=[Depends(require_api_key)])


def _get_batch(db: Session, batch_id: str) -> MigrationBatchModel:
    batch = db.get(MigrationBatchModel, batch_id)
    if batch is None:
        raise HTTPException(404, "No encontrado")
    return batch


@router.post("/migration-batches/{batch_id}/start")
def start_batch(
    batch_id: str,
    max_items: int = Query(default=10, le=500, description="Procesa hasta N elementos de forma síncrona"),
    worker_owner: str = Query(default="api-inline-worker"),
    db: Session = Depends(get_db),
    source: SourceRepositoryPort = Depends(get_source_repository),
    destination: DestinationRepositoryPort = Depends(get_destination_repository),
    temp_storage: TempFileStorage = Depends(get_temp_storage),
) -> dict:
    """Procesa hasta `max_items` elementos de forma síncrona dentro de la
    misma petición HTTP. Para volúmenes grandes, usar
    `scripts/run_worker.py` como proceso independiente en lugar de este
    endpoint (documentado en el README como limitación conocida del MVP)."""
    batch = _get_batch(db, batch_id)
    batch.status = "RUNNING"

    # Confirma el plan: los elementos PLANNED que no requieren revisión pasan
    # a READY. Los que están en WAITING_REVIEW se quedan ahí hasta que un
    # revisor los apruebe explícitamente (name_review router).
    stmt = (
        select(MigrationItemModel)
        .where(MigrationItemModel.batch_id == batch_id)
        .where(MigrationItemModel.state == MigrationItemState.PLANNED.value)
    )
    for planned_item in db.execute(stmt).scalars().all():
        planned_item.state = transition(MigrationItemState.PLANNED, MigrationItemState.READY).value
    db.commit()

    from document_engine.worker.lease_manager import claim_next_item

    builder = Builder(db, source, destination, temp_storage)
    processed = []
    for _ in range(max_items):
        item = claim_next_item(db, batch_id, worker_owner=worker_owner)
        if item is None:
            break
        resolved = builder.process_item(item.id)
        processed.append({"id": resolved.id, "state": resolved.state})

    return {"batch_id": batch_id, "processed": processed}


@router.post("/migration-batches/{batch_id}/run")
def run_batch(
    batch_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    session_factory=Depends(get_db_session_factory),
    source: SourceRepositoryPort = Depends(get_source_repository),
    destination: DestinationRepositoryPort = Depends(get_destination_repository),
    temp_storage: TempFileStorage = Depends(get_temp_storage),
) -> dict:
    """Arranca el procesamiento completo del lote en segundo plano, en el
    proceso del servidor: sigue corriendo aunque se cierre la pestaña del
    navegador o el frontend entero (a diferencia de `/start`, que solo
    procesa una tanda y depende de que el cliente vuelva a llamarlo).
    Se detiene solo al terminar, o si se pausa/cancela el lote."""
    batch = _get_batch(db, batch_id)

    stmt = (
        select(MigrationItemModel)
        .where(MigrationItemModel.batch_id == batch_id)
        .where(MigrationItemModel.state == MigrationItemState.PLANNED.value)
    )
    for planned_item in db.execute(stmt).scalars().all():
        planned_item.state = transition(MigrationItemState.PLANNED, MigrationItemState.READY).value

    if is_running(batch_id):
        db.commit()
        return {"batch_id": batch_id, "status": "already_running"}

    batch.status = "RUNNING"
    db.commit()

    background_tasks.add_task(
        run_batch_in_background,
        batch_id,
        session_factory=session_factory,
        source=source,
        destination=destination,
        temp_storage=temp_storage,
    )
    return {"batch_id": batch_id, "status": "started"}


@router.post("/migration-batches/{batch_id}/pause")
def pause_batch(batch_id: str, db: Session = Depends(get_db)) -> dict:
    batch = _get_batch(db, batch_id)
    batch.status = "PAUSED"
    db.commit()
    return {"batch_id": batch_id, "status": batch.status}


@router.post("/migration-batches/{batch_id}/resume")
def resume_batch(batch_id: str, db: Session = Depends(get_db)) -> dict:
    batch = _get_batch(db, batch_id)
    batch.status = "PLANNED"
    db.commit()
    return {"batch_id": batch_id, "status": batch.status}


@router.post("/migration-batches/{batch_id}/cancel")
def cancel_batch(batch_id: str, db: Session = Depends(get_db)) -> dict:
    batch = _get_batch(db, batch_id)
    batch.status = "CANCELLED"
    db.commit()
    return {"batch_id": batch_id, "status": batch.status}


@router.post("/migration-batches/{batch_id}/retry-failed")
def retry_failed(batch_id: str, db: Session = Depends(get_db)) -> dict:
    stmt = (
        select(MigrationItemModel)
        .where(MigrationItemModel.batch_id == batch_id)
        .where(MigrationItemModel.state == MigrationItemState.FAILED.value)
    )
    items = db.execute(stmt).scalars().all()
    for item in items:
        item.state = MigrationItemState.RETRY_PENDING.value
    db.commit()
    return {"batch_id": batch_id, "retried": len(items)}


@router.get("/migration-batches/{batch_id}/status")
def batch_status(batch_id: str, db: Session = Depends(get_db)) -> dict:
    batch = _get_batch(db, batch_id)
    stmt = select(MigrationItemModel).where(MigrationItemModel.batch_id == batch_id)
    items = db.execute(stmt).scalars().all()
    counts: dict[str, int] = {}
    for item in items:
        counts[item.state] = counts.get(item.state, 0) + 1
    return {
        "batch_id": batch_id,
        "status": batch.status,
        "counts_by_state": counts,
        "background_running": is_running(batch_id),
    }


@router.get("/migration-batches/{batch_id}/events")
def batch_events(
    batch_id: str, db: Session = Depends(get_db), limit: int = Query(default=100, le=1000), offset: int = 0
):
    stmt = (
        select(JournalEvent)
        .where(JournalEvent.batch_id == batch_id)
        .order_by(JournalEvent.timestamp.desc())
        .offset(offset)
        .limit(limit)
    )
    events = db.execute(stmt).scalars().all()
    return [
        {
            "timestamp": e.timestamp,
            "event_type": e.event_type,
            "migration_item_id": e.migration_item_id,
            "operation": e.operation,
            "result": e.result,
            "error_code": e.error_code,
            "final_name": e.final_name,
        }
        for e in events
    ]


@router.get("/migration-batches/{batch_id}/report", response_model=BatchReportOut)
def batch_report(batch_id: str, db: Session = Depends(get_db)):
    _get_batch(db, batch_id)
    return generate_batch_report(db, batch_id)


@router.post("/migration-batches/{batch_id}/recover")
def recover_batch(
    batch_id: str,
    db: Session = Depends(get_db),
    destination: DestinationRepositoryPort = Depends(get_destination_repository),
    temp_storage: TempFileStorage = Depends(get_temp_storage),
) -> dict:
    """No está en la lista mínima de la sección 12.1, pero es necesario para
    exponer `RecoveryService` (sección 9.5) vía API en lugar de solo scripts."""
    recovered = RecoveryService(db, destination, temp_storage).recover_batch(batch_id)
    return {"batch_id": batch_id, "recovered": len(recovered)}
