import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from document_engine.adapters.database.models import Base
from document_engine.application.discovery_service import DiscoveryService
from document_engine.application.planning_service import (
    BatchService,
    NameReviewService,
    PlanningService,
    load_export_formats,
)
from document_engine.domain.enums import MigrationItemState, Priority, RenameMethod, SelectorKind
from document_engine.domain.errors import PermanentError
from document_engine.domain.naming_rules import NamingRulesEngine
from tests.unit.fakes import FakeSourceRepository, build_planning_tree

ABBREVIATIONS = {"INFORME": "INF", "REUNION": "REUN"}
EXPORT_FORMATS = {
    "application/vnd.google-apps.document": {"mime_type": "application/pdf", "extension": "pdf"},
}


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


@pytest.fixture
def snapshot_and_services():
    db = make_session()
    source = FakeSourceRepository(build_planning_tree())
    snapshot = DiscoveryService(source, db).run_full_snapshot("root")
    naming = NamingRulesEngine(ABBREVIATIONS)
    planning = PlanningService(db, naming, export_formats=EXPORT_FORMATS)
    batches = BatchService(db)
    return db, snapshot, batches, planning


def test_folder_selection_includes_all_descendants(snapshot_and_services):
    db, snapshot, batches, planning = snapshot_and_services
    batch = batches.create_batch(snapshot_id=snapshot.id, name="ola-a", priority=Priority.CRITICAL)
    batches.add_selector(batch.id, kind=SelectorKind.FOLDER_RECURSIVE, value="folder-a")

    planning.generate_plan(batch.id)
    preview = planning.preview(batch.id)

    # root (ancestra implícita) + folder-a + 5 hijos (normal, collide-1, collide-2, long, zip)
    assert preview["total_items"] == 7


def test_exclude_selector_removes_item(snapshot_and_services):
    db, snapshot, batches, planning = snapshot_and_services
    batch = batches.create_batch(snapshot_id=snapshot.id, name="ola-b", priority=Priority.NORMAL)
    batches.add_selector(batch.id, kind=SelectorKind.FOLDER_RECURSIVE, value="folder-b")
    batches.add_selector(batch.id, kind=SelectorKind.EXPLICIT_IDS, value="file-excluded", include=False)

    planning.generate_plan(batch.id)
    preview = planning.preview(batch.id)

    paths = {i["source_path"] for i in preview["items"]}
    assert not any("Excluir este.pdf" in p for p in paths)


def test_file_selection_implies_ancestor_folders(snapshot_and_services):
    db, snapshot, batches, planning = snapshot_and_services
    batch = batches.create_batch(snapshot_id=snapshot.id, name="ola-c", priority=Priority.NORMAL)
    batches.add_selector(batch.id, kind=SelectorKind.EXPLICIT_IDS, value="file-normal")

    planning.generate_plan(batch.id)
    preview = planning.preview(batch.id)

    source_ids = {i["source_path"] for i in preview["items"]}
    assert "ROOT/Carpeta A" in source_ids  # carpeta ancestra implícita
    assert "ROOT" in source_ids  # raíz también implícita
    assert preview["total_items"] == 3  # root, folder-a, file-normal


def test_priority_and_dfs_ordering(snapshot_and_services):
    db, snapshot, batches, planning = snapshot_and_services
    batch = batches.create_batch(snapshot_id=snapshot.id, name="ola-d", priority=Priority.NORMAL)
    batches.add_selector(batch.id, kind=SelectorKind.FOLDER_RECURSIVE, value="folder-b", priority=Priority.HIGH)
    batches.add_selector(batch.id, kind=SelectorKind.FOLDER_RECURSIVE, value="folder-a", priority=Priority.LOW)

    batch_obj = batches._db.get(type(batch), batch.id)
    priority_by_id, _ = planning.resolve_selection(batch_obj)
    items_by_id = planning._snapshot_items(snapshot.id)
    order = planning.order_by_priority_dfs(priority_by_id, items_by_id)

    # Todo folder-b (HIGH) antes que folder-a (LOW)
    idx_folder_b_items = [order.index(i) for i in order if items_by_id[i].logical_path.startswith("ROOT/Carpeta B")]
    idx_folder_a_items = [order.index(i) for i in order if items_by_id[i].logical_path.startswith("ROOT/Carpeta A")]
    assert max(idx_folder_b_items) < min(idx_folder_a_items)


def test_collision_is_resolved_and_never_produces_duplicate_paths(snapshot_and_services):
    db, snapshot, batches, planning = snapshot_and_services
    batch = batches.create_batch(snapshot_id=snapshot.id, name="ola-e", priority=Priority.NORMAL)
    batches.add_selector(batch.id, kind=SelectorKind.FOLDER_RECURSIVE, value="folder-a")

    planning.generate_plan(batch.id)
    preview = planning.preview(batch.id)

    dest_paths = [i["planned_destination_path"] for i in preview["items"] if i["planned_destination_path"]]
    assert len(dest_paths) == len(set(dest_paths))
    assert preview["collisions_resolved"] >= 1


def test_zip_file_is_included_by_default(snapshot_and_services):
    db, snapshot, batches, planning = snapshot_and_services
    batch = batches.create_batch(snapshot_id=snapshot.id, name="ola-f", priority=Priority.NORMAL)
    batches.add_selector(batch.id, kind=SelectorKind.EXPLICIT_IDS, value="file-zip")

    planning.generate_plan(batch.id)
    preview = planning.preview(batch.id)

    zip_item = next(i for i in preview["items"] if "Backup.zip" in i["source_path"])
    assert zip_item["state"] != MigrationItemState.BLOCKED.value


def test_zip_file_is_blocked_when_explicitly_configured(snapshot_and_services):
    db, snapshot, batches, _ = snapshot_and_services
    planning_blocking = PlanningService(db, NamingRulesEngine(ABBREVIATIONS), export_formats=EXPORT_FORMATS, block_compressed=True)
    batch = batches.create_batch(snapshot_id=snapshot.id, name="ola-f2", priority=Priority.NORMAL)
    batches.add_selector(batch.id, kind=SelectorKind.EXPLICIT_IDS, value="file-zip")

    planning_blocking.generate_plan(batch.id)
    preview = planning_blocking.preview(batch.id)

    zip_item = next(i for i in preview["items"] if "Backup.zip" in i["source_path"])
    assert zip_item["state"] == MigrationItemState.BLOCKED.value


def test_no_permission_file_is_blocked(snapshot_and_services):
    db, snapshot, batches, planning = snapshot_and_services
    batch = batches.create_batch(snapshot_id=snapshot.id, name="ola-g", priority=Priority.NORMAL)
    batches.add_selector(batch.id, kind=SelectorKind.EXPLICIT_IDS, value="file-no-permission")

    planning.generate_plan(batch.id)
    preview = planning.preview(batch.id)

    item = next(i for i in preview["items"] if "Sin permiso.pdf" in i["source_path"])
    assert item["state"] == MigrationItemState.BLOCKED.value


def test_google_native_doc_is_exported_with_configured_extension(snapshot_and_services):
    db, snapshot, batches, planning = snapshot_and_services
    batch = batches.create_batch(snapshot_id=snapshot.id, name="ola-h", priority=Priority.NORMAL)
    batches.add_selector(batch.id, kind=SelectorKind.EXPLICIT_IDS, value="file-google-doc")

    planning.generate_plan(batch.id)
    preview = planning.preview(batch.id)

    doc_item = next(i for i in preview["items"] if "Acta reunion" in i["source_path"])
    assert doc_item["state"] != MigrationItemState.BLOCKED.value
    assert doc_item["planned_destination_path"].endswith(".pdf")


def test_unsupported_google_mime_is_blocked_with_unsupported_reason(snapshot_and_services):
    db, snapshot, batches, planning = snapshot_and_services
    naming = NamingRulesEngine(ABBREVIATIONS)
    planning_no_formats = PlanningService(db, naming, export_formats={})
    batch = batches.create_batch(snapshot_id=snapshot.id, name="ola-i", priority=Priority.NORMAL)
    batches.add_selector(batch.id, kind=SelectorKind.EXPLICIT_IDS, value="file-google-doc")

    planning_no_formats.generate_plan(batch.id)
    preview = planning_no_formats.preview(batch.id)

    doc_item = next(i for i in preview["items"] if "Acta reunion" in i["source_path"])
    assert doc_item["state"] == MigrationItemState.BLOCKED.value


def test_long_name_marks_waiting_review_for_ai(snapshot_and_services):
    db, snapshot, batches, planning = snapshot_and_services
    batch = batches.create_batch(snapshot_id=snapshot.id, name="ola-j", priority=Priority.NORMAL)
    batches.add_selector(batch.id, kind=SelectorKind.EXPLICIT_IDS, value="file-long")

    planning.generate_plan(batch.id)
    preview = planning.preview(batch.id)

    item = next(i for i in preview["items"] if "extremadamente" in i["source_path"])
    assert item["state"] == MigrationItemState.WAITING_REVIEW.value
    assert item["rename_method"] == RenameMethod.AI_ASSISTED.value


def test_preview_counts_match_generated_items(snapshot_and_services):
    db, snapshot, batches, planning = snapshot_and_services
    batch = batches.create_batch(snapshot_id=snapshot.id, name="ola-k", priority=Priority.NORMAL)
    batches.add_selector(batch.id, kind=SelectorKind.FOLDER_RECURSIVE, value="folder-a")

    planning.generate_plan(batch.id)
    preview = planning.preview(batch.id)

    assert preview["blocked_count"] == 0  # file-zip ya no se bloquea por defecto
    assert preview["needs_review_count"] == 1  # file-long
    assert preview["folders"] == 2  # root (implícita) + folder-a
    assert preview["files"] == 5


def test_destination_base_path_is_used_verbatim_without_normalizing(snapshot_and_services):
    db, snapshot, batches, planning = snapshot_and_services
    batch = batches.create_batch(
        snapshot_id=snapshot.id,
        name="ola-base-path",
        priority=Priority.NORMAL,
        destination_base_path="Clientes/Empresa Ñu 2026",
    )
    batches.add_selector(batch.id, kind=SelectorKind.EXPLICIT_IDS, value="file-zip")

    planning.generate_plan(batch.id)
    preview = planning.preview(batch.id)

    zip_item = next(i for i in preview["items"] if "Backup.zip" in i["source_path"])
    assert zip_item["planned_destination_path"].startswith("Clientes/Empresa Ñu 2026/")


def test_replanning_does_not_duplicate_items(snapshot_and_services):
    db, snapshot, batches, planning = snapshot_and_services
    batch = batches.create_batch(snapshot_id=snapshot.id, name="ola-l", priority=Priority.NORMAL)
    batches.add_selector(batch.id, kind=SelectorKind.FOLDER_RECURSIVE, value="folder-a")

    planning.generate_plan(batch.id)
    first_count = planning.preview(batch.id)["total_items"]
    planning.generate_plan(batch.id)
    second_count = planning.preview(batch.id)["total_items"]

    assert first_count == second_count == 7


def test_manual_override_updates_name_and_leaves_review(snapshot_and_services):
    db, snapshot, batches, planning = snapshot_and_services
    batch = batches.create_batch(snapshot_id=snapshot.id, name="ola-m", priority=Priority.NORMAL)
    batches.add_selector(batch.id, kind=SelectorKind.EXPLICIT_IDS, value="file-long")
    planning.generate_plan(batch.id)

    from document_engine.adapters.database.models import MigrationItem as MigrationItemModel
    from sqlalchemy import select

    item = (
        db.execute(select(MigrationItemModel).where(MigrationItemModel.source_item_id == "file-long"))
        .scalars()
        .one()
    )
    assert item.state == MigrationItemState.WAITING_REVIEW.value

    reviews = NameReviewService(db)
    updated = reviews.override_destination_name(item.id, "DOC_LARGO_MANUAL", changed_by="brayan6264@gmail.com")

    assert updated.state == MigrationItemState.READY.value
    assert updated.rename_method == RenameMethod.MANUAL_OVERRIDE.value
    assert updated.planned_destination_name == "DOC_LARGO_MANUAL.pdf"


def test_manual_override_rejects_invalid_name(snapshot_and_services):
    db, snapshot, batches, planning = snapshot_and_services
    batch = batches.create_batch(snapshot_id=snapshot.id, name="ola-n", priority=Priority.NORMAL)
    batches.add_selector(batch.id, kind=SelectorKind.EXPLICIT_IDS, value="file-long")
    planning.generate_plan(batch.id)

    from document_engine.adapters.database.models import MigrationItem as MigrationItemModel
    from sqlalchemy import select

    item = (
        db.execute(select(MigrationItemModel).where(MigrationItemModel.source_item_id == "file-long"))
        .scalars()
        .one()
    )
    reviews = NameReviewService(db)

    with pytest.raises(PermanentError):
        reviews.override_destination_name(item.id, "nombre con espacios!!", changed_by="x")

    with pytest.raises(PermanentError):
        reviews.override_destination_name(item.id, "A" * 26, changed_by="x")


def test_new_plan_inherits_name_from_prior_batch_in_waiting_review(snapshot_and_services):
    """Un elemento en WAITING_REVIEW en un lote anterior (p.ej. renombrado por IA
    pero pendiente de revisión humana) debe heredarse en el nuevo plan en vez de
    volver a sugerir un renombre."""
    db, snapshot, batches, planning = snapshot_and_services

    # Lote 1: planificar el archivo largo → queda WAITING_REVIEW con nombre truncado
    batch1 = batches.create_batch(snapshot_id=snapshot.id, name="lote-heredar-1")
    batches.add_selector(batch1.id, kind=SelectorKind.EXPLICIT_IDS, value="file-long")
    planning.generate_plan(batch1.id)

    from document_engine.adapters.database.models import MigrationItem as MigrationItemModel
    from sqlalchemy import select

    item1 = db.execute(
        select(MigrationItemModel).where(MigrationItemModel.source_item_id == "file-long")
    ).scalars().one()
    assert item1.state == MigrationItemState.WAITING_REVIEW.value
    name_in_batch1 = item1.planned_destination_name

    # Lote 2: re-planificar el mismo archivo en un lote nuevo
    batch2 = batches.create_batch(snapshot_id=snapshot.id, name="lote-heredar-2")
    batches.add_selector(batch2.id, kind=SelectorKind.EXPLICIT_IDS, value="file-long")
    planning.generate_plan(batch2.id)

    item2 = db.execute(
        select(MigrationItemModel).where(
            MigrationItemModel.source_item_id == "file-long",
            MigrationItemModel.batch_id == batch2.id,
        )
    ).scalars().one()

    assert item2.planned_destination_name == name_in_batch1
    assert item2.rename_method == RenameMethod.INHERITED.value
    assert item2.state == MigrationItemState.PLANNED.value


def test_new_plan_inherits_name_from_prior_batch_in_failed(snapshot_and_services):
    """Un elemento FAILED en un lote anterior ya tenía nombre decidido; el
    nuevo plan debe heredarlo en lugar de recalcularlo."""
    db, snapshot, batches, planning = snapshot_and_services

    batch1 = batches.create_batch(snapshot_id=snapshot.id, name="lote-failed-1")
    batches.add_selector(batch1.id, kind=SelectorKind.EXPLICIT_IDS, value="file-normal")
    planning.generate_plan(batch1.id)

    from document_engine.adapters.database.models import MigrationItem as MigrationItemModel
    from sqlalchemy import select

    item1 = db.execute(
        select(MigrationItemModel).where(MigrationItemModel.source_item_id == "file-normal")
    ).scalars().one()
    item1.state = MigrationItemState.FAILED.value
    db.commit()
    name_in_batch1 = item1.planned_destination_name

    batch2 = batches.create_batch(snapshot_id=snapshot.id, name="lote-failed-2")
    batches.add_selector(batch2.id, kind=SelectorKind.EXPLICIT_IDS, value="file-normal")
    planning.generate_plan(batch2.id)

    item2 = db.execute(
        select(MigrationItemModel).where(
            MigrationItemModel.source_item_id == "file-normal",
            MigrationItemModel.batch_id == batch2.id,
        )
    ).scalars().one()

    assert item2.planned_destination_name == name_in_batch1
    assert item2.rename_method == RenameMethod.INHERITED.value


def test_set_batch_priority_before_start_allowed(snapshot_and_services):
    db, snapshot, batches, planning = snapshot_and_services
    batch = batches.create_batch(snapshot_id=snapshot.id, name="ola-o", priority=Priority.NORMAL)
    updated = batches.set_batch_priority(batch.id, Priority.CRITICAL)
    assert updated.priority == 100


def test_set_batch_priority_blocked_once_running(snapshot_and_services):
    db, snapshot, batches, planning = snapshot_and_services
    batch = batches.create_batch(snapshot_id=snapshot.id, name="ola-p", priority=Priority.NORMAL)
    batch.status = "RUNNING"
    db.commit()

    with pytest.raises(PermanentError):
        batches.set_batch_priority(batch.id, Priority.CRITICAL)


def test_load_export_formats_from_yaml_file():
    formats = load_export_formats("config/export_formats.yml")
    assert formats["application/vnd.google-apps.document"]["extension"] == "pdf"
    assert formats["application/vnd.google-apps.spreadsheet"]["extension"] == "xlsx"
