from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from document_engine.adapters.database.models import JournalEvent
from document_engine.adapters.database.models import MigrationItem as MigrationItemModel
from document_engine.adapters.filesystem.temp_storage import TempFileStorage
from document_engine.domain.entities import RepositoryItem
from document_engine.domain.enums import ItemType, MigrationItemState
from document_engine.domain.errors import (
    NAME_COLLISION_UNRESOLVED,
    VALIDATION_SIZE_MISMATCH,
    PermanentError,
    TransientError,
)
from document_engine.domain.state_machine import transition
from document_engine.ports.destination_repository import DestinationRepositoryPort
from document_engine.ports.source_repository import SourceRepositoryPort

GOOGLE_APPS_PREFIX = "application/vnd.google-apps"
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

_EXPORT_TARGET_MIME_BY_EXTENSION = {
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _is_native_export(mime_type: str | None) -> bool:
    return bool(mime_type) and mime_type.startswith(GOOGLE_APPS_PREFIX) and mime_type not in (
        GOOGLE_FOLDER_MIME,
        GOOGLE_SHORTCUT_MIME,
    )


def _temp_remote_name(migration_item_id: str, destination_name: str) -> str:
    return f".{destination_name}.partial.{migration_item_id}"


class Builder:
    """Transfiere un `MigrationItem` de forma incremental (sección 4.5):
    crea carpetas, descarga/exporta a un temporal local, calcula SHA-256,
    sube con nombre temporal, valida, renombra atómicamente y marca
    completado. Nunca sobrescribe silenciosamente un archivo existente."""

    def __init__(
        self,
        db: Session,
        source: SourceRepositoryPort,
        destination: DestinationRepositoryPort,
        temp_storage: TempFileStorage,
    ):
        self._db = db
        self._source = source
        self._destination = destination
        self._temp_storage = temp_storage

    def process_item(self, migration_item_id: str) -> MigrationItemModel:
        item = self._db.get(MigrationItemModel, migration_item_id)
        if item is None:
            raise PermanentError(f"Elemento {migration_item_id} no existe")

        if item.state == MigrationItemState.COMPLETED.value:
            return item  # idempotente: nunca se repite un elemento completado

        if item.state not in (MigrationItemState.READY.value, MigrationItemState.RETRY_PENDING.value):
            raise PermanentError(
                f"El elemento debe estar READY o RETRY_PENDING para procesarse (estado actual: {item.state})",
                code="INVALID_STATE_TRANSITION",
            )

        is_folder = item.item_type == ItemType.FOLDER.value
        try:
            self._create_directories(item)
            if is_folder:
                self._complete_folder(item)
            else:
                local_path = self._download(item)
                self._upload_temp(item, local_path)
                self._validate_and_finalize(item)
        except TransientError as exc:
            self._fail(item, exc, terminal_state=MigrationItemState.RETRY_PENDING)
            return item
        except PermanentError as exc:
            self._fail(item, exc, terminal_state=MigrationItemState.FAILED)
            return item

        if not is_folder:
            self._temp_storage.remove(item.id)
        item.lease_owner = None
        item.lease_expires_at = None
        self._db.commit()
        return item

    # ---- pasos -----------------------------------------------------------

    def _journal(
        self, item: MigrationItemModel, *, event_type: str, operation: str, result: str, error_code: str | None = None
    ) -> None:
        self._db.add(
            JournalEvent(
                batch_id=item.batch_id,
                migration_item_id=item.id,
                event_type=event_type,
                previous_state=item.state,
                new_state=item.state,
                operation=operation,
                result=result,
                attempt_number=item.attempt_count,
                error_code=error_code,
                final_name=item.planned_destination_name,
            )
        )

    def _fail(
        self, item: MigrationItemModel, exc: Exception, *, terminal_state: MigrationItemState
    ) -> None:
        item.last_error_code = getattr(exc, "code", None)
        item.last_error_message = str(exc)
        item.attempt_count += 1
        item.state = transition(MigrationItemState(item.state), terminal_state).value
        item.lease_owner = None
        item.lease_expires_at = None
        self._journal(item, event_type="ITEM_FAILED", operation="TRANSFER", result="ERROR", error_code=item.last_error_code)
        self._db.commit()

    def _dest_parent(self, item: MigrationItemModel) -> str:
        if item.planned_destination_path and "/" in item.planned_destination_path:
            return item.planned_destination_path.rsplit("/", 1)[0]
        return ""

    def _create_directories(self, item: MigrationItemModel) -> None:
        item.state = transition(MigrationItemState(item.state), MigrationItemState.CREATING_DIRECTORIES).value

        if item.item_type == ItemType.FOLDER.value:
            self._destination.ensure_directory(item.planned_destination_path or "")
        else:
            parent = self._dest_parent(item)
            if parent:
                self._destination.ensure_directory(parent)

        self._journal(item, event_type="ITEM_STATE_CHANGED", operation="CREATE_FOLDER", result="OK")

    def _complete_folder(self, item: MigrationItemModel) -> None:
        """Una carpeta no se descarga ni se sube: crear el directorio ya
        cumple la acción, así que avanza directo a COMPLETED."""
        item.state = transition(MigrationItemState.CREATING_DIRECTORIES, MigrationItemState.DOWNLOADED).value
        item.state = transition(MigrationItemState.DOWNLOADED, MigrationItemState.UPLOADING).value
        item.state = transition(MigrationItemState.UPLOADING, MigrationItemState.UPLOADED_TEMP).value
        item.state = transition(MigrationItemState.UPLOADED_TEMP, MigrationItemState.VALIDATING).value
        item.state = transition(MigrationItemState.VALIDATING, MigrationItemState.COMPLETED).value
        item.completed_at = datetime.now(timezone.utc)
        self._journal(item, event_type="ITEM_COMPLETED", operation="CREATE_FOLDER", result="OK")

    def _download(self, item: MigrationItemModel):
        item.state = transition(MigrationItemState.CREATING_DIRECTORIES, MigrationItemState.DOWNLOADING).value

        source_item = RepositoryItem(
            source_item_id=item.source_item_id,
            parent_id=None,
            name=item.source_name,
            item_type=ItemType(item.item_type),
            mime_type=item.source_mime_type,
            size=item.source_size,
            created_time=None,
            modified_time=None,
            checksum=None,
            trashed=False,
            can_download=True,
            logical_path=item.source_path,
        )

        is_export = _is_native_export(item.source_mime_type)
        existing_size = self._temp_storage.current_size(item.id)

        if (
            existing_size
            and item.downloaded_bytes
            and existing_size == item.downloaded_bytes
            and item.source_size
            and existing_size == item.source_size
        ):
            # El temporal local ya está completo (p.ej. tras una caída antes
            # de la carga): se reutiliza sin volver a descargar (sección 9.2).
            local_path = self._temp_storage.stable_path(item.id)
            total_bytes = existing_size
            sha256 = item.local_sha256 or self._temp_storage.compute_sha256(item.id)
        elif (
            not is_export
            and existing_size
            and item.downloaded_bytes
            and existing_size == item.downloaded_bytes
            and item.source_size
            and existing_size < item.source_size
        ):
            # Descarga parcial: se reanuda por rango de bytes desde donde quedó.
            stream = self._source.open_download_stream(source_item, offset=existing_size)
            local_path, total_bytes, sha256 = self._temp_storage.append_stream(item.id, stream)
        else:
            if is_export:
                target_mime = _EXPORT_TARGET_MIME_BY_EXTENSION.get(item.extension or "", "application/pdf")
                stream = self._source.export(source_item, target_mime)
            else:
                stream = self._source.open_download_stream(source_item)
            local_path, total_bytes, sha256 = self._temp_storage.write_stream(item.id, stream)

        item.local_temp_path = str(local_path)
        item.downloaded_bytes = total_bytes
        item.local_sha256 = sha256
        item.state = transition(MigrationItemState.DOWNLOADING, MigrationItemState.DOWNLOADED).value
        self._journal(item, event_type="ITEM_STATE_CHANGED", operation="DOWNLOAD", result="OK")
        return local_path

    def _upload_temp(self, item: MigrationItemModel, local_path) -> None:
        item.state = transition(MigrationItemState.DOWNLOADED, MigrationItemState.UPLOADING).value

        temp_path = self._temp_remote_path(item)
        resume_offset = 0
        if self._destination.supports_resume():
            remote_partial_size = self._destination.get_size(temp_path)
            if remote_partial_size and remote_partial_size < item.downloaded_bytes:
                resume_offset = remote_partial_size

        uploaded_size = self._destination.upload(str(local_path), temp_path, resume_offset=resume_offset)
        item.uploaded_bytes = uploaded_size
        item.state = transition(MigrationItemState.UPLOADING, MigrationItemState.UPLOADED_TEMP).value
        self._journal(item, event_type="ITEM_STATE_CHANGED", operation="UPLOAD", result="OK")

    def _temp_remote_path(self, item: MigrationItemModel) -> str:
        parent = self._dest_parent(item)
        temp_name = _temp_remote_name(item.id, item.planned_destination_name or item.id)
        return f"{parent}/{temp_name}" if parent else temp_name

    def _validate_and_finalize(self, item: MigrationItemModel) -> None:
        item.state = transition(MigrationItemState.UPLOADED_TEMP, MigrationItemState.VALIDATING).value

        temp_path = self._temp_remote_path(item)
        final_path = item.planned_destination_path

        remote_size = self._destination.get_size(temp_path)
        if remote_size != item.downloaded_bytes:
            raise PermanentError(
                f"Tamaño remoto ({remote_size}) no coincide con el descargado ({item.downloaded_bytes})",
                code=VALIDATION_SIZE_MISMATCH,
            )

        if self._destination.exists(final_path):
            raise PermanentError(
                f"El destino ya existe, no se sobrescribe: {final_path}",
                code=NAME_COLLISION_UNRESOLVED,
            )

        self._destination.rename(temp_path, final_path)
        item.remote_size = remote_size
        item.completed_at = datetime.now(timezone.utc)
        item.state = transition(MigrationItemState.VALIDATING, MigrationItemState.COMPLETED).value
        self._journal(item, event_type="ITEM_COMPLETED", operation="RENAME", result="OK")
