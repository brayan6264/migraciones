from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from document_engine.adapters.database.models import MigrationItem as MigrationItemModel
from document_engine.api.dependencies import get_db, get_ai_naming_provider, get_naming_engine, require_api_key
from document_engine.api.schemas import (
    ApproveNameRequest,
    DestinationNameUpdate,
    MigrationItemOut,
    RegenerateAiNameRequest,
)
from document_engine.application.naming_service import NamingAssistantService
from document_engine.application.planning_service import NameReviewService
from document_engine.domain.enums import MigrationItemState, RenameMethod
from document_engine.domain.naming_rules import NamingRulesEngine
from document_engine.ports.ai_naming_provider import AINamingProviderPort
from document_engine.settings import get_settings

router = APIRouter(tags=["name-review"], dependencies=[Depends(require_api_key)])


@router.get("/migration-batches/{batch_id}/name-reviews", response_model=list[MigrationItemOut])
def list_name_reviews(batch_id: str, db: Session = Depends(get_db)):
    stmt = (
        select(MigrationItemModel)
        .where(MigrationItemModel.batch_id == batch_id)
        .where(MigrationItemModel.state == MigrationItemState.WAITING_REVIEW.value)
    )
    return db.execute(stmt).scalars().all()


@router.patch("/migration-items/{item_id}/destination-name", response_model=MigrationItemOut)
def override_destination_name(item_id: str, payload: DestinationNameUpdate, db: Session = Depends(get_db)):
    return NameReviewService(db).override_destination_name(item_id, payload.new_base_name, changed_by=payload.changed_by)


@router.post("/migration-items/{item_id}/approve-name", response_model=MigrationItemOut)
def approve_name(item_id: str, payload: ApproveNameRequest, db: Session = Depends(get_db)):
    return NameReviewService(db).approve_name(item_id, changed_by=payload.changed_by)


@router.post("/migration-items/{item_id}/regenerate-ai-name", response_model=MigrationItemOut)
def regenerate_ai_name(
    item_id: str,
    payload: RegenerateAiNameRequest,
    db: Session = Depends(get_db),
    naming_engine: NamingRulesEngine = Depends(get_naming_engine),
    ai_provider: AINamingProviderPort | None = Depends(get_ai_naming_provider),
):
    item = db.get(MigrationItemModel, item_id)
    if item is None:
        raise HTTPException(404, "No encontrado")

    is_over_limit = item.rename_method == RenameMethod.AI_ASSISTED.value
    if not is_over_limit and not payload.force:
        raise HTTPException(
            409,
            "regenerate-ai-name solo aplica a nombres que superan 25 caracteres, salvo orden explícita (force=true)",
        )
    if ai_provider is None:
        raise HTTPException(503, "OPENAI_RENAME_ENABLED está apagado o falta OPENAI_API_KEY")

    settings = get_settings()
    service = NamingAssistantService(db, ai_provider, naming_engine, ai_model_name=settings.openai_rename_model)
    return service.resolve_item(
        item_id,
        obtc_code=payload.obtc_code,
        date=payload.date,
        version=payload.version,
        category=payload.category,
        force=payload.force,
    )
