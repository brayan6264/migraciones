from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from document_engine.adapters.database.models import RepositorySnapshot as RepositorySnapshotModel
from document_engine.api.dependencies import get_db, get_source_repository, require_api_key
from document_engine.api.schemas import DiscoveryRunCreate, RepositoryItemOut, SnapshotOut
from document_engine.application.discovery_service import DiscoveryService
from document_engine.application.search_service import SnapshotSearchService
from document_engine.ports.source_repository import SourceRepositoryPort

router = APIRouter(tags=["discovery"], dependencies=[Depends(require_api_key)])


@router.post("/discovery-runs", response_model=SnapshotOut)
def create_discovery_run(
    payload: DiscoveryRunCreate,
    db: Session = Depends(get_db),
    source: SourceRepositoryPort = Depends(get_source_repository),
) -> RepositorySnapshotModel:
    """Ejecuta el discovery de forma síncrona (MVP). Para repositorios muy
    grandes, usar `scripts/run_worker.py` o un job en segundo plano en lugar
    de esta llamada HTTP bloqueante."""
    service = DiscoveryService(source, db)
    if payload.folder_ids:
        return service.run_partial_snapshot(payload.folder_ids)
    if not payload.root_folder_id:
        raise HTTPException(400, "Debe indicar root_folder_id o folder_ids")
    return service.run_full_snapshot(payload.root_folder_id)


@router.post("/discovery-runs/{run_id}/pause")
def pause_discovery_run(run_id: str) -> None:
    raise HTTPException(501, "Discovery corre de forma síncrona en este MVP; pausar no aplica")


@router.post("/discovery-runs/{run_id}/resume")
def resume_discovery_run(run_id: str) -> None:
    raise HTTPException(501, "Discovery corre de forma síncrona en este MVP; resumir no aplica")


@router.get("/discovery-runs/{run_id}", response_model=SnapshotOut)
def get_discovery_run(run_id: str, db: Session = Depends(get_db)) -> RepositorySnapshotModel:
    snapshot = db.get(RepositorySnapshotModel, run_id)
    if snapshot is None:
        raise HTTPException(404, "No encontrado")
    return snapshot


@router.get("/snapshots", response_model=list[SnapshotOut])
def list_snapshots(db: Session = Depends(get_db), limit: int = Query(default=50, le=200), offset: int = 0):
    stmt = select(RepositorySnapshotModel).order_by(RepositorySnapshotModel.started_at.desc()).offset(offset).limit(limit)
    return db.execute(stmt).scalars().all()


@router.get("/snapshots/{snapshot_id}", response_model=SnapshotOut)
def get_snapshot(snapshot_id: str, db: Session = Depends(get_db)) -> RepositorySnapshotModel:
    snapshot = db.get(RepositorySnapshotModel, snapshot_id)
    if snapshot is None:
        raise HTTPException(404, "No encontrado")
    return snapshot


@router.get("/snapshots/{snapshot_id}/items/search", response_model=list[RepositoryItemOut])
def search_snapshot_items(
    snapshot_id: str,
    db: Session = Depends(get_db),
    text: str | None = None,
    path_prefix: str | None = None,
    item_type: str | None = None,
    mime_type: str | None = None,
    parent_source_id: str | None = None,
    limit: int = Query(default=100, le=500),
    offset: int = 0,
):
    service = SnapshotSearchService(db)
    return service.search(
        snapshot_id,
        text=text,
        path_prefix=path_prefix,
        item_type=item_type,
        mime_type=mime_type,
        parent_source_id=parent_source_id,
        limit=limit,
        offset=offset,
    )
