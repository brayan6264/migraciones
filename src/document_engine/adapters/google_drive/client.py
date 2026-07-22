from __future__ import annotations

from googleapiclient.discovery import Resource, build
from google.oauth2 import service_account

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

FIELDS = (
    "nextPageToken, files(id, name, parents, mimeType, size, createdTime, "
    "modifiedTime, md5Checksum, trashed, capabilities/canDownload, "
    "shortcutDetails/targetId, shortcutDetails/targetMimeType)"
)


def build_drive_client(service_account_file: str) -> Resource:
    credentials = service_account.Credentials.from_service_account_file(
        service_account_file, scopes=SCOPES
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def build_drive_client_api_key(api_key: str) -> Resource:
    """Cliente de solo lectura autenticado por API key (sin OAuth). Solo
    puede acceder a archivos/carpetas compartidos públicamente ("cualquiera
    con el enlace"); no sirve para contenido restringido a usuarios/cuentas
    específicas."""
    return build("drive", "v3", developerKey=api_key, cache_discovery=False)
