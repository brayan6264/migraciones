from __future__ import annotations

import ftplib
import ssl
from pathlib import PurePosixPath

from document_engine.domain.errors import (
    FTP_AUTH_FAILED,
    FTP_WRITE_DENIED,
    PermanentError,
    TransientError,
)
from document_engine.ports.destination_repository import DestinationRepositoryPort

_TRANSIENT_FTP_ERRORS = (ftplib.error_temp, OSError, EOFError, ConnectionError, TimeoutError)


class _NatSafeFTP(ftplib.FTP):
    """FTP con el workaround estándar para servidores detrás de NAT que
    anuncian una IP privada en la respuesta a `PASV` (común en NAS
    domésticos con DDNS). Igual que hacen el Explorador de Windows,
    FileZilla, etc.: se ignora la IP devuelta y se reutiliza el host al que
    ya se estableció la conexión de control."""

    def makepasv(self) -> tuple[str, int]:
        _, port = super().makepasv()
        return self.host, port


class _NatSafeFTPTLS(ftplib.FTP_TLS):
    def makepasv(self) -> tuple[str, int]:
        _, port = super().makepasv()
        return self.host, port


class FTPRepository(DestinationRepositoryPort):
    """Adaptador de destino sobre `ftplib`, con soporte para FTP y FTPS
    explícitos (sección 8). No debe confundirse con SFTP: un servidor SFTP
    real requeriría otro adaptador con esta misma interfaz."""

    def __init__(
        self,
        *,
        host: str,
        port: int = 21,
        username: str,
        password: str,
        mode: str = "ftps",
        passive: bool = True,
        verify_tls: bool = True,
        timeout_seconds: int = 60,
        root_path: str = "/",
        chunk_size_bytes: int = 8 * 1024 * 1024,
    ):
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._mode = mode
        self._passive = passive
        self._verify_tls = verify_tls
        self._timeout = timeout_seconds
        self._root_path = "/" + root_path.strip("/") if root_path.strip("/") else "/"
        self._chunk_size = chunk_size_bytes
        self._conn: ftplib.FTP | None = None
        self._resume_supported: bool | None = None

    # ---- conexión ----------------------------------------------------------

    def _full_path(self, path: str) -> str:
        """Resuelve `path` dentro de la raíz configurada, rechazando
        cualquier intento de salir de ella (path traversal)."""
        relative = PurePosixPath(path.strip("/"))
        if ".." in relative.parts:
            raise PermanentError(f"Ruta inválida (path traversal): {path}", code=FTP_WRITE_DENIED)
        full = PurePosixPath(self._root_path) / relative
        return str(full)

    def connect(self) -> None:
        try:
            if self._mode == "ftps":
                context = ssl.create_default_context()
                if not self._verify_tls:
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                conn: ftplib.FTP = _NatSafeFTPTLS(context=context, timeout=self._timeout)
                conn.connect(self._host, self._port)
                conn.login(self._username, self._password)
                conn.prot_p()
            elif self._mode == "ftp":
                conn = _NatSafeFTP(timeout=self._timeout)
                conn.connect(self._host, self._port)
                conn.login(self._username, self._password)
            else:
                raise PermanentError(f"Modo FTP desconocido: {self._mode}")
            conn.set_pasv(self._passive)
            conn.encoding = "utf-8"
            self._conn = conn
        except ftplib.error_perm as exc:
            raise PermanentError(str(exc), code=FTP_AUTH_FAILED) from exc
        except _TRANSIENT_FTP_ERRORS as exc:
            raise TransientError(str(exc)) from exc

    def disconnect(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.quit()
        except Exception:  # noqa: BLE001 - el cierre nunca debe romper el flujo
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._conn = None

    def _ensure_connected(self) -> ftplib.FTP:
        if self._conn is None:
            self.connect()
            return self._conn  # type: ignore[return-value]
        try:
            self._conn.voidcmd("NOOP")  # keepalive / detecta desconexión
        except Exception:  # noqa: BLE001 - reconexión automática
            self.connect()
        return self._conn  # type: ignore[return-value]

    # ---- operaciones ---------------------------------------------------------

    def ensure_directory(self, path: str) -> None:
        conn = self._ensure_connected()
        full = self._full_path(path)
        current = ""
        for part in [p for p in full.split("/") if p]:
            current += f"/{part}"
            try:
                conn.mkd(current)
            except ftplib.error_perm as exc:
                if "550" in str(exc):  # ya existe
                    continue
                raise PermanentError(str(exc), code=FTP_WRITE_DENIED) from exc

    def exists(self, path: str) -> bool:
        if self.get_size(path) is not None:
            return True
        parent = str(PurePosixPath(self._full_path(path)).parent)
        name = PurePosixPath(self._full_path(path)).name
        return name in self.list_dir(parent)

    def get_size(self, path: str) -> int | None:
        conn = self._ensure_connected()
        full = self._full_path(path)
        try:
            conn.voidcmd("TYPE I")
            return conn.size(full)
        except ftplib.error_perm:
            return None

    def upload(self, local_path: str, remote_path: str, *, resume_offset: int = 0) -> int:
        conn = self._ensure_connected()
        full = self._full_path(remote_path)
        rest = resume_offset if resume_offset and self.supports_resume() else None
        try:
            with open(local_path, "rb") as handle:
                if rest:
                    handle.seek(rest)
                conn.storbinary(f"STOR {full}", handle, blocksize=self._chunk_size, rest=rest)
        except ftplib.error_perm as exc:
            raise PermanentError(str(exc), code=FTP_WRITE_DENIED) from exc
        except _TRANSIENT_FTP_ERRORS as exc:
            raise TransientError(str(exc)) from exc
        return self.get_size(remote_path) or 0

    def rename(self, old_path: str, new_path: str) -> None:
        conn = self._ensure_connected()
        try:
            conn.rename(self._full_path(old_path), self._full_path(new_path))
        except ftplib.error_perm as exc:
            raise PermanentError(str(exc), code=FTP_WRITE_DENIED) from exc
        except _TRANSIENT_FTP_ERRORS as exc:
            raise TransientError(str(exc)) from exc

    def delete(self, path: str) -> None:
        conn = self._ensure_connected()
        try:
            conn.delete(self._full_path(path))
        except ftplib.error_perm:
            pass  # ya no existe: eliminar es idempotente

    def supports_resume(self) -> bool:
        if self._resume_supported is None:
            conn = self._ensure_connected()
            try:
                features = conn.sendcmd("FEAT")
                self._resume_supported = "REST STREAM" in features.upper()
            except Exception:  # noqa: BLE001
                self._resume_supported = False
        return self._resume_supported

    def list_dir(self, path: str) -> list[str]:
        conn = self._ensure_connected()
        full = self._full_path(path)
        try:
            entries = conn.nlst(full)
        except ftplib.error_perm:
            return []
        return [PurePosixPath(e).name for e in entries]

    def download_to(self, remote_path: str, local_path: str) -> None:
        conn = self._ensure_connected()
        full = self._full_path(remote_path)
        try:
            with open(local_path, "wb") as handle:
                conn.retrbinary(f"RETR {full}", handle.write, blocksize=self._chunk_size)
        except ftplib.error_perm as exc:
            raise PermanentError(str(exc), code=FTP_WRITE_DENIED) from exc
        except _TRANSIENT_FTP_ERRORS as exc:
            raise TransientError(str(exc)) from exc

    def check_connectivity(self) -> dict:
        """Prueba de conectividad previa a producción (sección 8): valida
        host/puerto, autenticación, escritura, lectura de tamaño,
        renombramiento y eliminación de un temporal de prueba."""
        report: dict = {"connected": False}
        probe_dir = "_document_engine_connectivity_probe"
        probe_file = f"{probe_dir}/probe.tmp"
        renamed_file = f"{probe_dir}/probe.renamed"
        try:
            self.connect()
            report["connected"] = True
            self.ensure_directory(probe_dir)
            report["can_write"] = True

            import io
            import tempfile

            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp.write(b"document-engine-connectivity-probe")
                tmp_path = tmp.name
            try:
                self.upload(tmp_path, probe_file)
                report["remote_size"] = self.get_size(probe_file)
                self.rename(probe_file, renamed_file)
                report["can_rename"] = True
                self.delete(renamed_file)
                report["can_delete"] = True
                report["supports_resume"] = self.supports_resume()
            finally:
                import os

                os.unlink(tmp_path)
        finally:
            self.disconnect()
        return report
