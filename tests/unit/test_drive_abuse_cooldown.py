"""Verifica el enfriamiento COMPARTIDO entre workers tras un bloqueo por
abuso de Google (`drive_repository._trip_abuse_cooldown`): el bloqueo es
por IP, no por elemento, así que mientras esté activo NINGUNA llamada a
Drive debe tocar la red — todas deben fallar rápido con `TransientError`
antes de intentar el request, para no seguir renovándolo entre workers en
paralelo."""
from __future__ import annotations

import pytest

from document_engine.adapters.google_drive import drive_repository as drive_repository_module
from document_engine.adapters.google_drive.drive_repository import (
    DRIVE_ABUSE_COOLDOWN,
    GoogleDriveRepository,
    _check_abuse_cooldown,
    _trip_abuse_cooldown,
)
from document_engine.domain.entities import RepositoryItem
from document_engine.domain.enums import ItemType
from document_engine.domain.errors import TransientError


@pytest.fixture(autouse=True)
def _reset_cooldown():
    drive_repository_module._abuse_cooldown_until = 0.0
    drive_repository_module._abuse_last_duration = 0.0
    yield
    drive_repository_module._abuse_cooldown_until = 0.0
    drive_repository_module._abuse_last_duration = 0.0


class _ExplodingClient:
    """Cliente fake que hace fallar la prueba si se le llega a pedir algo
    — el gate debe cortar ANTES de tocar el cliente."""

    def files(self):  # pragma: no cover - no debe llamarse
        raise AssertionError("no debía intentarse la llamada a Drive: el gate de enfriamiento debía cortar antes")


def _sample_item() -> RepositoryItem:
    return RepositoryItem(
        source_item_id="f1",
        parent_id=None,
        name="a.bin",
        item_type=ItemType.FILE,
        mime_type="application/octet-stream",
        size=1,
        created_time=None,
        modified_time=None,
        checksum=None,
        trashed=False,
        can_download=True,
        logical_path="a.bin",
    )


def test_check_abuse_cooldown_raises_transient_while_active():
    _trip_abuse_cooldown()

    with pytest.raises(TransientError) as exc_info:
        _check_abuse_cooldown()

    assert exc_info.value.code == DRIVE_ABUSE_COOLDOWN


def test_check_abuse_cooldown_noop_when_inactive():
    _check_abuse_cooldown()  # no lanza


def test_repository_methods_fail_fast_without_touching_client_during_cooldown():
    _trip_abuse_cooldown()
    repo = GoogleDriveRepository(_ExplodingClient())
    item = _sample_item()

    with pytest.raises(TransientError):
        repo.get_item("f1")
    with pytest.raises(TransientError):
        list(repo.list_children("folder"))
    with pytest.raises(TransientError):
        repo.open_download_stream(item)
    with pytest.raises(TransientError):
        repo.export(item, "application/pdf")


def test_repeated_trip_while_active_escalates_up_to_cap():
    _trip_abuse_cooldown()
    first_duration = drive_repository_module._abuse_last_duration

    _trip_abuse_cooldown()
    second_duration = drive_repository_module._abuse_last_duration

    assert second_duration > first_duration
    assert second_duration <= drive_repository_module._ABUSE_COOLDOWN_MAX_SECONDS
