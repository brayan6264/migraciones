from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from document_engine.domain.enums import ItemType, MigrationItemState, Priority, SnapshotStatus


@dataclass(frozen=True, slots=True)
class RepositoryItem:
    """Metadatos de un elemento del repositorio de origen, sin su contenido."""

    source_item_id: str
    parent_id: str | None
    name: str
    item_type: ItemType
    mime_type: str | None
    size: int | None
    created_time: datetime | None
    modified_time: datetime | None
    checksum: str | None
    trashed: bool
    can_download: bool
    logical_path: str


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    """Snapshot inmutable de un alcance explorado en el repositorio de origen."""

    id: str
    scope_description: str
    started_at: datetime
    finished_at: datetime | None
    status: SnapshotStatus
    file_count: int
    folder_count: int
    errors: tuple[str, ...] = field(default_factory=tuple)
    metadata_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class BatchSelector:
    """Un selector de elementos dentro de una ola de migración."""

    id: str
    kind: str  # EXPLICIT_IDS | FOLDER_RECURSIVE | PATH_PREFIX | SEARCH_RESULT
    value: str
    include: bool = True


@dataclass(frozen=True, slots=True)
class MigrationBatch:
    id: str
    snapshot_id: str
    name: str
    priority: Priority | int
    selectors: tuple[BatchSelector, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class MigrationItem:
    id: str
    batch_id: str
    source_item_id: str
    source_path: str
    source_name: str
    item_type: ItemType
    priority: int
    planned_destination_path: str | None
    planned_destination_name: str | None
    state: MigrationItemState
    idempotency_key: str
