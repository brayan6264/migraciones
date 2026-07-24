from __future__ import annotations

import httplib2
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import Resource, build
from google.oauth2 import service_account

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

FIELDS = (
    "nextPageToken, files(id, name, parents, mimeType, size, createdTime, "
    "modifiedTime, md5Checksum, trashed, capabilities/canDownload, "
    "shortcutDetails/targetId, shortcutDetails/targetMimeType)"
)

# httplib2 no tiene timeout por defecto: un stall de red (frecuente con
# archivos grandes, p. ej. video) deja la descarga colgada para siempre en
# vez de fallar y dejar que el mecanismo de reintentos/lease actúe.
_DEFAULT_TIMEOUT_SECONDS = 120


def build_drive_client(service_account_file: str, *, timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS) -> Resource:
    credentials = service_account.Credentials.from_service_account_file(
        service_account_file, scopes=SCOPES
    )
    http = AuthorizedHttp(credentials, http=httplib2.Http(timeout=timeout_seconds))
    return build("drive", "v3", http=http, cache_discovery=False)


def build_drive_client_api_key(api_key: str, *, timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS) -> Resource:
    """Cliente de solo lectura autenticado por API key (sin OAuth). Solo
    puede acceder a archivos/carpetas compartidos públicamente ("cualquiera
    con el enlace"); no sirve para contenido restringido a usuarios/cuentas
    específicas."""
    http = httplib2.Http(timeout=timeout_seconds)
    return build("drive", "v3", developerKey=api_key, http=http, cache_discovery=False)
