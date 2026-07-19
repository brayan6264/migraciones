from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from document_engine.adapters.database.models import MigrationItem as MigrationItemModel
from document_engine.api.dependencies import get_db, require_api_key
from document_engine.api.schemas import MigrationItemOut
from document_engine.domain.enums import MigrationItemState
from document_engine.domain.state_machine import transition

router = APIRouter(tags=["items"], dependencies=[Depends(require_api_key)])


def _get_item(db: Session, item_id: str) -> MigrationItemModel:
    item = db.get(MigrationItemModel, item_id)
    if item is None:
        raise HTTPException(404, "No encontrado")
    return item


@router.get("/migration-items/{item_id}", response_model=MigrationItemOut)
def get_item(item_id: str, db: Session = Depends(get_db)) -> MigrationItemModel:
    return _get_item(db, item_id)


@router.post("/migration-items/{item_id}/retry", response_model=MigrationItemOut)
def retry_item(item_id: str, db: Session = Depends(get_db)) -> MigrationItemModel:
    item = _get_item(db, item_id)
    if item.state != MigrationItemState.FAILED.value:
        raise HTTPException(409, f"Solo se puede reintentar un elemento FAILED (estado actual: {item.state})")
    item.state = transition(MigrationItemState(item.state), MigrationItemState.RETRY_PENDING).value
    db.commit()
    return item


@router.post("/migration-items/{item_id}/skip", response_model=MigrationItemOut)
def skip_item(item_id: str, db: Session = Depends(get_db)) -> MigrationItemModel:
    item = _get_item(db, item_id)
    item.state = transition(MigrationItemState(item.state), MigrationItemState.SKIPPED).value
    db.commit()
    return item


@router.post("/migration-items/{item_id}/reprocess", response_model=MigrationItemOut)
def reprocess_item(item_id: str, db: Session = Depends(get_db)) -> MigrationItemModel:
    """Reintenta un elemento `FAILED` o `BLOCKED`. Un elemento `COMPLETED`
    nunca se reprocesa automáticamente (principio de no-repetición, sección
    9.1): requeriría una orden explícita fuera de este MVP."""
    item = _get_item(db, item_id)
    if item.state == MigrationItemState.BLOCKED.value:
        item.state = transition(MigrationItemState(item.state), MigrationItemState.WAITING_REVIEW).value
    elif item.state == MigrationItemState.FAILED.value:
        item.state = transition(MigrationItemState(item.state), MigrationItemState.RETRY_PENDING).value
    else:
        raise HTTPException(409, f"No se puede reprocesar desde el estado {item.state}")
    db.commit()
    return item
