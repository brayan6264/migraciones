from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from document_engine.adapters.database.models import BatchSelector as BatchSelectorModel
from document_engine.adapters.database.models import MigrationBatch as MigrationBatchModel
from document_engine.api.dependencies import get_db, get_naming_engine, get_source_repository, require_api_key
from document_engine.api.schemas import (
    BatchCreate,
    BatchCreateFromSelection,
    BatchOut,
    PreviewOut,
    SelectorCreate,
    SelectorOut,
)
from document_engine.application.discovery_service import AncestorRef, DiscoveryService, SelectionInput
from document_engine.application.planning_service import BatchService, PlanningService, load_export_formats
from document_engine.domain.enums import ItemType, SelectorKind
from document_engine.domain.naming_rules import NamingRulesEngine
from document_engine.ports.source_repository import SourceRepositoryPort
from document_engine.settings import get_settings

router = APIRouter(tags=["batches"], dependencies=[Depends(require_api_key)])


def _planning_service(db: Session, naming_engine: NamingRulesEngine) -> PlanningService:
    settings = get_settings()
    export_formats = {}
    try:
        export_formats = load_export_formats(settings.export_formats_file)
    except FileNotFoundError:
        pass
    return PlanningService(db, naming_engine, export_formats=export_formats)


@router.post("/migration-batches", response_model=BatchOut)
def create_batch(payload: BatchCreate, db: Session = Depends(get_db)) -> MigrationBatchModel:
    return BatchService(db).create_batch(snapshot_id=payload.snapshot_id, name=payload.name, priority=payload.priority)


@router.post("/migration-batches/from-selection", response_model=BatchOut)
def create_batch_from_selection(
    payload: BatchCreateFromSelection,
    db: Session = Depends(get_db),
    source: SourceRepositoryPort = Depends(get_source_repository),
) -> MigrationBatchModel:
    """Crea snapshot + lote + selectores en un solo paso a partir de una
    selección hecha en el explorador visual de Drive (sección "Nueva
    migración"): el usuario nunca ve ni escribe un ID.

    Si `destination_folder_name` viene informado, se antepone como una
    carpeta ancestro sintética (no existe en Drive, solo organiza el
    destino) a la cadena de ancestros de cada selección — así todo el lote
    queda anidado dentro de esa carpeta en el FTP. Si se omite, cada
    selección conserva su ruta de Drive tal cual, migrando directo a la
    raíz configurada (FTP_ROOT_PATH)."""
    wrapper: AncestorRef | None = None
    folder_name = (payload.destination_folder_name or "").strip()
    if folder_name:
        wrapper = AncestorRef(id=f"dest-folder-{uuid.uuid4()}", name=folder_name)

    selections = [
        SelectionInput(
            id=node.id,
            name=node.name,
            type=node.type,
            ancestor_chain=(
                ([wrapper] if wrapper else []) + [AncestorRef(id=a.id, name=a.name) for a in node.ancestor_chain]
            ),
        )
        for node in payload.selections
    ]
    snapshot = DiscoveryService(source, db).run_selection_snapshot(selections)
    batch = BatchService(db).create_batch(snapshot_id=snapshot.id, name=payload.name, priority=payload.priority)
    for node in payload.selections:
        kind = SelectorKind.FOLDER_RECURSIVE if node.type == ItemType.FOLDER.value else SelectorKind.EXPLICIT_IDS
        BatchService(db).add_selector(batch.id, kind=kind, value=node.id, include=True)
    return batch


@router.get("/migration-batches", response_model=list[BatchOut])
def list_batches(db: Session = Depends(get_db), limit: int = Query(default=50, le=200), offset: int = 0):
    stmt = select(MigrationBatchModel).order_by(MigrationBatchModel.created_at.desc()).offset(offset).limit(limit)
    return db.execute(stmt).scalars().all()


@router.get("/migration-batches/{batch_id}", response_model=BatchOut)
def get_batch(batch_id: str, db: Session = Depends(get_db)) -> MigrationBatchModel:
    batch = db.get(MigrationBatchModel, batch_id)
    if batch is None:
        raise HTTPException(404, "No encontrado")
    return batch


@router.get("/migration-batches/{batch_id}/selectors", response_model=list[SelectorOut])
def list_selectors(batch_id: str, db: Session = Depends(get_db)):
    stmt = select(BatchSelectorModel).where(BatchSelectorModel.batch_id == batch_id)
    return db.execute(stmt).scalars().all()


@router.post("/migration-batches/{batch_id}/selectors", response_model=SelectorOut)
def add_selector(batch_id: str, payload: SelectorCreate, db: Session = Depends(get_db)) -> BatchSelectorModel:
    try:
        kind = SelectorKind(payload.kind)
    except ValueError as exc:
        raise HTTPException(422, f"kind inválido: {payload.kind}") from exc
    return BatchService(db).add_selector(
        batch_id, kind=kind, value=payload.value, include=payload.include, priority=payload.priority
    )


@router.delete("/migration-batches/{batch_id}/selectors/{selector_id}", status_code=204)
def remove_selector(batch_id: str, selector_id: str, db: Session = Depends(get_db)) -> None:
    BatchService(db).remove_selector(selector_id)


@router.post("/migration-batches/{batch_id}/plan")
def plan_batch(batch_id: str, db: Session = Depends(get_db), naming_engine: NamingRulesEngine = Depends(get_naming_engine)):
    plan = _planning_service(db, naming_engine).generate_plan(batch_id)
    return {"batch_id": batch_id, "plan_version": plan.version}


@router.get("/migration-batches/{batch_id}/preview", response_model=PreviewOut)
def preview_batch(batch_id: str, db: Session = Depends(get_db), naming_engine: NamingRulesEngine = Depends(get_naming_engine)):
    return _planning_service(db, naming_engine).preview(batch_id)
