from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from document_engine.adapters.database.models import Base
from document_engine.application.discovery_service import DiscoveryService
from document_engine.application.search_service import SnapshotSearchService
from document_engine.domain.enums import SnapshotStatus
from tests.unit.fakes import FakeSourceRepository, build_sample_tree


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_full_snapshot_persists_tree_and_excludes_trashed():
    db = make_session()
    source = FakeSourceRepository(build_sample_tree())
    service = DiscoveryService(source, db)

    snapshot = service.run_full_snapshot("root")

    assert snapshot.status == SnapshotStatus.COMPLETED.value
    assert snapshot.folder_count == 2  # root + folder-a
    assert snapshot.file_count == 1  # solo file-1, el trashed se excluye
    assert snapshot.metadata_fingerprint is not None


def test_partial_snapshot_scopes_to_given_folders():
    db = make_session()
    source = FakeSourceRepository(build_sample_tree())
    service = DiscoveryService(source, db)

    snapshot = service.run_partial_snapshot(["folder-a"])

    assert snapshot.scope_description == "partial:folder-a"
    assert snapshot.folder_count == 1
    assert snapshot.file_count == 1


def test_search_by_text_and_path_prefix():
    db = make_session()
    source = FakeSourceRepository(build_sample_tree())
    discovery = DiscoveryService(source, db)
    snapshot = discovery.run_full_snapshot("root")

    search = SnapshotSearchService(db)

    by_text = search.search(snapshot.id, text="Informe")
    assert len(by_text) == 1
    assert by_text[0].source_item_id == "file-1"

    by_path = search.search(snapshot.id, path_prefix="ROOT/Carpeta A")
    assert {i.source_item_id for i in by_path} == {"folder-a", "file-1"}

    by_type = search.search(snapshot.id, item_type="FOLDER")
    assert {i.source_item_id for i in by_type} == {"root", "folder-a"}

    none_found = search.search(snapshot.id, text="no-existe")
    assert none_found == []


def test_second_snapshot_does_not_mutate_first():
    db = make_session()
    source = FakeSourceRepository(build_sample_tree())
    discovery = DiscoveryService(source, db)

    first = discovery.run_full_snapshot("root")
    second = discovery.run_partial_snapshot(["folder-a"])

    assert first.id != second.id
    search = SnapshotSearchService(db)
    assert len(search.search(first.id)) == 3
    assert len(search.search(second.id)) == 2
