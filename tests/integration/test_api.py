from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from document_engine.adapters.database.models import Base
from document_engine.adapters.filesystem.temp_storage import TempFileStorage
from document_engine.api.dependencies import (
    get_db,
    get_db_session_factory,
    get_destination_repository,
    get_source_repository,
    get_temp_storage,
)
from document_engine.domain.entities import RepositoryItem
from document_engine.domain.enums import ItemType
from document_engine.main import app
from document_engine.settings import get_settings, Settings
from tests.unit.fakes import FakeDestinationRepository, FakeSourceRepository


def _small_tree() -> list[RepositoryItem]:
    now = datetime.now(timezone.utc)
    return [
        RepositoryItem(
            source_item_id="root",
            parent_id=None,
            name="ROOT",
            item_type=ItemType.FOLDER,
            mime_type="application/vnd.google-apps.folder",
            size=None,
            created_time=now,
            modified_time=now,
            checksum=None,
            trashed=False,
            can_download=True,
            logical_path="ROOT",
        ),
        RepositoryItem(
            source_item_id="file-1",
            parent_id="root",
            name="Informe corto.pdf",
            item_type=ItemType.FILE,
            mime_type="application/pdf",
            size=5,
            created_time=now,
            modified_time=now,
            checksum=None,
            trashed=False,
            can_download=True,
            logical_path="ROOT/Informe corto.pdf",
        ),
    ]


def _nested_tree() -> list[RepositoryItem]:
    now = datetime.now(timezone.utc)

    def folder(item_id: str, parent: str | None, name: str, path: str) -> RepositoryItem:
        return RepositoryItem(
            source_item_id=item_id,
            parent_id=parent,
            name=name,
            item_type=ItemType.FOLDER,
            mime_type="application/vnd.google-apps.folder",
            size=None,
            created_time=now,
            modified_time=now,
            checksum=None,
            trashed=False,
            can_download=True,
            logical_path=path,
        )

    def file(item_id: str, parent: str, name: str, path: str) -> RepositoryItem:
        return RepositoryItem(
            source_item_id=item_id,
            parent_id=parent,
            name=name,
            item_type=ItemType.FILE,
            mime_type="text/plain",
            size=5,
            created_time=now,
            modified_time=now,
            checksum=None,
            trashed=False,
            can_download=True,
            logical_path=path,
        )

    return [
        folder("root", None, "ROOT", "ROOT"),
        folder("a", "root", "Carpeta A", "ROOT/Carpeta A"),
        folder("b", "a", "Subcarpeta B", "ROOT/Carpeta A/Subcarpeta B"),
        file("deep-file", "b", "archivo.txt", "ROOT/Carpeta A/Subcarpeta B/archivo.txt"),
        file("sibling-file", "b", "otro.txt", "ROOT/Carpeta A/Subcarpeta B/otro.txt"),
    ]


def test_create_batch_from_selection_keeps_ancestor_folders_without_siblings(tmp_path):
    """Selección visual: el usuario elige un archivo a 3 niveles de
    profundidad. El lote resultante debe traer las carpetas ancestro
    (vacías salvo lo seleccionado) sin rastrear el árbol completo ni
    incluir al hermano no elegido."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    source = FakeSourceRepository(_nested_tree(), contents={"deep-file": b"hola!"})
    destination = FakeDestinationRepository()
    temp_storage = TempFileStorage(tmp_path)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_db_session_factory] = lambda: session_factory
    app.dependency_overrides[get_source_repository] = lambda: source
    app.dependency_overrides[get_destination_repository] = lambda: destination
    app.dependency_overrides[get_temp_storage] = lambda: temp_storage

    try:
        with TestClient(app) as test_client:
            resp = test_client.post(
                "/migration-batches/from-selection",
                json={
                    "name": "seleccion visual",
                    "priority": 100,
                    "selections": [
                        {
                            "id": "deep-file",
                            "name": "archivo.txt",
                            "type": "FILE",
                            "ancestor_chain": [
                                {"id": "root", "name": "ROOT"},
                                {"id": "a", "name": "Carpeta A"},
                                {"id": "b", "name": "Subcarpeta B"},
                            ],
                        }
                    ],
                },
            )
            assert resp.status_code == 200, resp.text
            batch = resp.json()

            snapshot = test_client.get(f"/snapshots/{batch['snapshot_id']}").json()
            assert snapshot["file_count"] == 1
            assert snapshot["folder_count"] == 3  # ROOT, Carpeta A, Subcarpeta B

            assert test_client.post(f"/migration-batches/{batch['id']}/plan").status_code == 200

            preview = test_client.get(f"/migration-batches/{batch['id']}/preview").json()
            assert preview["total_items"] == 4  # 3 carpetas ancestro + el archivo elegido
            paths = {i["source_path"] for i in preview["items"]}
            assert "ROOT/Carpeta A/Subcarpeta B/archivo.txt" in paths
            assert not any("otro.txt" in p for p in paths)  # el hermano no seleccionado no aparece
    finally:
        app.dependency_overrides.clear()


def test_create_batch_from_selection_wraps_in_destination_folder(tmp_path):
    """Con destination_folder_name informado, todo el lote queda anidado
    dentro de esa carpeta destino (sintética, no existe en Drive) en vez de
    migrar directo a la raíz configurada."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    source = FakeSourceRepository(_nested_tree(), contents={"deep-file": b"hola!"})
    destination = FakeDestinationRepository()
    temp_storage = TempFileStorage(tmp_path)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_db_session_factory] = lambda: session_factory
    app.dependency_overrides[get_source_repository] = lambda: source
    app.dependency_overrides[get_destination_repository] = lambda: destination
    app.dependency_overrides[get_temp_storage] = lambda: temp_storage

    try:
        with TestClient(app) as test_client:
            resp = test_client.post(
                "/migration-batches/from-selection",
                json={
                    "name": "con carpeta destino",
                    "priority": 50,
                    "destination_folder_name": "Respaldo Julio",
                    "selections": [
                        {
                            "id": "deep-file",
                            "name": "archivo.txt",
                            "type": "FILE",
                            "ancestor_chain": [
                                {"id": "root", "name": "ROOT"},
                                {"id": "a", "name": "Carpeta A"},
                                {"id": "b", "name": "Subcarpeta B"},
                            ],
                        }
                    ],
                },
            )
            assert resp.status_code == 200, resp.text
            batch = resp.json()

            snapshot = test_client.get(f"/snapshots/{batch['snapshot_id']}").json()
            assert snapshot["folder_count"] == 4  # carpeta destino + ROOT + Carpeta A + Subcarpeta B

            assert test_client.post(f"/migration-batches/{batch['id']}/plan").status_code == 200
            preview = test_client.get(f"/migration-batches/{batch['id']}/preview").json()
            item = next(i for i in preview["items"] if i["source_path"].endswith("archivo.txt"))
            assert item["planned_destination_path"].startswith("RESPALDO_JULIO/")
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def client(tmp_path):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    source = FakeSourceRepository(_small_tree(), contents={"file-1": b"hola!"})
    destination = FakeDestinationRepository()
    temp_storage = TempFileStorage(tmp_path)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_db_session_factory] = lambda: session_factory
    app.dependency_overrides[get_source_repository] = lambda: source
    app.dependency_overrides[get_destination_repository] = lambda: destination
    app.dependency_overrides[get_temp_storage] = lambda: temp_storage

    with TestClient(app) as test_client:
        yield test_client, destination

    app.dependency_overrides.clear()


def test_health_endpoints(client):
    test_client, _ = client
    assert test_client.get("/health/live").status_code == 200
    assert test_client.get("/health/ready").status_code == 200


def test_capabilities_exposes_no_secrets(client):
    test_client, _ = client
    response = test_client.get("/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert "openai_api_key" not in body
    assert "ftp_password" not in body


def test_full_flow_discovery_to_completed(client):
    test_client, destination = client

    snapshot_resp = test_client.post("/discovery-runs", json={"root_folder_id": "root"})
    assert snapshot_resp.status_code == 200, snapshot_resp.text
    snapshot = snapshot_resp.json()
    assert snapshot["file_count"] == 1
    assert snapshot["folder_count"] == 1

    batch_resp = test_client.post(
        "/migration-batches", json={"snapshot_id": snapshot["id"], "name": "ola-api", "priority": 100}
    )
    assert batch_resp.status_code == 200, batch_resp.text
    batch = batch_resp.json()

    selector_resp = test_client.post(
        f"/migration-batches/{batch['id']}/selectors",
        json={"kind": "FOLDER_RECURSIVE", "value": "root", "include": True},
    )
    assert selector_resp.status_code == 200, selector_resp.text

    list_selectors_resp = test_client.get(f"/migration-batches/{batch['id']}/selectors")
    assert list_selectors_resp.status_code == 200
    assert [s["id"] for s in list_selectors_resp.json()] == [selector_resp.json()["id"]]

    plan_resp = test_client.post(f"/migration-batches/{batch['id']}/plan")
    assert plan_resp.status_code == 200, plan_resp.text
    assert plan_resp.json()["plan_version"] == 1

    preview_resp = test_client.get(f"/migration-batches/{batch['id']}/preview")
    assert preview_resp.status_code == 200
    preview = preview_resp.json()
    assert preview["total_items"] == 2  # root + file-1

    start_resp = test_client.post(f"/migration-batches/{batch['id']}/start", params={"max_items": 10})
    assert start_resp.status_code == 200, start_resp.text
    processed = start_resp.json()["processed"]
    assert len(processed) == 2
    assert all(p["state"] == "COMPLETED" for p in processed)

    status_resp = test_client.get(f"/migration-batches/{batch['id']}/status")
    assert status_resp.json()["counts_by_state"] == {"COMPLETED": 2}

    report_resp = test_client.get(f"/migration-batches/{batch['id']}/report")
    assert report_resp.status_code == 200
    assert report_resp.json()["total_completed"] == 2

    events_resp = test_client.get(f"/migration-batches/{batch['id']}/events")
    assert events_resp.status_code == 200
    assert len(events_resp.json()) > 0

    assert destination.exists("ROOT/INF_CORTO.pdf")


def test_run_batch_processes_everything_in_background(client):
    """`/run` no depende de que el cliente siga llamando: procesa todo el
    lote en un BackgroundTask del propio servidor. TestClient ejecuta las
    background tasks de forma síncrona antes de devolver la respuesta, así
    que para cuando el POST retorna, el lote ya debería estar completo."""
    test_client, destination = client

    snapshot = test_client.post("/discovery-runs", json={"root_folder_id": "root"}).json()
    batch = test_client.post(
        "/migration-batches", json={"snapshot_id": snapshot["id"], "name": "bg-run", "priority": 100}
    ).json()
    test_client.post(
        f"/migration-batches/{batch['id']}/selectors",
        json={"kind": "FOLDER_RECURSIVE", "value": "root", "include": True},
    )
    test_client.post(f"/migration-batches/{batch['id']}/plan")

    run_resp = test_client.post(f"/migration-batches/{batch['id']}/run")
    assert run_resp.status_code == 200, run_resp.text
    assert run_resp.json()["status"] == "started"

    status = test_client.get(f"/migration-batches/{batch['id']}/status").json()
    assert status["counts_by_state"] == {"COMPLETED": 2}
    assert status["status"] == "COMPLETED"
    assert status["background_running"] is False
    assert destination.exists("ROOT/INF_CORTO.pdf")

    # Llamarlo de nuevo sobre un lote ya completo no debe reprocesar nada.
    second_run = test_client.post(f"/migration-batches/{batch['id']}/run")
    assert second_run.status_code == 200
    status_after = test_client.get(f"/migration-batches/{batch['id']}/status").json()
    assert status_after["counts_by_state"] == {"COMPLETED": 2}


def test_retry_failed_endpoint_requires_existing_batch(client):
    test_client, _ = client
    response = test_client.get("/migration-batches/does-not-exist")
    assert response.status_code == 404


def test_selector_with_invalid_kind_returns_422(client):
    test_client, _ = client
    snapshot_resp = test_client.post("/discovery-runs", json={"root_folder_id": "root"})
    batch_resp = test_client.post(
        "/migration-batches", json={"snapshot_id": snapshot_resp.json()["id"], "name": "x"}
    )
    batch_id = batch_resp.json()["id"]

    response = test_client.post(
        f"/migration-batches/{batch_id}/selectors", json={"kind": "NOT_A_KIND", "value": "root"}
    )
    assert response.status_code == 422


def test_item_skip_from_invalid_state_returns_409(client):
    test_client, _ = client
    snapshot_resp = test_client.post("/discovery-runs", json={"root_folder_id": "root"})
    batch_resp = test_client.post(
        "/migration-batches", json={"snapshot_id": snapshot_resp.json()["id"], "name": "x"}
    )
    batch_id = batch_resp.json()["id"]
    test_client.post(
        f"/migration-batches/{batch_id}/selectors",
        json={"kind": "EXPLICIT_IDS", "value": "file-1"},
    )
    test_client.post(f"/migration-batches/{batch_id}/plan")
    preview = test_client.get(f"/migration-batches/{batch_id}/preview").json()
    item_id = None
    # el item aún está en estado PLANNED, no READY, así que "skip" debe fallar
    events = test_client.get(f"/migration-batches/{batch_id}/events").json()
    item_id = next(e["migration_item_id"] for e in events if e["migration_item_id"])

    response = test_client.post(f"/migration-items/{item_id}/skip")
    assert response.status_code == 409


def test_api_key_required_when_configured(tmp_path):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def override_settings():
        return Settings(internal_api_key="secret-key", database_url="sqlite:///:memory:")

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_db_session_factory] = lambda: session_factory
    app.dependency_overrides[get_settings] = override_settings

    with TestClient(app) as test_client:
        no_key = test_client.get("/capabilities")
        assert no_key.status_code == 401

        with_key = test_client.get("/capabilities", headers={"x-api-key": "secret-key"})
        assert with_key.status_code == 200

    app.dependency_overrides.clear()
