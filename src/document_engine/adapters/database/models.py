from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class RepositoryConnection(Base):
    __tablename__ = "repository_connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    provider: Mapped[str] = mapped_column(String(50))  # google_drive | ftp | ftps
    name: Mapped[str] = mapped_column(String(200))
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RepositorySnapshot(Base):
    __tablename__ = "repository_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    scope_description: Mapped[str] = mapped_column(Text)
    root_folder_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="RUNNING")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    folder_count: Mapped[int] = mapped_column(Integer, default=0)
    errors_json: Mapped[list] = mapped_column(JSON, default=list)
    metadata_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)


class RepositoryItem(Base):
    __tablename__ = "repository_items"
    __table_args__ = (UniqueConstraint("snapshot_id", "source_item_id", name="uq_repo_item_snapshot_source"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    snapshot_id: Mapped[str] = mapped_column(String(36), ForeignKey("repository_snapshots.id"), index=True)
    source_item_id: Mapped[str] = mapped_column(String(200), index=True)
    parent_source_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    name: Mapped[str] = mapped_column(Text)
    item_type: Mapped[str] = mapped_column(String(20))
    mime_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    modified_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trashed: Mapped[bool] = mapped_column(Boolean, default=False)
    can_download: Mapped[bool] = mapped_column(Boolean, default=True)
    logical_path: Mapped[str] = mapped_column(Text, index=True)


class MigrationBatch(Base):
    __tablename__ = "migration_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    snapshot_id: Mapped[str] = mapped_column(String(36), ForeignKey("repository_snapshots.id"))
    name: Mapped[str] = mapped_column(String(200))
    priority: Mapped[int] = mapped_column(Integer, default=50)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BatchSelector(Base):
    __tablename__ = "batch_selectors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    batch_id: Mapped[str] = mapped_column(String(36), ForeignKey("migration_batches.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    value: Mapped[str] = mapped_column(Text)
    include: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int | None] = mapped_column(Integer, nullable=True)


class MigrationPlan(Base):
    __tablename__ = "migration_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    batch_id: Mapped[str] = mapped_column(String(36), ForeignKey("migration_batches.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class MigrationItem(Base):
    __tablename__ = "migration_items"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_migration_item_idempotency_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    batch_id: Mapped[str] = mapped_column(String(36), ForeignKey("migration_batches.id"), index=True)
    source_item_id: Mapped[str] = mapped_column(String(200))
    source_path: Mapped[str] = mapped_column(Text)
    source_name: Mapped[str] = mapped_column(Text)
    source_mime_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    item_type: Mapped[str] = mapped_column(String(20))
    priority: Mapped[int] = mapped_column(Integer, default=50)
    planned_destination_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    planned_destination_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    extension: Mapped[str | None] = mapped_column(String(20), nullable=True)
    rename_method: Mapped[str | None] = mapped_column(String(30), nullable=True)
    state: Mapped[str] = mapped_column(String(30), default="DISCOVERED", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    downloaded_bytes: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_bytes: Mapped[int] = mapped_column(Integer, default=0)
    local_temp_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    remote_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remote_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), index=True)


class NameDecision(Base):
    __tablename__ = "name_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    migration_item_id: Mapped[str] = mapped_column(String(36), ForeignKey("migration_items.id"), index=True)
    method: Mapped[str] = mapped_column(String(30))
    input_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    suggested_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ai_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ai_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_confidence: Mapped[float | None] = mapped_column(nullable=True)
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False)
    fallback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class TransferCheckpoint(Base):
    __tablename__ = "transfer_checkpoints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    migration_item_id: Mapped[str] = mapped_column(String(36), ForeignKey("migration_items.id"), index=True)
    stage: Mapped[str] = mapped_column(String(30))  # DOWNLOAD | UPLOAD
    bytes_transferred: Mapped[int] = mapped_column(Integer, default=0)
    total_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class ValidationResult(Base):
    __tablename__ = "validation_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    migration_item_id: Mapped[str] = mapped_column(String(36), ForeignKey("migration_items.id"), index=True)
    level: Mapped[str] = mapped_column(String(20))
    passed: Mapped[bool] = mapped_column(Boolean)
    details_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class JournalEvent(Base):
    __tablename__ = "journal_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    migration_item_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(60))
    previous_state: Mapped[str | None] = mapped_column(String(30), nullable=True)
    new_state: Mapped[str | None] = mapped_column(String(30), nullable=True)
    operation: Mapped[str | None] = mapped_column(String(60), nullable=True)
    result: Mapped[str | None] = mapped_column(String(30), nullable=True)
    attempt_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    original_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    name_decision_source: Mapped[str | None] = mapped_column(String(30), nullable=True)
    changed_by_user: Mapped[str | None] = mapped_column(String(100), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class WorkerLease(Base):
    __tablename__ = "worker_leases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    migration_item_id: Mapped[str] = mapped_column(String(36), ForeignKey("migration_items.id"), unique=True)
    lease_owner: Mapped[str] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
