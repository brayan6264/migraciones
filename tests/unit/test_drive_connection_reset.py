"""Verifica que `GoogleDriveRepository._reset_connection` recicla el pool
de conexiones TCP de httplib2 sin romperse, sin importar cómo esté
envuelto el cliente (API key = httplib2.Http directo; cuenta de servicio =
AuthorizedHttp que envuelve el httplib2.Http). Es la defensa de raíz contra
descargas colgadas por reuso de una conexión medio-muerta."""
from __future__ import annotations

from document_engine.adapters.google_drive.drive_repository import GoogleDriveRepository


class _FakeConn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeHttplib2Http:
    def __init__(self) -> None:
        self.connections = {"https:drive.googleapis.com": _FakeConn(), "https:oauth2": _FakeConn()}


class _FakeAuthorizedHttp:
    """Emula google_auth_httplib2.AuthorizedHttp: envuelve el httplib2.Http
    real en `.http`."""

    def __init__(self, inner: _FakeHttplib2Http) -> None:
        self.http = inner


class _FakeClient:
    def __init__(self, http) -> None:
        self._http = http


def test_reset_connection_closes_and_clears_pool_for_service_account_client():
    inner = _FakeHttplib2Http()
    conns = list(inner.connections.values())
    client = _FakeClient(_FakeAuthorizedHttp(inner))
    repo = GoogleDriveRepository(client)

    repo._reset_connection()

    assert all(c.closed for c in conns)
    assert inner.connections == {}


def test_reset_connection_closes_and_clears_pool_for_api_key_client():
    # Con API key, `_http` ES el httplib2.Http (sin envoltura AuthorizedHttp).
    inner = _FakeHttplib2Http()
    conns = list(inner.connections.values())
    client = _FakeClient(inner)
    repo = GoogleDriveRepository(client)

    repo._reset_connection()

    assert all(c.closed for c in conns)
    assert inner.connections == {}


def test_reset_connection_is_safe_when_pool_absent_or_empty():
    # No debe explotar si la estructura no es la esperada o el pool está vacío.
    repo = GoogleDriveRepository(_FakeClient(object()))
    repo._reset_connection()  # no lanza

    empty = _FakeHttplib2Http()
    empty.connections = {}
    repo2 = GoogleDriveRepository(_FakeClient(empty))
    repo2._reset_connection()  # no lanza
