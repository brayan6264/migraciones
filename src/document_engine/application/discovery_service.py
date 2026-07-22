from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from document_engine.adapters.database.models import RepositoryItem as RepositoryItemModel
from document_engine.adapters.database.models import RepositorySnapshot as RepositorySnapshotModel
from document_engine.domain.entities import RepositoryItem
from document_engine.domain.enums import ItemType, SnapshotStatus
from document_engine.domain.errors import PermanentError
from document_engine.ports.source_repository import SourceRepositoryPort


@dataclass(frozen=True)
class AncestorRef:
    id: str
    name: str


@dataclass(frozen=True)
class SelectionInput:
    """Un ítem elegido en el explorador visual, con la cadena de
    ancestros tal como la conoce el frontend por el breadcrumb de
    navegación (desde la raíz configurada hasta el padre de este ítem)."""

    id: str
    name: str
    type: str  # "FOLDER" | "FILE"
    ancestor_chain: list[AncestorRef] = field(default_factory=list)


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

    def run_selection_snapshot(self, selections: list[SelectionInput]) -> RepositorySnapshotModel:
        """Snapshot dirigido a partir de una selección hecha en el
        explorador visual de Drive (sección "Nueva migración" del panel).

        A diferencia de `run_full_snapshot`, no rastrea el árbol completo
        desde la raíz: la cadena de carpetas ancestro ya la conoce el
        frontend (por el breadcrumb de navegación) y se registra tal cual,
        sin llamar a Drive — evita además la limitación real de que
        `files.get` con autenticación por API key no devuelve `parents`.
        Solo se recorre con `walk()` el subárbol de las carpetas
        efectivamente seleccionadas."""
        if not selections:
            raise PermanentError("La selección no puede estar vacía")

        scope = "selection:" + ",".join(sorted(s.id for s in selections))
        snapshot = RepositorySnapshotModel(
            scope_description=scope,
            root_folder_id=None,
            status=SnapshotStatus.RUNNING.value,
            started_at=datetime.now(timezone.utc),
        )
        self._db.add(snapshot)
        self._db.flush()

        file_count = 0
        folder_count = 0
        errors: list[str] = []
        fingerprint_parts: list[str] = []
        registered_ids: set[str] = set()

        def register(item: RepositoryItem, parent_source_id: str | None, logical_path: str) -> None:
            nonlocal file_count, folder_count
            if item.source_item_id in registered_ids:
                return
            registered_ids.add(item.source_item_id)
            self._db.add(
                RepositoryItemModel(
                    snapshot_id=snapshot.id,
                    source_item_id=item.source_item_id,
                    parent_source_id=parent_source_id,
                    name=item.name,
                    item_type=item.item_type.value,
                    mime_type=item.mime_type,
                    size=item.size,
                    created_time=item.created_time,
                    modified_time=item.modified_time,
                    checksum=item.checksum,
                    trashed=item.trashed,
                    can_download=item.can_download,
                    logical_path=logical_path,
                )
            )
            if item.item_type == ItemType.FOLDER:
                folder_count += 1
            else:
                file_count += 1
            fingerprint_parts.append(f"{item.source_item_id}:{item.modified_time}:{item.checksum}")

        try:
            for selection in selections:
                parent_id: str | None = None
                path = ""
                for ancestor in selection.ancestor_chain:
                    path = f"{path}/{ancestor.name}" if path else ancestor.name
                    register(
                        RepositoryItem(
                            source_item_id=ancestor.id,
                            parent_id=parent_id,
                            name=ancestor.name,
                            item_type=ItemType.FOLDER,
                            mime_type="application/vnd.google-apps.folder",
                            size=None,
                            created_time=None,
                            modified_time=None,
                            checksum=None,
                            trashed=False,
                            can_download=True,
                            logical_path=path,
                        ),
                        parent_id,
                        path,
                    )
                    parent_id = ancestor.id

                if selection.type == "FOLDER":
                    for walked in self._source.walk(selection.id):
                        if walked.trashed:
                            continue
                        full_path = f"{path}/{walked.logical_path}" if path else walked.logical_path
                        effective_parent = parent_id if walked.source_item_id == selection.id else walked.parent_id
                        register(walked, effective_parent, full_path)
                else:
                    item = self._source.get_item(selection.id)
                    full_path = f"{path}/{item.name}" if path else item.name
                    register(item, parent_id, full_path)
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
