from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from document_engine.adapters.database.models import JournalEvent
from document_engine.adapters.database.models import MigrationItem as MigrationItemModel
from document_engine.adapters.database.models import ValidationResult as ValidationResultModel
from document_engine.adapters.filesystem.temp_storage import TempFileStorage
from document_engine.domain.enums import ItemType, MigrationItemState, RenameMethod, ValidationLevel
from document_engine.domain.errors import VALIDATION_HASH_MISMATCH, VALIDATION_SIZE_MISMATCH, PermanentError
from document_engine.ports.destination_repository import DestinationRepositoryPort


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    passed: bool
    level: str
    details: dict = field(default_factory=dict)


class ValidationService:
    """Valida la integridad de un elemento ya completado, con niveles
    configurables BASIC / STRONG / STRICT (sección 10)."""

    def __init__(
        self,
        db: Session,
        destination: DestinationRepositoryPort,
        *,
        level: str = ValidationLevel.BASIC.value,
        temp_storage: TempFileStorage | None = None,
    ):
        self._db = db
        self._destination = destination
        self._level = level
        self._temp_storage = temp_storage

    def validate_item(self, migration_item_id: str) -> ValidationOutcome:
        item = self._db.get(MigrationItemModel, migration_item_id)
        if item is None:
            raise PermanentError(f"Elemento {migration_item_id} no existe")

        if item.item_type == ItemType.FOLDER.value:
            outcome = self._validate_folder(item)
        else:
            outcome = self._validate_file(item)

        self._db.add(
            ValidationResultModel(
                migration_item_id=item.id,
                level=self._level,
                passed=outcome.passed,
                details_json=outcome.details,
            )
        )
        self._db.add(
            JournalEvent(
                batch_id=item.batch_id,
                migration_item_id=item.id,
                event_type="VALIDATION_RESULT",
                previous_state=item.state,
                new_state=item.state,
                operation=f"VALIDATE_{self._level}",
                result="OK" if outcome.passed else "ERROR",
                metadata_json=outcome.details,
            )
        )
        self._db.commit()
        return outcome

    def _validate_folder(self, item: MigrationItemModel) -> ValidationOutcome:
        exists = self._destination.exists(item.planned_destination_path or "")
        return ValidationOutcome(passed=exists, level=self._level, details={"exists": exists})

    def _validate_file(self, item: MigrationItemModel) -> ValidationOutcome:
        details: dict = {}
        final_path = item.planned_destination_path or ""

        expected_size = item.downloaded_bytes or item.source_size
        remote_size = self._destination.get_size(final_path)
        details["expected_size"] = expected_size
        details["remote_size"] = remote_size

        if remote_size is None or remote_size != expected_size:
            details["error_code"] = VALIDATION_SIZE_MISMATCH
            return ValidationOutcome(passed=False, level=self._level, details=details)

        if self._level in (ValidationLevel.STRONG.value, ValidationLevel.STRICT.value):
            remote_checksum = self._destination.get_checksum(final_path)
            details["remote_checksum"] = remote_checksum
            if remote_checksum and item.local_sha256 and remote_checksum.lower() != item.local_sha256.lower():
                details["error_code"] = VALIDATION_HASH_MISMATCH
                return ValidationOutcome(passed=False, level=self._level, details=details)

        if self._level == ValidationLevel.STRICT.value:
            if self._temp_storage is None:
                raise PermanentError("La validación STRICT requiere TempFileStorage para re-descargar")
            verify_id = f"verify-{item.id}-{uuid.uuid4().hex[:8]}"
            verify_path = self._temp_storage.stable_path(verify_id)
            self._destination.download_to(final_path, str(verify_path))
            actual_sha256 = self._temp_storage.compute_sha256(verify_id)
            details["redownload_sha256"] = actual_sha256
            self._temp_storage.remove(verify_id)
            if item.local_sha256 and actual_sha256 != item.local_sha256:
                details["error_code"] = VALIDATION_HASH_MISMATCH
                return ValidationOutcome(passed=False, level=self._level, details=details)

        return ValidationOutcome(passed=True, level=self._level, details=details)


def generate_batch_report(db: Session, batch_id: str, *, started_at: datetime | None = None) -> dict:
    """Reporte final por lote (sección 10): totales, bytes transferidos,
    nombres modificados por regla/IA, pendientes de revisión, colisiones
    resueltas y duración."""
    items = db.execute(select(MigrationItemModel).where(MigrationItemModel.batch_id == batch_id)).scalars().all()

    total_completed = sum(1 for i in items if i.state == MigrationItemState.COMPLETED.value)
    total_skipped = sum(1 for i in items if i.state == MigrationItemState.SKIPPED.value)
    total_blocked = sum(1 for i in items if i.state == MigrationItemState.BLOCKED.value)
    total_failed = sum(1 for i in items if i.state == MigrationItemState.FAILED.value)
    total_pending_review = sum(1 for i in items if i.state == MigrationItemState.WAITING_REVIEW.value)
    bytes_transferred = sum(i.uploaded_bytes or 0 for i in items if i.state == MigrationItemState.COMPLETED.value)
    renamed_by_rules = sum(1 for i in items if i.rename_method == RenameMethod.RULE_BASED.value)
    renamed_by_ai = sum(1 for i in items if i.rename_method == RenameMethod.AI_ASSISTED.value)
    collisions_resolved = sum(1 for i in items if i.rename_method == RenameMethod.COLLISION_RESOLUTION.value)

    finished_at = datetime.now(timezone.utc)
    duration_seconds = (finished_at - started_at).total_seconds() if started_at else None

    return {
        "batch_id": batch_id,
        "total_discovered": len(items),
        "total_selected": len(items),
        "total_completed": total_completed,
        "total_skipped": total_skipped,
        "total_blocked": total_blocked,
        "total_failed": total_failed,
        "bytes_transferred": bytes_transferred,
        "renamed_by_rules": renamed_by_rules,
        "renamed_by_ai": renamed_by_ai,
        "pending_review": total_pending_review,
        "collisions_resolved": collisions_resolved,
        "duration_seconds": duration_seconds,
    }
