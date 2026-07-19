from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from document_engine.adapters.database.models import JournalEvent
from document_engine.adapters.database.models import MigrationItem as MigrationItemModel
from document_engine.adapters.filesystem.temp_storage import TempFileStorage
from document_engine.domain.enums import ItemType, MigrationItemState
from document_engine.domain.state_machine import transition
from document_engine.ports.destination_repository import DestinationRepositoryPort

IN_FLIGHT_STATES = [
    MigrationItemState.CREATING_DIRECTORIES.value,
    MigrationItemState.DOWNLOADING.value,
    MigrationItemState.DOWNLOADED.value,
    MigrationItemState.UPLOADING.value,
    MigrationItemState.UPLOADED_TEMP.value,
    MigrationItemState.VALIDATING.value,
]


class RecoveryService:
    """Se ejecuta al iniciar la aplicación (sección 9.5): busca elementos
    con lease vencido, inspecciona el temporal local y el destino remoto, y
    decide si el elemento ya se completó, si puede reanudarse, o si debe
    reiniciarse desde cero. Nunca repite un elemento ya `COMPLETED`."""

    def __init__(self, db: Session, destination: DestinationRepositoryPort, temp_storage: TempFileStorage):
        self._db = db
        self._destination = destination
        self._temp_storage = temp_storage

    def recover_batch(self, batch_id: str) -> list[MigrationItemModel]:
        now = datetime.now(timezone.utc)
        stmt = (
            select(MigrationItemModel)
            .where(MigrationItemModel.batch_id == batch_id)
            .where(MigrationItemModel.state.in_(IN_FLIGHT_STATES))
            .where(or_(MigrationItemModel.lease_expires_at.is_(None), MigrationItemModel.lease_expires_at < now))
        )
        stuck_items = self._db.execute(stmt).scalars().all()
        return [self._recover_item(item) for item in stuck_items]

    def _recover_item(self, item: MigrationItemModel) -> MigrationItemModel:
        previous_state = item.state
        item.lease_owner = None
        item.lease_expires_at = None

        if item.item_type == ItemType.FOLDER.value:
            decision, target_state = self._recover_folder(item)
        else:
            decision, target_state = self._recover_file(item)

        item.state = transition(MigrationItemState(item.state), target_state).value
        self._db.add(
            JournalEvent(
                batch_id=item.batch_id,
                migration_item_id=item.id,
                event_type="RECOVERY_DECISION",
                previous_state=previous_state,
                new_state=item.state,
                operation="RECOVER",
                result=decision,
                metadata_json={"decision": decision},
            )
        )
        self._db.commit()
        return item

    def _recover_folder(self, item: MigrationItemModel) -> tuple[str, MigrationItemState]:
        if self._destination.exists(item.planned_destination_path or ""):
            item.completed_at = datetime.now(timezone.utc)
            return "ALREADY_COMPLETED", MigrationItemState.COMPLETED
        return "RESTART", MigrationItemState.RETRY_PENDING

    def _recover_file(self, item: MigrationItemModel) -> tuple[str, MigrationItemState]:
        final_path = item.planned_destination_path or ""
        remote_size = self._destination.get_size(final_path) if final_path else None
        expected_size = item.downloaded_bytes or item.source_size

        if remote_size is not None and expected_size and remote_size == expected_size:
            # El archivo final ya existe con el tamaño esperado: el worker
            # murió después de renombrar pero antes de confirmar en la BD.
            item.remote_size = remote_size
            item.completed_at = datetime.now(timezone.utc)
            return "ALREADY_COMPLETED", MigrationItemState.COMPLETED

        local_size = self._temp_storage.current_size(item.id)
        if local_size > 0:
            # Se conserva el temporal local para reanudar la descarga o,
            # si ya está completo, saltar directo a la carga (sección 9.2).
            item.downloaded_bytes = local_size
            return "RESUME_FROM_LOCAL_TEMP", MigrationItemState.RETRY_PENDING

        item.downloaded_bytes = 0
        item.local_temp_path = None
        item.local_sha256 = None
        return "RESTART", MigrationItemState.RETRY_PENDING
