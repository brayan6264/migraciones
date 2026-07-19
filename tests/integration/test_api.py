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
    app.dependency_overrides[get_settings] = override_settings

    with TestClient(app) as test_client:
        no_key = test_client.get("/capabilities")
        assert no_key.status_code == 401

        with_key = test_client.get("/capabilities", headers={"x-api-key": "secret-key"})
        assert with_key.status_code == 200

    app.dependency_overrides.clear()
