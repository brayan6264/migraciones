import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from document_engine.adapters.database.models import Base
from document_engine.adapters.database.models import MigrationBatch as MigrationBatchModel
from document_engine.adapters.database.models import MigrationItem as MigrationItemModel
from document_engine.application.migration_service import Builder
from document_engine.domain.enums import MigrationItemState, RenameMethod
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


def make_ready_item(
    db,
    batch_id: str,
    *,
    source_item_id: str = "file-1",
    source_name: str = "Reporte.pdf",
    source_path: str = "ROOT/Carpeta A/Reporte.pdf",
    item_type: str = "FILE",
    source_mime_type: str = "application/pdf",
    extension: str | None = "pdf",
    planned_destination_path: str = "ROOT/CARPETA_A/REPORTE.pdf",
    planned_destination_name: str = "REPORTE.pdf",
    source_size: int = 11,
) -> MigrationItemModel:
    item = MigrationItemModel(
        batch_id=batch_id,
        source_item_id=source_item_id,
        source_path=source_path,
        source_name=source_name,
        source_mime_type=source_mime_type,
        source_size=source_size,
        item_type=item_type,
        priority=50,
        planned_destination_path=planned_destination_path,
        planned_destination_name=planned_destination_name,
        extension=extension,
        rename_method=RenameMethod.RULE_BASED.value,
        state=MigrationItemState.READY.value,
        idempotency_key=str(uuid.uuid4()),
    )
    db.add(item)
    db.commit()
    return item


@pytest.fixture
def builder_env():
    db = make_session()
    batch = make_batch(db)
    destination = FakeDestinationRepository()
    return db, batch, destination


def test_full_success_cycle_completes_item(builder_env, tmp_path):
    db, batch, destination = builder_env
    item = make_ready_item(db, batch.id)
    source = FakeSourceRepository([], contents={"file-1": b"hola mundo!"})

    from document_engine.adapters.filesystem.temp_storage import TempFileStorage

    builder = Builder(db, source, destination, TempFileStorage(tmp_path))
    resolved = builder.process_item(item.id)

    assert resolved.state == MigrationItemState.COMPLETED.value
    assert resolved.completed_at is not None
    assert resolved.remote_size == len(b"hola mundo!")
    assert destination.exists("ROOT/CARPETA_A/REPORTE.pdf")
    assert not destination.exists(f"ROOT/CARPETA_A/.REPORTE.pdf.partial.{item.id}")


def test_temp_name_pattern_used_during_upload(builder_env, tmp_path):
    db, batch, destination = builder_env
    item = make_ready_item(db, batch.id)
    source = FakeSourceRepository([], contents={"file-1": b"contenido"})

    from document_engine.adapters.filesystem.temp_storage import TempFileStorage

    builder = Builder(db, source, destination, TempFileStorage(tmp_path))
    builder.process_item(item.id)

    assert destination.upload_calls == [f"ROOT/CARPETA_A/.REPORTE.pdf.partial.{item.id}"]


def test_rename_is_atomic_from_temp_to_final(builder_env, tmp_path):
    db, batch, destination = builder_env
    item = make_ready_item(db, batch.id)
    source = FakeSourceRepository([], contents={"file-1": b"contenido"})

    from document_engine.adapters.filesystem.temp_storage import TempFileStorage

    builder = Builder(db, source, destination, TempFileStorage(tmp_path))
    builder.process_item(item.id)

    assert destination.rename_calls == [
        (f"ROOT/CARPETA_A/.REPORTE.pdf.partial.{item.id}", "ROOT/CARPETA_A/REPORTE.pdf")
    ]


def test_local_temp_removed_after_success(builder_env, tmp_path):
    db, batch, destination = builder_env
    item = make_ready_item(db, batch.id)
    source = FakeSourceRepository([], contents={"file-1": b"contenido"})

    from document_engine.adapters.filesystem.temp_storage import TempFileStorage

    storage = TempFileStorage(tmp_path)
    builder = Builder(db, source, destination, storage)
    builder.process_item(item.id)

    assert not storage.exists(item.id)


def test_folder_item_completes_without_download_or_upload(builder_env, tmp_path):
    db, batch, destination = builder_env
    item = make_ready_item(
        db,
        batch.id,
        item_type="FOLDER",
        source_mime_type="application/vnd.google-apps.folder",
        extension=None,
        planned_destination_path="ROOT/CARPETA_A",
        planned_destination_name="CARPETA_A",
        source_size=0,
    )
    source = FakeSourceRepository([])

    from document_engine.adapters.filesystem.temp_storage import TempFileStorage

    builder = Builder(db, source, destination, TempFileStorage(tmp_path))
    resolved = builder.process_item(item.id)

    assert resolved.state == MigrationItemState.COMPLETED.value
    assert destination.exists("ROOT/CARPETA_A")
    assert destination.upload_calls == []


def test_native_google_doc_is_exported_not_downloaded(builder_env, tmp_path):
    db, batch, destination = builder_env
    item = make_ready_item(
        db,
        batch.id,
        source_mime_type="application/vnd.google-apps.document",
        planned_destination_path="ROOT/CARPETA_A/ACTA.pdf",
        planned_destination_name="ACTA.pdf",
    )
    source = FakeSourceRepository([], contents={"file-1": b"pdf exportado"})

    from document_engine.adapters.filesystem.temp_storage import TempFileStorage

    builder = Builder(db, source, destination, TempFileStorage(tmp_path))
    resolved = builder.process_item(item.id)

    assert resolved.state == MigrationItemState.COMPLETED.value
    assert resolved.downloaded_bytes == len(b"pdf exportado")


def test_size_mismatch_fails_item_and_does_not_rename(builder_env, tmp_path):
    db, batch, destination = builder_env
    item = make_ready_item(db, batch.id)
    source = FakeSourceRepository([], contents={"file-1": b"contenido"})

    from document_engine.adapters.filesystem.temp_storage import TempFileStorage

    class TruncatingDestination(FakeDestinationRepository):
        def upload(self, local_path, remote_path, *, resume_offset=0):
            super().upload(local_path, remote_path, resume_offset=resume_offset)
            # simula una carga corrupta/truncada
            truncated = self._files[self._norm(remote_path)][:2]
            self._files[self._norm(remote_path)] = truncated
            return len(truncated)

    bad_destination = TruncatingDestination()
    builder = Builder(db, source, bad_destination, TempFileStorage(tmp_path))
    resolved = builder.process_item(item.id)

    assert resolved.state == MigrationItemState.FAILED.value
    assert resolved.last_error_code == "VALIDATION_SIZE_MISMATCH"
    assert bad_destination.rename_calls == []
    assert not bad_destination.exists("ROOT/CARPETA_A/REPORTE.pdf")


def test_existing_destination_is_never_silently_overwritten(builder_env, tmp_path):
    db, batch, destination = builder_env
    item = make_ready_item(db, batch.id)
    destination.ensure_directory("ROOT/CARPETA_A")
    destination._files["ROOT/CARPETA_A/REPORTE.pdf"] = b"archivo preexistente ajeno"
    source = FakeSourceRepository([], contents={"file-1": b"contenido nuevo"})

    from document_engine.adapters.filesystem.temp_storage import TempFileStorage

    builder = Builder(db, source, destination, TempFileStorage(tmp_path))
    resolved = builder.process_item(item.id)

    assert resolved.state == MigrationItemState.FAILED.value
    assert resolved.last_error_code == "NAME_COLLISION_UNRESOLVED"
    assert destination._files["ROOT/CARPETA_A/REPORTE.pdf"] == b"archivo preexistente ajeno"


def test_already_completed_item_is_noop(builder_env, tmp_path):
    db, batch, destination = builder_env
    item = make_ready_item(db, batch.id)
    item.state = MigrationItemState.COMPLETED.value
    db.commit()
    source = FakeSourceRepository([], contents={"file-1": b"no deberia leerse"})

    from document_engine.adapters.filesystem.temp_storage import TempFileStorage

    builder = Builder(db, source, destination, TempFileStorage(tmp_path))
    resolved = builder.process_item(item.id)

    assert resolved.state == MigrationItemState.COMPLETED.value
    assert destination.upload_calls == []


def test_non_ready_item_raises(builder_env, tmp_path):
    db, batch, destination = builder_env
    item = make_ready_item(db, batch.id)
    item.state = MigrationItemState.DISCOVERED.value
    db.commit()
    source = FakeSourceRepository([], contents={"file-1": b"x"})

    from document_engine.adapters.filesystem.temp_storage import TempFileStorage
    from document_engine.domain.errors import PermanentError

    builder = Builder(db, source, destination, TempFileStorage(tmp_path))
    with pytest.raises(PermanentError):
        builder.process_item(item.id)
