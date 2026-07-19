from __future__ import annotations

from abc import ABC, abstractmethod


class DestinationRepositoryPort(ABC):
    """Puerto hacia el repositorio de destino (FTP/FTPS u otro protocolo con
    la misma interfaz, p.ej. un futuro adaptador SFTP)."""

    @abstractmethod
    def ensure_directory(self, path: str) -> None:
        """Crea el directorio y sus ancestros de forma idempotente."""

    @abstractmethod
    def exists(self, path: str) -> bool:
        """True si existe un archivo o directorio en esa ruta."""

    @abstractmethod
    def get_size(self, path: str) -> int | None:
        """Tamaño remoto en bytes, o None si no existe."""

    @abstractmethod
    def upload(self, local_path: str, remote_path: str, *, resume_offset: int = 0) -> int:
        """Sube el archivo local por chunks. Si `resume_offset` > 0 y el
        servidor soporta `REST`, continúa desde ese punto. Devuelve el
        tamaño remoto final en bytes."""

    @abstractmethod
    def rename(self, old_path: str, new_path: str) -> None:
        """Renombra atómicamente (RNFR/RNTO en FTP)."""

    @abstractmethod
    def delete(self, path: str) -> None:
        """Elimina una ruta. Idempotente: no falla si ya no existe."""

    @abstractmethod
    def supports_resume(self) -> bool:
        """True si el servidor soporta reanudar cargas mediante REST."""

    @abstractmethod
    def list_dir(self, path: str) -> list[str]:
        """Lista los nombres reales devueltos por el servidor bajo `path`."""

    @abstractmethod
    def download_to(self, remote_path: str, local_path: str) -> None:
        """Descarga el archivo remoto a `local_path`. Solo se usa para
        validación STRICT (sección 10), nunca para ejecutar el contenido."""

    def get_checksum(self, path: str) -> str | None:
        """Hash remoto si el servidor expone una extensión compatible
        (p.ej. `XCRC`/`XSHA256`). None si no está disponible; en ese caso
        la validación STRONG simplemente no compara hash."""
        return None
