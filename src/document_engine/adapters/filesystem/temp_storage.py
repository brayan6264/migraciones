from __future__ import annotations

import hashlib
from pathlib import Path
from typing import IO


class TempFileStorage:
    """Administra rutas temporales locales estables por `migration_item_id`
    (sección 4.5, punto 3) y calcula SHA-256 mientras se descarga."""

    def __init__(self, base_dir: str | Path):
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def stable_path(self, migration_item_id: str) -> Path:
        return self._base_dir / f"{migration_item_id}.tmp"

    def exists(self, migration_item_id: str) -> bool:
        return self.stable_path(migration_item_id).exists()

    def current_size(self, migration_item_id: str) -> int:
        path = self.stable_path(migration_item_id)
        return path.stat().st_size if path.exists() else 0

    def write_stream(
        self, migration_item_id: str, stream: IO[bytes], *, chunk_size: int = 8 * 1024 * 1024
    ) -> tuple[Path, int, str]:
        """Escribe `stream` en la ruta temporal estable, calculando SHA-256
        en el mismo paso. Devuelve (ruta, bytes_escritos, sha256_hex)."""
        path = self.stable_path(migration_item_id)
        digest = hashlib.sha256()
        total = 0
        with open(path, "wb") as handle:
            while True:
                chunk = stream.read(chunk_size)
                if not chunk:
                    break
                handle.write(chunk)
                digest.update(chunk)
                total += len(chunk)
        return path, total, digest.hexdigest()

    def append_stream(
        self, migration_item_id: str, stream: IO[bytes], *, chunk_size: int = 8 * 1024 * 1024
    ) -> tuple[Path, int, str]:
        """Continúa escribiendo `stream` al final del temporal existente
        (reanudación de descarga por rangos, sección 9.2). El SHA-256 final
        se recalcula sobre el archivo completo. Devuelve (ruta,
        bytes_totales, sha256_hex)."""
        path = self.stable_path(migration_item_id)
        with open(path, "ab") as handle:
            while True:
                chunk = stream.read(chunk_size)
                if not chunk:
                    break
                handle.write(chunk)
        return path, path.stat().st_size, self.compute_sha256(migration_item_id)

    def compute_sha256(self, migration_item_id: str, *, chunk_size: int = 8 * 1024 * 1024) -> str:
        path = self.stable_path(migration_item_id)
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def remove(self, migration_item_id: str) -> None:
        """Elimina el temporal local. Idempotente: no falla si no existe."""
        path = self.stable_path(migration_item_id)
        if path.exists():
            path.unlink()
