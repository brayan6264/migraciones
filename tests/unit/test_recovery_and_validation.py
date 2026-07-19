import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from document_engine.adapters.database.models import Base
from document_engine.adapters.database.models import MigrationBatch as MigrationBatchModel
from document_engine.adapters.database.models import MigrationItem as MigrationItemModel
from document_engine.adapters.filesystem.temp_storage import TempFileStorage
from document_engine.application.migration_service import Builder
from document_engine.application.recovery_service import RecoveryService
from document_engine.application.validation_service import ValidationService, generate_batch_report
from document_engine.domain.enums import MigrationItemState, RenameMethod, ValidationLevel
from document_engine.worker.lease_manager import claim_next_item, heartbeat, release_lease
from tests.unit.fakes import FakeDestinationRepository, FakeSourceRepository


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def make_batch(db) -> MigrationBatchModel:
    batch = MigrationBatchModel(snapshot_id="snap-1", name="ola", priority=50, status="RUNNING")
    db.add(batch)
    db.commit()
    return batch


def make_item(
    db,
    batch_id: str,
    *,
    state: str = MigrationItemState.READY.value,
    item_type: str = "FILE",
    source_item_id: str = "file-1",
    source_size: int | None = 11,
    downloaded_bytes: int = 0,
    planned_destination_path: str = "ROOT/CARPETA_A/REPORTE.pdf",
    planned_destination_name: str = "REPORTE.pdf",
    extension: str | None = "pdf",
    priority: int = 50,
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
    local_sha256: str | None = None,
    rename_method: str = RenameMethod.RULE_BASED.value,
    uploaded_bytes: int = 0,
) -> MigrationItemModel:
    item = MigrationItemModel(
        batch_id=batch_id,
        source_item_id=source_item_id,
        source_path="ROOT/Carpeta A/Reporte.pdf",
        source_name="Reporte.pdf",
        source_mime_type="application/pdf",
        source_size=source_size,
        item_type=item_type,
        priority=priority,
        planned_destination_path=planned_destination_path,
        planned_destination_name=planned_destination_name,
        extension=extension,
        rename_method=rename_method,
        state=state,
        downloaded_bytes=downloaded_bytes,
        uploaded_bytes=uploaded_bytes,
        local_sha256=local_sha256,
        lease_owner=lease_owner,
        lease_expires_at=lease_expires_at,
        idempotency_key=str(uuid.uuid4()),
    )
    db.add(item)
    db.commit()
    return item


# --- lease_manager -----------------------------------------------------------


def test_claim_next_item_picks_highest_priority():
    db = make_session()
    batch = make_batch(db)
    make_item(db, batch.id, source_item_id="low", priority=25, planned_destination_path="ROOT/A.pdf")
    high = make_item(db, batch.id, source_item_id="high", priority=100, planned_destination_path="ROOT/B.pdf")

    claimed = claim_next_item(db, batch.id, worker_owner="worker-1")

    assert claimed.id == high.id
    assert claimed.lease_owner == "worker-1"
    assert claimed.lease_expires_at is not None


def test_claim_next_item_skips_actively_leased_items():
    db = make_session()
    batch = make_batch(db)
    future = datetime.now(timezone.utc) + timedelta(minutes=5)
    make_item(db, batch.id, source_item_id="leased", priority=100, lease_owner="other", lease_expires_at=future)
    free_item = make_item(db, batch.id, source_item_id="free", priority=50)

    claimed = claim_next_item(db, batch.id, worker_owner="worker-2")

    assert claimed.id == free_item.id


def test_claim_next_item_reclaims_expired_lease():
    db = make_session()
    batch = make_batch(db)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    stuck = make_item(db, batch.id, source_item_id="stuck", lease_owner="dead-worker", lease_expires_at=past)

    claimed = claim_next_item(db, batch.id, worker_owner="worker-3")

    assert claimed.id == stuck.id
    assert claimed.lease_owner == "worker-3"


def test_heartbeat_extends_lease_only_for_owner():
    db = make_session()
    batch = make_batch(db)
    item = make_item(db, batch.id)
    claim_next_item(db, batch.id, worker_owner="worker-1", lease_seconds=60)

    heartbeat(db, item.id, worker_owner="someone-else", lease_seconds=999)
    unchanged = db.get(MigrationItemModel, item.id)
    original_expiry = unchanged.lease_expires_at

    heartbeat(db, item.id, worker_owner="worker-1", lease_seconds=999)
    extended = db.get(MigrationItemModel, item.id)

    assert extended.lease_expires_at > original_expiry


def test_release_lease_clears_fields():
    db = make_session()
    batch = make_batch(db)
    item = make_item(db, batch.id)
    claim_next_item(db, batch.id, worker_owner="worker-1")

    release_lease(db, item.id)
    released = db.get(MigrationItemModel, item.id)

    assert released.lease_owner is None
    assert released.lease_expires_at is None


# --- reanudación por bytes en el Builder --------------------------------------


def test_builder_resumes_partial_download_from_local_temp(tmp_path):
    db = make_session()
    batch = make_batch(db)
    content = b"hola mundo!"  # 11 bytes
    item = make_item(
        db,
        batch.id,
        state=MigrationItemState.RETRY_PENDING.value,
        source_size=len(content),
        downloaded_bytes=5,
    )
    storage = TempFileStorage(tmp_path)
    storage.stable_path(item.id).write_bytes(content[:5])

    source = FakeSourceRepository([], contents={"file-1": content})
    destination = FakeDestinationRepository()
    builder = Builder(db, source, destination, storage)

    resolved = builder.process_item(item.id)

    assert resolved.state == MigrationItemState.COMPLETED.value
    assert resolved.downloaded_bytes == len(content)
    assert destination._files["ROOT/CARPETA_A/REPORTE.pdf"] == content


def test_builder_reuses_already_complete_local_temp_without_redownloading(tmp_path):
    db = make_session()
    batch = make_batch(db)
    content = b"hola mundo!"
    item = make_item(
        db,
        batch.id,
        state=MigrationItemState.RETRY_PENDING.value,
        source_size=len(content),
        downloaded_bytes=len(content),
    )
    storage = TempFileStorage(tmp_path)
    storage.stable_path(item.id).write_bytes(content)

    class ExplodingSource(FakeSourceRepository):
        def open_download_stream(self, item, *, offset=0):
            raise AssertionError("no debería volver a descargar")

    source = ExplodingSource([], contents={"file-1": content})
    destination = FakeDestinationRepository()
    builder = Builder(db, source, destination, storage)

    resolved = builder.process_item(item.id)

    assert resolved.state == MigrationItemState.COMPLETED.value


def test_builder_resumes_ftp_upload_with_rest(tmp_path):
    db = make_session()
    batch = make_batch(db)
    content = b"hola mundo!"
    item = make_item(db, batch.id, source_size=len(content), downloaded_bytes=0)
    storage = TempFileStorage(tmp_path)

    source = FakeSourceRepository([], contents={"file-1": content})
    destination = FakeDestinationRepository(supports_resume=True)
    temp_remote_path = f"ROOT/CARPETA_A/.REPORTE.pdf.partial.{item.id}"
    destination.ensure_directory("ROOT/CARPETA_A")
    destination._files[temp_remote_path] = content[:4]  # carga parcial previa

    builder = Builder(db, source, destination, storage)
    resolved = builder.process_item(item.id)

    assert resolved.state == MigrationItemState.COMPLETED.value
    assert 4 in destination.resume_offsets_used
    assert destination._files["ROOT/CARPETA_A/REPORTE.pdf"] == content


# --- RecoveryService -----------------------------------------------------------


def test_recovery_marks_completed_when_remote_final_already_matches(tmp_path):
    db = make_session()
    batch = make_batch(db)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    item = make_item(
        db,
        batch.id,
        state=MigrationItemState.UPLOADING.value,
        source_size=11,
        downloaded_bytes=11,
        lease_owner="dead-worker",
        lease_expires_at=past,
    )
    destination = FakeDestinationRepository()
    destination.ensure_directory("ROOT/CARPETA_A")
    destination._files["ROOT/CARPETA_A/REPORTE.pdf"] = b"hola mundo!"

    recovery = RecoveryService(db, destination, TempFileStorage(tmp_path))
    recovered = recovery.recover_batch(batch.id)

    assert len(recovered) == 1
    assert recovered[0].state == MigrationItemState.COMPLETED.value
    assert recovered[0].lease_owner is None


def test_recovery_resumes_from_local_temp_when_present(tmp_path):
    db = make_session()
    batch = make_batch(db)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    item = make_item(
        db,
        batch.id,
        state=MigrationItemState.DOWNLOADING.value,
        source_size=11,
        downloaded_bytes=0,
        lease_owner="dead-worker",
        lease_expires_at=past,
    )
    storage = TempFileStorage(tmp_path)
    storage.stable_path(item.id).write_bytes(b"hola ")  # descarga parcial de 5 bytes
    destination = FakeDestinationRepository()

    recovery = RecoveryService(db, destination, storage)
    recovered = recovery.recover_batch(batch.id)

    assert recovered[0].state == MigrationItemState.RETRY_PENDING.value
    assert recovered[0].downloaded_bytes == 5


def test_recovery_restarts_when_nothing_to_resume(tmp_path):
    db = make_session()
    batch = make_batch(db)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    make_item(
        db,
        batch.id,
        state=MigrationItemState.DOWNLOADING.value,
        lease_owner="dead-worker",
        lease_expires_at=past,
    )
    destination = FakeDestinationRepository()

    recovery = RecoveryService(db, destination, TempFileStorage(tmp_path))
    recovered = recovery.recover_batch(batch.id)

    assert recovered[0].state == MigrationItemState.RETRY_PENDING.value
    assert recovered[0].downloaded_bytes == 0


def test_recovery_ignores_items_with_active_lease(tmp_path):
    db = make_session()
    batch = make_batch(db)
    future = datetime.now(timezone.utc) + timedelta(minutes=5)
    make_item(
        db,
        batch.id,
        state=MigrationItemState.DOWNLOADING.value,
        lease_owner="alive-worker",
        lease_expires_at=future,
    )
    destination = FakeDestinationRepository()

    recovery = RecoveryService(db, destination, TempFileStorage(tmp_path))
    recovered = recovery.recover_batch(batch.id)

    assert recovered == []


def test_recovery_folder_completed_if_destination_dir_exists(tmp_path):
    db = make_session()
    batch = make_batch(db)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    make_item(
        db,
        batch.id,
        state=MigrationItemState.CREATING_DIRECTORIES.value,
        item_type="FOLDER",
        planned_destination_path="ROOT/CARPETA_A",
        planned_destination_name="CARPETA_A",
        extension=None,
        source_size=0,
        lease_owner="dead-worker",
        lease_expires_at=past,
    )
    destination = FakeDestinationRepository()
    destination.ensure_directory("ROOT/CARPETA_A")

    recovery = RecoveryService(db, destination, TempFileStorage(tmp_path))
    recovered = recovery.recover_batch(batch.id)

    assert recovered[0].state == MigrationItemState.COMPLETED.value


# --- ValidationService ---------------------------------------------------------


def test_basic_validation_passes_on_size_match(tmp_path):
    db = make_session()
    batch = make_batch(db)
    item = make_item(db, batch.id, state=MigrationItemState.COMPLETED.value, downloaded_bytes=11)
    destination = FakeDestinationRepository()
    destination.ensure_directory("ROOT/CARPETA_A")
    destination._files["ROOT/CARPETA_A/REPORTE.pdf"] = b"hola mundo!"

    service = ValidationService(db, destination, level=ValidationLevel.BASIC.value)
    outcome = service.validate_item(item.id)

    assert outcome.passed is True


def test_basic_validation_fails_on_size_mismatch(tmp_path):
    db = make_session()
    batch = make_batch(db)
    item = make_item(db, batch.id, state=MigrationItemState.COMPLETED.value, downloaded_bytes=999)
    destination = FakeDestinationRepository()
    destination.ensure_directory("ROOT/CARPETA_A")
    destination._files["ROOT/CARPETA_A/REPORTE.pdf"] = b"hola mundo!"

    service = ValidationService(db, destination, level=ValidationLevel.BASIC.value)
    outcome = service.validate_item(item.id)

    assert outcome.passed is False
    assert outcome.details["error_code"] == "VALIDATION_SIZE_MISMATCH"


def test_strict_validation_redownloads_and_compares_hash(tmp_path):
    import hashlib

    db = make_session()
    batch = make_batch(db)
    content = b"hola mundo!"
    sha256 = hashlib.sha256(content).hexdigest()
    item = make_item(
        db, batch.id, state=MigrationItemState.COMPLETED.value, downloaded_bytes=len(content), local_sha256=sha256
    )
    destination = FakeDestinationRepository()
    destination.ensure_directory("ROOT/CARPETA_A")
    destination._files["ROOT/CARPETA_A/REPORTE.pdf"] = content

    service = ValidationService(db, destination, level=ValidationLevel.STRICT.value, temp_storage=TempFileStorage(tmp_path))
    outcome = service.validate_item(item.id)

    assert outcome.passed is True
    assert outcome.details["redownload_sha256"] == sha256


def test_strict_validation_fails_on_hash_mismatch(tmp_path):
    db = make_session()
    batch = make_batch(db)
    item = make_item(
        db,
        batch.id,
        state=MigrationItemState.COMPLETED.value,
        downloaded_bytes=11,
        local_sha256="a" * 64,
    )
    destination = FakeDestinationRepository()
    destination.ensure_directory("ROOT/CARPETA_A")
    destination._files["ROOT/CARPETA_A/REPORTE.pdf"] = b"hola mundo!"

    service = ValidationService(db, destination, level=ValidationLevel.STRICT.value, temp_storage=TempFileStorage(tmp_path))
    outcome = service.validate_item(item.id)

    assert outcome.passed is False
    assert outcome.details["error_code"] == "VALIDATION_HASH_MISMATCH"


def test_folder_validation_checks_existence(tmp_path):
    db = make_session()
    batch = make_batch(db)
    item = make_item(
        db,
        batch.id,
        state=MigrationItemState.COMPLETED.value,
        item_type="FOLDER",
        planned_destination_path="ROOT/CARPETA_A",
        extension=None,
        source_size=0,
    )
    destination = FakeDestinationRepository()

    service = ValidationService(db, destination)
    outcome = service.validate_item(item.id)
    assert outcome.passed is False

    destination.ensure_directory("ROOT/CARPETA_A")
    outcome2 = service.validate_item(item.id)
    assert outcome2.passed is True


# --- reporte de lote -----------------------------------------------------------


def test_generate_batch_report_counts_by_state_and_method():
    db = make_session()
    batch = make_batch(db)
    make_item(
        db, batch.id, source_item_id="a", state=MigrationItemState.COMPLETED.value,
        rename_method=RenameMethod.RULE_BASED.value, uploaded_bytes=100,
    )
    make_item(
        db, batch.id, source_item_id="b", state=MigrationItemState.COMPLETED.value,
        rename_method=RenameMethod.AI_ASSISTED.value, uploaded_bytes=200,
    )
    make_item(
        db, batch.id, source_item_id="c", state=MigrationItemState.BLOCKED.value,
        rename_method=RenameMethod.UNCHANGED.value,
    )
    make_item(
        db, batch.id, source_item_id="d", state=MigrationItemState.WAITING_REVIEW.value,
        rename_method=RenameMethod.UNCHANGED.value,
    )
    make_item(
        db, batch.id, source_item_id="e", state=MigrationItemState.COMPLETED.value,
        rename_method=RenameMethod.COLLISION_RESOLUTION.value, uploaded_bytes=50,
    )

    started_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    report = generate_batch_report(db, batch.id, started_at=started_at)

    assert report["total_completed"] == 3
    assert report["total_blocked"] == 1
    assert report["pending_review"] == 1
    assert report["bytes_transferred"] == 350
    assert report["renamed_by_rules"] == 1
    assert report["renamed_by_ai"] == 1
    assert report["collisions_resolved"] == 1
    assert report["duration_seconds"] >= 30
