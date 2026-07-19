from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from document_engine.adapters.database.models import RepositoryItem as RepositoryItemModel
from document_engine.adapters.database.models import RepositorySnapshot as RepositorySnapshotModel
from document_engine.domain.enums import SnapshotStatus
from document_engine.ports.source_repository import SourceRepositoryPort


class DiscoveryService:
    """Explora el repositorio de origen y persiste un snapshot inmutable.

    Nunca escribe en el origen. Cada llamada crea un snapshot nuevo; los
    snapshots previos no se modifican.
    """

    def __init__(self, source: SourceRepositoryPort, db: Session):
        self._source = source
        self._db = db

    def run_full_snapshot(self, root_folder_id: str) -> RepositorySnapshotModel:
        return self._run_snapshot(
            root_folder_ids=[root_folder_id],
            scope_description=f"full:{root_folder_id}",
        )

    def run_partial_snapshot(self, folder_ids: list[str]) -> RepositorySnapshotModel:
        scope = "partial:" + ",".join(sorted(folder_ids))
        return self._run_snapshot(root_folder_ids=folder_ids, scope_description=scope)

    def _run_snapshot(self, *, root_folder_ids: list[str], scope_description: str) -> RepositorySnapshotModel:
        snapshot = RepositorySnapshotModel(
            scope_description=scope_description,
            root_folder_id=root_folder_ids[0] if len(root_folder_ids) == 1 else None,
            status=SnapshotStatus.RUNNING.value,
            started_at=datetime.now(timezone.utc),
        )
        self._db.add(snapshot)
        self._db.flush()

        file_count = 0
        folder_count = 0
        errors: list[str] = []
        fingerprint_parts: list[str] = []

        try:
            for root_id in root_folder_ids:
                for item in self._source.walk(root_id):
                    if item.trashed:
                        continue
                    model = RepositoryItemModel(
                        snapshot_id=snapshot.id,
                        source_item_id=item.source_item_id,
                        parent_source_id=item.parent_id,
                        name=item.name,
                        item_type=item.item_type.value,
                        mime_type=item.mime_type,
                        size=item.size,
                        created_time=item.created_time,
                        modified_time=item.modified_time,
                        checksum=item.checksum,
                        trashed=item.trashed,
                        can_download=item.can_download,
                        logical_path=item.logical_path,
                    )
                    self._db.add(model)
                    if item.item_type.value == "FOLDER":
                        folder_count += 1
                    else:
                        file_count += 1
                    fingerprint_parts.append(
                        f"{item.source_item_id}:{item.modified_time}:{item.checksum}"
                    )
            snapshot.status = SnapshotStatus.COMPLETED.value
        except Exception as exc:  # noqa: BLE001 - registrado en el snapshot, no silenciado
            errors.append(str(exc))
            snapshot.status = SnapshotStatus.FAILED.value
            raise
        finally:
            snapshot.file_count = file_count
            snapshot.folder_count = folder_count
            snapshot.errors_json = errors
            snapshot.finished_at = datetime.now(timezone.utc)
            snapshot.metadata_fingerprint = hashlib.sha256(
                "|".join(sorted(fingerprint_parts)).encode("utf-8")
            ).hexdigest()
            self._db.commit()

        return snapshot
