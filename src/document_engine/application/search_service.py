from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from document_engine.adapters.database.models import RepositoryItem as RepositoryItemModel


class SnapshotSearchService:
    """Búsqueda de elementos dentro de un snapshot ya cerrado."""

    def __init__(self, db: Session):
        self._db = db

    def search(
        self,
        snapshot_id: str,
        *,
        text: str | None = None,
        path_prefix: str | None = None,
        source_item_id: str | None = None,
        item_type: str | None = None,
        mime_type: str | None = None,
        modified_after: datetime | None = None,
        modified_before: datetime | None = None,
        min_size: int | None = None,
        max_size: int | None = None,
        parent_source_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RepositoryItemModel]:
        stmt = select(RepositoryItemModel).where(RepositoryItemModel.snapshot_id == snapshot_id)

        if text:
            stmt = stmt.where(RepositoryItemModel.name.ilike(f"%{text}%"))
        if path_prefix:
            stmt = stmt.where(RepositoryItemModel.logical_path.like(f"{path_prefix}%"))
        if source_item_id:
            stmt = stmt.where(RepositoryItemModel.source_item_id == source_item_id)
        if item_type:
            stmt = stmt.where(RepositoryItemModel.item_type == item_type)
        if mime_type:
            stmt = stmt.where(RepositoryItemModel.mime_type == mime_type)
        if modified_after:
            stmt = stmt.where(RepositoryItemModel.modified_time >= modified_after)
        if modified_before:
            stmt = stmt.where(RepositoryItemModel.modified_time <= modified_before)
        if min_size is not None:
            stmt = stmt.where(RepositoryItemModel.size >= min_size)
        if max_size is not None:
            stmt = stmt.where(RepositoryItemModel.size <= max_size)
        if parent_source_id:
            stmt = stmt.where(RepositoryItemModel.parent_source_id == parent_source_id)

        stmt = stmt.order_by(RepositoryItemModel.logical_path).offset(offset).limit(limit)
        return list(self._db.execute(stmt).scalars().all())
