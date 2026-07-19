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
