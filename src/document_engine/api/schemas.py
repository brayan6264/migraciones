from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class SnapshotOut(BaseModel):
    id: str
    scope_description: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    file_count: int
    folder_count: int
    metadata_fingerprint: str | None

    model_config = {"from_attributes": True}


class DiscoveryRunCreate(BaseModel):
    root_folder_id: str | None = None
    folder_ids: list[str] | None = Field(default=None, description="Para un snapshot parcial")


class DriveBrowseItemOut(BaseModel):
    id: str
    name: str
    type: str
    mime_type: str | None
    size: int | None


class FtpBrowseItemOut(BaseModel):
    name: str
    path: str


class AncestorRef(BaseModel):
    id: str
    name: str


class SelectionNode(BaseModel):
    id: str
    name: str
    type: str  # FOLDER | FILE
    ancestor_chain: list[AncestorRef] = Field(
        default_factory=list, description="Desde la raíz configurada hasta el padre de este ítem"
    )


class BatchCreateFromSelection(BaseModel):
    name: str
    priority: int = Field(default=50, ge=0, le=100)
    selections: list[SelectionNode]
    destination_base_path: str | None = Field(
        default=None,
        description=(
            "Ruta ya existente en el FTP (elegida explorando el servidor) donde queda "
            "todo lo migrado. Se usa tal cual, sin normalizar — es un directorio real "
            "que ya está ahí. Si se omite, se migra desde la raíz configurada (FTP_ROOT_PATH)."
        ),
    )
    destination_folder_name: str | None = Field(
        default=None,
        description=(
            "Si se indica, se crea esta carpeta nueva dentro de `destination_base_path` "
            "(o de la raíz, si no se indicó base) y todo lo seleccionado queda dentro. "
            "Si se omite, se migra directo al destino elegido, conservando los nombres "
            "de carpeta tal como están en Drive."
        ),
    )


class RepositoryItemOut(BaseModel):
    source_item_id: str
    parent_source_id: str | None
    name: str
    item_type: str
    mime_type: str | None
    size: int | None
    logical_path: str

    model_config = {"from_attributes": True}


class BatchCreate(BaseModel):
    snapshot_id: str
    name: str
    priority: int = Field(default=50, ge=0, le=100)
    destination_base_path: str | None = None


class BatchOut(BaseModel):
    id: str
    snapshot_id: str
    name: str
    priority: int
    status: str
    destination_base_path: str | None = None

    model_config = {"from_attributes": True}


class SelectorCreate(BaseModel):
    kind: str
    value: str
    include: bool = True
    priority: int | None = Field(default=None, ge=0, le=100)


class SelectorOut(BaseModel):
    id: str
    kind: str
    value: str
    include: bool
    priority: int | None

    model_config = {"from_attributes": True}


class MigrationItemOut(BaseModel):
    id: str
    source_path: str
    source_name: str
    item_type: str
    priority: int
    planned_destination_path: str | None
    planned_destination_name: str | None
    rename_method: str | None
    state: str
    attempt_count: int
    last_error_code: str | None

    model_config = {"from_attributes": True}


class PreviewOut(BaseModel):
    total_items: int
    folders: int
    files: int
    total_size_bytes: int
    blocked_count: int
    needs_review_count: int
    collisions_resolved: int
    items: list[dict]


class DestinationNameUpdate(BaseModel):
    new_base_name: str
    changed_by: str


class ApproveNameRequest(BaseModel):
    changed_by: str


class RegenerateAiNameRequest(BaseModel):
    changed_by: str
    force: bool = False
    obtc_code: str | None = None
    date: str | None = None
    version: str | None = None
    category: str | None = None


class BatchReportOut(BaseModel):
    batch_id: str
    total_discovered: int
    total_selected: int
    total_completed: int
    total_skipped: int
    total_blocked: int
    total_failed: int
    bytes_transferred: int
    renamed_by_rules: int
    renamed_by_ai: int
    pending_review: int
    collisions_resolved: int
    duration_seconds: float | None


class ConnectivityTestOut(BaseModel):
    ok: bool
    detail: str
