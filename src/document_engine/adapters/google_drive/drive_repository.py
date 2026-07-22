from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import datetime

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from document_engine.adapters.google_drive.client import FIELDS
from document_engine.domain.entities import RepositoryItem
from document_engine.domain.enums import ItemType
from document_engine.domain.errors import DRIVE_ITEM_NOT_FOUND, DRIVE_PERMISSION_DENIED, PermanentError, TransientError
from document_engine.ports.source_repository import SourceRepositoryPort

GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_SHORTCUT_MIME = "application/vnd.google-apps.shortcut"


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _item_type(mime_type: str) -> ItemType:
    if mime_type == GOOGLE_FOLDER_MIME:
        return ItemType.FOLDER
    if mime_type == GOOGLE_SHORTCUT_MIME:
        return ItemType.SHORTCUT
    return ItemType.FILE


class GoogleDriveRepository(SourceRepositoryPort):
    """Adaptador de solo lectura sobre la Google Drive API v3."""

    def __init__(self, client, *, shared_drive_id: str | None = None, page_size: int = 200):
        self._client = client
        self._shared_drive_id = shared_drive_id
        self._page_size = page_size

    def _list_kwargs(self) -> dict:
        kwargs: dict = {}
        if self._shared_drive_id:
            kwargs.update(
                corpora="drive",
                driveId=self._shared_drive_id,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            )
        return kwargs

    def _to_repository_item(self, raw: dict, logical_path: str) -> RepositoryItem:
        parents = raw.get("parents") or []
        mime_type = raw.get("mimeType", "")
        capabilities = raw.get("capabilities", {})
        return RepositoryItem(
            source_item_id=raw["id"],
            parent_id=parents[0] if parents else None,
            name=raw["name"],
            item_type=_item_type(mime_type),
            mime_type=mime_type,
            size=int(raw["size"]) if raw.get("size") is not None else None,
            created_time=_parse_time(raw.get("createdTime")),
            modified_time=_parse_time(raw.get("modifiedTime")),
            checksum=raw.get("md5Checksum"),
            trashed=bool(raw.get("trashed", False)),
            can_download=bool(capabilities.get("canDownload", True)),
            logical_path=logical_path,
        )

    def list_children(self, folder_id: str) -> Iterator[RepositoryItem]:
        yield from self._list_children_raw(folder_id, parent_logical_path="")

    def _list_children_raw(self, folder_id: str, *, parent_logical_path: str) -> Iterator[RepositoryItem]:
        page_token = None
        query = f"'{folder_id}' in parents and trashed = false"
        while True:
            try:
                response = (
                    self._client.files()
                    .list(
                        q=query,
                        fields=FIELDS,
                        pageSize=self._page_size,
                        pageToken=page_token,
                        **self._list_kwargs(),
                    )
                    .execute()
                )
            except HttpError as exc:
                raise self._translate_error(exc) from exc

            for raw in response.get("files", []):
                if raw.get("trashed"):
                    continue
                path = f"{parent_logical_path}/{raw['name']}" if parent_logical_path else raw["name"]
                yield self._to_repository_item(raw, path)

            page_token = response.get("nextPageToken")
            if not page_token:
                break

    def get_item(self, item_id: str) -> RepositoryItem:
        try:
            raw = (
                self._client.files()
                .get(
                    fileId=item_id,
                    fields=FIELDS.replace("nextPageToken, files(", "").rstrip(")"),
                    supportsAllDrives=bool(self._shared_drive_id),
                )
                .execute()
            )
        except HttpError as exc:
            raise self._translate_error(exc) from exc
        return self._to_repository_item(raw, logical_path=raw["name"])

    def walk(self, root_id: str) -> Iterator[RepositoryItem]:
        """DFS iterativo del subárbol, evitando ciclos y resolviendo shortcuts
        únicamente dentro del alcance autorizado (root_id)."""
        visited: set[str] = set()
        root = self.get_item(root_id)
        yield root
        visited.add(root.source_item_id)
        stack: list[tuple[str, str]] = [(root_id, root.logical_path)]

        while stack:
            folder_id, folder_path = stack.pop()
            for item in self._list_children_raw(folder_id, parent_logical_path=folder_path):
                if item.source_item_id in visited:
                    continue
                visited.add(item.source_item_id)
                yield item
                if item.item_type == ItemType.FOLDER:
                    stack.append((item.source_item_id, item.logical_path))

    def open_download_stream(self, item: RepositoryItem, *, offset: int = 0) -> io.BytesIO:
        if not item.can_download:
            raise PermanentError(
                f"Sin permiso de descarga para {item.source_item_id}", code=DRIVE_PERMISSION_DENIED
            )
        try:
            request = self._client.files().get_media(fileId=item.source_item_id)
            buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(buffer, request, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            buffer.seek(offset)
            return buffer
        except HttpError as exc:
            raise self._translate_error(exc) from exc

    def export(self, item: RepositoryItem, target_mime_type: str) -> io.BytesIO:
        try:
            request = self._client.files().export_media(
                fileId=item.source_item_id, mimeType=target_mime_type
            )
            buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(buffer, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            buffer.seek(0)
            return buffer
        except HttpError as exc:
            raise self._translate_error(exc) from exc

    @staticmethod
    def _translate_error(exc: HttpError) -> Exception:
        status = getattr(exc.resp, "status", None)
        if status == 404:
            return PermanentError(str(exc), code=DRIVE_ITEM_NOT_FOUND)
        if status == 403:
            return PermanentError(str(exc), code=DRIVE_PERMISSION_DENIED)
        if status in (429, 500, 502, 503, 504):
            return TransientError(str(exc))
        return PermanentError(str(exc))
