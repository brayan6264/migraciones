from __future__ import annotations

import io
import json
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

# Google Drive devuelve el límite de tasa por usuario como HTTP 403 (no 429),
# distinguible solo por el `reason` machine-readable del cuerpo del error.
# Sin esto, un lote grande dispara el límite y cada archivo queda marcado
# como "permiso denegado" para siempre en vez de reintentarse.
_RATE_LIMIT_REASONS = {"userRateLimitExceeded", "rateLimitExceeded", "quotaExceeded", "dailyLimitExceeded"}

# Reintentos con backoff exponencial que la propia librería de Google ya
# implementa (ver `googleapiclient.http._should_retry_response`), pero que no
# se activan a menos que se pase `num_retries` explícitamente en cada llamada.
_NUM_RETRIES = 5


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
        # Siempre True: una carpeta con `folder_id` conocido (p. ej. el
        # destino resuelto de un shortcut) puede vivir en cualquier Unidad
        # compartida aunque la raíz migrada no lo esté (GOOGLE_SHARED_DRIVE_ID
        # vacío). Sin esto, Drive devuelve una lista vacía de hijos para esa
        # carpeta en vez de un error — silenciosamente pierde contenido.
        kwargs: dict = {"includeItemsFromAllDrives": True, "supportsAllDrives": True}
        if self._shared_drive_id:
            # Acota además el corpus a esa unidad compartida específica,
            # solo relevante cuando la raíz configurada de la migración es
            # una Unidad compartida completa (no solo una carpeta puntual).
            kwargs.update(corpora="drive", driveId=self._shared_drive_id)
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
            shortcut_target_id=(raw.get("shortcutDetails") or {}).get("targetId"),
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
                    .execute(num_retries=_NUM_RETRIES)
                )
            except HttpError as exc:
                raise self._translate_error(exc) from exc
            except OSError as exc:
                raise TransientError(str(exc)) from exc

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
                    # Siempre True (no solo cuando hay GOOGLE_SHARED_DRIVE_ID
                    # configurado): un elemento referenciado por ID puntual
                    # (p. ej. el destino de un shortcut) puede vivir en
                    # cualquier Unidad compartida aunque la raíz migrada esté
                    # en Mi unidad. Sin esto, Drive devuelve 404 — indistingible
                    # de "no existe" — para contenido de Unidades compartidas
                    # aunque el acceso sea válido.
                    supportsAllDrives=True,
                )
                .execute(num_retries=_NUM_RETRIES)
            )
        except HttpError as exc:
            raise self._translate_error(exc) from exc
        except OSError as exc:
            # Timeout o corte de red a mitad de la llamada (frecuente con
            # archivos grandes): tratable como transitorio, para que el
            # mecanismo de reintentos/lease lo recoja en vez de dejar el
            # elemento colgado indefinidamente en estado intermedio.
            raise TransientError(str(exc)) from exc
        return self._to_repository_item(raw, logical_path=raw["name"])

    def _resolve_shortcut(self, item: RepositoryItem, visited: set[str]) -> RepositoryItem | None:
        """Sigue un acceso directo hasta su destino real, conservando el
        nombre y la ubicación del acceso directo (así aparece en el FTP
        donde el usuario lo veía en Drive) pero la identidad/contenido del
        destino. Devuelve `None` si el acceso directo está roto, apunta a
        algo sin permiso, o ya fue visitado — este último caso es lo que
        evita ciclos (p. ej. un acceso directo que apunta a un ancestro
        propio): al no resolverse, el elemento sigue siendo tipo SHORTCUT y
        Planning lo bloquea como ya hacía antes."""
        target_id = item.shortcut_target_id
        if not target_id or target_id in visited:
            return None
        try:
            target = self.get_item(target_id)
        except (PermanentError, TransientError):
            return None
        if target.trashed or target.item_type == ItemType.SHORTCUT:
            return None
        return RepositoryItem(
            source_item_id=target.source_item_id,
            parent_id=item.parent_id,
            name=item.name,
            item_type=target.item_type,
            mime_type=target.mime_type,
            size=target.size,
            created_time=target.created_time,
            modified_time=target.modified_time,
            checksum=target.checksum,
            trashed=target.trashed,
            can_download=target.can_download,
            logical_path=item.logical_path,
        )

    def walk(self, root_id: str) -> Iterator[RepositoryItem]:
        """DFS iterativo del subárbol, evitando ciclos y resolviendo shortcuts
        únicamente dentro del alcance autorizado (root_id)."""
        visited: set[str] = set()
        root = self.get_item(root_id)
        if root.item_type == ItemType.SHORTCUT:
            resolved_root = self._resolve_shortcut(root, visited)
            if resolved_root is not None:
                root = resolved_root
        yield root
        visited.add(root.source_item_id)
        stack: list[tuple[str, str]] = [(root.source_item_id, root.logical_path)]

        while stack:
            folder_id, folder_path = stack.pop()
            for item in self._list_children_raw(folder_id, parent_logical_path=folder_path):
                if item.item_type == ItemType.SHORTCUT:
                    resolved = self._resolve_shortcut(item, visited)
                    if resolved is None:
                        continue
                    item = resolved
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
            request = self._client.files().get_media(fileId=item.source_item_id, supportsAllDrives=True)
            buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(buffer, request, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _, done = downloader.next_chunk(num_retries=_NUM_RETRIES)
            buffer.seek(offset)
            return buffer
        except HttpError as exc:
            raise self._translate_error(exc) from exc
        except OSError as exc:
            # Timeout o corte de red a mitad de la llamada (frecuente con
            # archivos grandes): tratable como transitorio, para que el
            # mecanismo de reintentos/lease lo recoja en vez de dejar el
            # elemento colgado indefinidamente en estado intermedio.
            raise TransientError(str(exc)) from exc

    def export(self, item: RepositoryItem, target_mime_type: str) -> io.BytesIO:
        try:
            request = self._client.files().export_media(
                fileId=item.source_item_id, mimeType=target_mime_type
            )
            buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(buffer, request)
            done = False
            while not done:
                _, done = downloader.next_chunk(num_retries=_NUM_RETRIES)
            buffer.seek(0)
            return buffer
        except HttpError as exc:
            raise self._translate_error(exc) from exc
        except OSError as exc:
            # Timeout o corte de red a mitad de la llamada (frecuente con
            # archivos grandes): tratable como transitorio, para que el
            # mecanismo de reintentos/lease lo recoja en vez de dejar el
            # elemento colgado indefinidamente en estado intermedio.
            raise TransientError(str(exc)) from exc

    @staticmethod
    def _is_rate_limit_error(exc: HttpError) -> bool:
        if not exc.content:
            return False
        try:
            data = json.loads(exc.content.decode("utf-8"))
        except (ValueError, AttributeError):
            return False
        error = data.get("error") if isinstance(data, dict) else None
        if not isinstance(error, dict):
            return False
        if error.get("status") in _RATE_LIMIT_REASONS:
            return True
        return any(
            isinstance(e, dict) and e.get("reason") in _RATE_LIMIT_REASONS
            for e in error.get("errors", [])
        )

    @staticmethod
    def _translate_error(exc: HttpError) -> Exception:
        status = getattr(exc.resp, "status", None)
        if status == 404:
            return PermanentError(str(exc), code=DRIVE_ITEM_NOT_FOUND)
        if status == 403:
            if GoogleDriveRepository._is_rate_limit_error(exc):
                return TransientError(str(exc))
            return PermanentError(str(exc), code=DRIVE_PERMISSION_DENIED)
        if status in (429, 500, 502, 503, 504):
            return TransientError(str(exc))
        return PermanentError(str(exc))
