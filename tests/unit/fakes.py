from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import datetime, timezone

from document_engine.domain.entities import RepositoryItem
from document_engine.domain.enums import ItemType
from document_engine.domain.errors import PermanentError
from document_engine.ports.ai_naming_provider import AINamingProviderPort, AINamingRequest, AINamingResponse
from document_engine.ports.destination_repository import DestinationRepositoryPort
from document_engine.ports.source_repository import SourceRepositoryPort


class FakeAINamingProvider(AINamingProviderPort):
    """Fake del proveedor de IA: reproduce una secuencia programada de
    respuestas o excepciones, una por llamada (la última se repite si se
    agota la lista)."""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[AINamingRequest] = []

    def suggest_name(self, request: AINamingRequest) -> AINamingResponse:
        self.calls.append(request)
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        outcome = self._responses[index]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeSourceRepository(SourceRepositoryPort):
    """Fake en memoria del origen, para pruebas sin llamar a Google Drive."""

    def __init__(self, items: list[RepositoryItem], *, contents: dict[str, bytes] | None = None):
        self._by_id = {item.source_item_id: item for item in items}
        self._children: dict[str | None, list[RepositoryItem]] = {}
        self._contents = contents or {}
        for item in items:
            self._children.setdefault(item.parent_id, []).append(item)

    def list_children(self, folder_id: str) -> Iterator[RepositoryItem]:
        yield from self._children.get(folder_id, [])

    def get_item(self, item_id: str) -> RepositoryItem:
        return self._by_id[item_id]

    def walk(self, root_id: str) -> Iterator[RepositoryItem]:
        root = self._by_id[root_id]
        yield root
        stack = [root_id]
        while stack:
            current = stack.pop()
            for child in self._children.get(current, []):
                yield child
                if child.item_type == ItemType.FOLDER:
                    stack.append(child.source_item_id)

    def open_download_stream(self, item: RepositoryItem, *, offset: int = 0):
        data = self._contents.get(item.source_item_id, b"")
        return io.BytesIO(data[offset:])

    def export(self, item: RepositoryItem, target_mime_type: str):
        data = self._contents.get(item.source_item_id, b"")
        return io.BytesIO(data)


class FakeDestinationRepository(DestinationRepositoryPort):
    """Destino FTP/FTPS en memoria, para probar el Builder sin red."""

    def __init__(self, *, supports_resume: bool = True):
        self._files: dict[str, bytes] = {}
        self._dirs: set[str] = {""}
        self._supports_resume = supports_resume
        self.rename_calls: list[tuple[str, str]] = []
        self.upload_calls: list[str] = []
        self.resume_offsets_used: list[int] = []

    @staticmethod
    def _norm(path: str) -> str:
        return path.strip("/")

    def ensure_directory(self, path: str) -> None:
        current = ""
        for part in [p for p in self._norm(path).split("/") if p]:
            current = f"{current}/{part}" if current else part
            self._dirs.add(current)

    def exists(self, path: str) -> bool:
        p = self._norm(path)
        return p in self._files or p in self._dirs

    def get_size(self, path: str) -> int | None:
        data = self._files.get(self._norm(path))
        return len(data) if data is not None else None

    def upload(self, local_path: str, remote_path: str, *, resume_offset: int = 0) -> int:
        self.upload_calls.append(remote_path)
        self.resume_offsets_used.append(resume_offset)
        key = self._norm(remote_path)
        with open(local_path, "rb") as handle:
            if resume_offset and self._supports_resume and key in self._files:
                handle.seek(resume_offset)
                self._files[key] = self._files[key][:resume_offset] + handle.read()
            else:
                self._files[key] = handle.read()
        return len(self._files[key])

    def rename(self, old_path: str, new_path: str) -> None:
        old, new = self._norm(old_path), self._norm(new_path)
        if old not in self._files:
            raise PermanentError(f"No existe el temporal remoto: {old_path}")
        self._files[new] = self._files.pop(old)
        self.rename_calls.append((old_path, new_path))

    def delete(self, path: str) -> None:
        self._files.pop(self._norm(path), None)

    def supports_resume(self) -> bool:
        return self._supports_resume

    def list_dir(self, path: str) -> list[str]:
        prefix = self._norm(path)
        return [f.rsplit("/", 1)[-1] for f in self._files if f.startswith(prefix)]

    def list_directories(self, path: str) -> list[str]:
        prefix = self._norm(path)
        children = set()
        for d in self._dirs:
            if d == prefix or not d.startswith(f"{prefix}/" if prefix else ""):
                continue
            rest = d[len(prefix) + 1 :] if prefix else d
            children.add(rest.split("/", 1)[0])
        return sorted(children)

    def download_to(self, remote_path: str, local_path: str) -> None:
        data = self._files.get(self._norm(remote_path), b"")
        with open(local_path, "wb") as handle:
            handle.write(data)


def build_sample_tree() -> list[RepositoryItem]:
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
            source_item_id="folder-a",
            parent_id="root",
            name="Carpeta A",
            item_type=ItemType.FOLDER,
            mime_type="application/vnd.google-apps.folder",
            size=None,
            created_time=now,
            modified_time=now,
            checksum=None,
            trashed=False,
            can_download=True,
            logical_path="ROOT/Carpeta A",
        ),
        RepositoryItem(
            source_item_id="file-1",
            parent_id="folder-a",
            name="Informe Bimestral.pdf",
            item_type=ItemType.FILE,
            mime_type="application/pdf",
            size=1024,
            created_time=now,
            modified_time=now,
            checksum="abc123",
            trashed=False,
            can_download=True,
            logical_path="ROOT/Carpeta A/Informe Bimestral.pdf",
        ),
        RepositoryItem(
            source_item_id="file-trashed",
            parent_id="folder-a",
            name="Borrador viejo.docx",
            item_type=ItemType.FILE,
            mime_type="application/vnd.google-apps.document",
            size=512,
            created_time=now,
            modified_time=now,
            checksum=None,
            trashed=True,
            can_download=True,
            logical_path="ROOT/Carpeta A/Borrador viejo.docx",
        ),
    ]


def build_planning_tree() -> list[RepositoryItem]:
    """Árbol más rico para las pruebas de PlanningService (sprint 3):
    incluye colisiones, un nombre largo, un zip y un doc nativo de Google."""
    now = datetime.now(timezone.utc)

    def folder(item_id: str, parent: str | None, path: str) -> RepositoryItem:
        return RepositoryItem(
            source_item_id=item_id,
            parent_id=parent,
            name=path.rsplit("/", 1)[-1],
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

    def file(
        item_id: str,
        parent: str,
        name: str,
        path: str,
        *,
        mime_type: str = "application/pdf",
        size: int = 100,
        can_download: bool = True,
    ) -> RepositoryItem:
        return RepositoryItem(
            source_item_id=item_id,
            parent_id=parent,
            name=name,
            item_type=ItemType.FILE,
            mime_type=mime_type,
            size=size,
            created_time=now,
            modified_time=now,
            checksum=None,
            trashed=False,
            can_download=can_download,
            logical_path=path,
        )

    return [
        folder("root", None, "ROOT"),
        folder("folder-a", "root", "ROOT/Carpeta A"),
        folder("folder-b", "root", "ROOT/Carpeta B"),
        file("file-normal", "folder-a", "Reporte Normal.pdf", "ROOT/Carpeta A/Reporte Normal.pdf"),
        file("file-collide-1", "folder-a", "Reporte!!.pdf", "ROOT/Carpeta A/Reporte!!.pdf"),
        file("file-collide-2", "folder-a", "Reporte??.pdf", "ROOT/Carpeta A/Reporte??.pdf"),
        file(
            "file-long",
            "folder-a",
            "Documento extremadamente descriptivo y largo.pdf",
            "ROOT/Carpeta A/Documento extremadamente descriptivo y largo.pdf",
        ),
        file("file-zip", "folder-a", "Backup.zip", "ROOT/Carpeta A/Backup.zip", mime_type="application/zip"),
        file(
            "file-google-doc",
            "folder-b",
            "Acta reunion",
            "ROOT/Carpeta B/Acta reunion",
            mime_type="application/vnd.google-apps.document",
            size=0,
        ),
        file("file-excluded", "folder-b", "Excluir este.pdf", "ROOT/Carpeta B/Excluir este.pdf"),
        file(
            "file-no-permission",
            "folder-b",
            "Sin permiso.pdf",
            "ROOT/Carpeta B/Sin permiso.pdf",
            can_download=False,
        ),
    ]
