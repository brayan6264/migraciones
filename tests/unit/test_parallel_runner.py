"""Verifica que `run_batch_in_background` procesa varios elementos EN
PARALELO (no de a uno) y que todos terminan `COMPLETED`.

Usa una BD SQLite en archivo (no `:memory:`) para que cada worker abra su
propia conexión y haya concurrencia real, imposible con la conexión única
compartida de `:memory:`+StaticPool."""
from __future__ import annotations

import io
import threading
import time
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from document_engine.adapters.database.models import Base
from document_engine.adapters.database.models import MigrationBatch as MigrationBatchModel
from document_engine.adapters.database.models import MigrationItem as MigrationItemModel
from document_engine.adapters.filesystem.temp_storage import TempFileStorage
from document_engine.application.background_runner import run_batch_in_background
from document_engine.domain.enums import MigrationItemState
from tests.unit.fakes import FakeDestinationRepository


class _ConcurrencyTrackingSource:
    """Origen fake que registra cuántas descargas ocurren a la vez, para
    poder afirmar que de verdad hubo procesamiento en paralelo."""

    def __init__(self, contents: dict[str, bytes]) -> None:
        self._contents = contents
        self._lock = threading.Lock()
        self._active = 0
        self.max_concurrent = 0

    def get_item(self, item_id):  # pragma: no cover - no usado en este flujo
        raise NotImplementedError

    def list_children(self, folder_id):  # pragma: no cover
        raise NotImplementedError

    def walk(self, root_id):  # pragma: no cover
        raise NotImplementedError

    def export(self, item, target_mime_type):  # pragma: no cover
        raise NotImplementedError

    def open_download_stream(self, item, *, offset: int = 0):
        with self._lock:
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
        try:
            time.sleep(0.2)  # simula una descarga que dura, para solapar workers
            return io.BytesIO(self._contents.get(item.source_item_id, b"")[offset:])
        finally:
            with self._lock:
                self._active -= 1


def _make_file_db(tmp_path):
    url = f"sqlite:///{tmp_path / 'parallel.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_batch(session_factory, *, n_items: int) -> str:
    db = session_factory()
    batch = MigrationBatchModel(snapshot_id="snap", name="parallel", priority=50, status="RUNNING")
    db.add(batch)
    db.commit()
    contents = {}
    for i in range(n_items):
        item = MigrationItemModel(
            batch_id=batch.id,
            source_item_id=f"file-{i}",
            source_path=f"ROOT/file_{i}.bin",
            source_name=f"file_{i}.bin",
            source_mime_type="application/octet-stream",
            source_size=5,
            item_type="FILE",
            priority=50,
            planned_destination_path=f"ROOT/FILE_{i}.bin",
            planned_destination_name=f"FILE_{i}.bin",
            extension="bin",
            rename_method="RULE_BASED",
            state=MigrationItemState.READY.value,
            idempotency_key=str(uuid.uuid4()),
        )
        db.add(item)
        contents[f"file-{i}"] = b"datos"
    db.commit()
    batch_id = batch.id
    db.close()
    return batch_id, contents


def test_run_batch_processes_items_in_parallel(tmp_path):
    session_factory = _make_file_db(tmp_path)
    batch_id, contents = _seed_batch(session_factory, n_items=6)

    source = _ConcurrencyTrackingSource(contents)
    destination = FakeDestinationRepository()
    destination_lock = threading.Lock()

    class _ThreadSafeDestination(FakeDestinationRepository):
        """Serializa las mutaciones del fake para que la aserción sea sobre
        el paralelismo del pool, no sobre condiciones de carrera del fake."""

        def ensure_directory(self, path):
            with destination_lock:
                return super().ensure_directory(path)

        def upload(self, local_path, remote_path, *, resume_offset=0):
            with destination_lock:
                return super().upload(local_path, remote_path, resume_offset=resume_offset)

        def rename(self, old_path, new_path):
            with destination_lock:
                return super().rename(old_path, new_path)

    shared_destination = _ThreadSafeDestination()
    temp_storage = TempFileStorage(tmp_path / "tmp")

    run_batch_in_background(
        batch_id,
        session_factory=session_factory,
        source_factory=lambda: source,
        destination_factory=lambda: shared_destination,
        temp_storage=temp_storage,
        worker_concurrency=3,
    )

    db = session_factory()
    states = [
        row[0]
        for row in db.execute(
            MigrationItemModel.__table__.select().with_only_columns(MigrationItemModel.state)
        ).all()
    ]
    batch = db.get(MigrationBatchModel, batch_id)
    db.close()

    assert all(s == MigrationItemState.COMPLETED.value for s in states), states
    assert batch.status == "COMPLETED"
    # La prueba de fuego: hubo más de una descarga simultánea.
    assert source.max_concurrent >= 2, f"esperaba paralelismo, max_concurrent={source.max_concurrent}"


def test_run_batch_single_worker_still_completes(tmp_path):
    session_factory = _make_file_db(tmp_path)
    batch_id, contents = _seed_batch(session_factory, n_items=3)

    source = _ConcurrencyTrackingSource(contents)
    temp_storage = TempFileStorage(tmp_path / "tmp")

    run_batch_in_background(
        batch_id,
        session_factory=session_factory,
        source_factory=lambda: source,
        destination_factory=lambda: FakeDestinationRepository(),
        temp_storage=temp_storage,
        worker_concurrency=1,
    )

    db = session_factory()
    batch = db.get(MigrationBatchModel, batch_id)
    remaining = db.execute(
        MigrationItemModel.__table__.select().where(
            MigrationItemModel.state != MigrationItemState.COMPLETED.value
        )
    ).all()
    db.close()

    assert batch.status == "COMPLETED"
    assert remaining == []
    assert source.max_concurrent == 1
