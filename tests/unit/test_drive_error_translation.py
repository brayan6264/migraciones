"""Verifica cómo `GoogleDriveRepository._translate_error` clasifica un 403
de la API de Drive, en particular el caso reportado en producción: la
página HTML "Sorry... your computer or network may be sending automated
queries" que sirve el front-end de Google (no la API) cuando detecta
demasiadas peticiones desde una misma IP. Antes de este fix se clasificaba
como PermanentError/DRIVE_PERMISSION_DENIED y el elemento quedaba FAILED
para siempre en el primer intento, en vez de reintentarse."""
from __future__ import annotations

from types import SimpleNamespace

from googleapiclient.errors import HttpError

from document_engine.adapters.google_drive.drive_repository import GoogleDriveRepository
from document_engine.domain.errors import DRIVE_PERMISSION_DENIED, PermanentError, TransientError

_SORRY_HTML = (
    b"<html><head><title>Sorry...</title></head><body>"
    b"<h1>We're sorry...</h1><p>... but your computer or network may be "
    b"sending automated queries.</p></body></html>"
)


def _http_error(status: int, content: bytes) -> HttpError:
    return HttpError(SimpleNamespace(status=status, reason="error"), content)


def test_403_with_html_abuse_block_page_is_transient_not_permanent():
    exc = _http_error(403, _SORRY_HTML)

    translated = GoogleDriveRepository._translate_error(exc)

    assert isinstance(translated, TransientError)


def test_403_with_json_rate_limit_reason_is_transient():
    body = b'{"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}'
    exc = _http_error(403, body)

    translated = GoogleDriveRepository._translate_error(exc)

    assert isinstance(translated, TransientError)


def test_403_with_json_real_permission_denied_stays_permanent():
    body = b'{"error": {"errors": [{"reason": "insufficientFilePermissions"}]}}'
    exc = _http_error(403, body)

    translated = GoogleDriveRepository._translate_error(exc)

    assert isinstance(translated, PermanentError)
    assert translated.code == DRIVE_PERMISSION_DENIED


def test_404_stays_permanent_not_found():
    exc = _http_error(404, b'{"error": {"errors": [{"reason": "notFound"}]}}')

    translated = GoogleDriveRepository._translate_error(exc)

    assert isinstance(translated, PermanentError)
