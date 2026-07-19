from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from document_engine.domain.entities import RepositoryItem


class SourceRepositoryPort(ABC):
    """Puerto de solo lectura hacia el repositorio de origen (p.ej. Google Drive).

    Ninguna implementación puede modificar, mover, borrar ni renombrar
    elementos en el origen.
    """

    @abstractmethod
    def list_children(self, folder_id: str) -> Iterator[RepositoryItem]:
        """Lista los hijos directos de una carpeta, paginando internamente."""

    @abstractmethod
    def get_item(self, item_id: str) -> RepositoryItem:
        """Obtiene los metadatos de un único elemento por su id de origen."""

    @abstractmethod
    def walk(self, root_id: str) -> Iterator[RepositoryItem]:
        """Recorre recursivamente un subárbol a partir de root_id.

        Debe excluir elementos en papelera, evitar ciclos vía accesos
        directos y producir la ruta lógica de cada elemento.
        """

    @abstractmethod
    def open_download_stream(self, item: RepositoryItem, *, offset: int = 0):
        """Abre un flujo de descarga del contenido binario, opcionalmente
        reanudado desde `offset` bytes."""

    @abstractmethod
    def export(self, item: RepositoryItem, target_mime_type: str):
        """Exporta un documento nativo de Google Workspace al mime indicado."""
