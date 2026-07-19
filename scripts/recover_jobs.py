"""Recuperación al iniciar (sección 9.5): busca elementos con lease vencido
en un lote y decide reanudar, validar, reiniciar o enviar a revisión.

Uso:
    python scripts/recover_jobs.py <batch_id>
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from document_engine.adapters.database.session import get_session_factory
from document_engine.adapters.filesystem.temp_storage import TempFileStorage
from document_engine.adapters.ftp.ftp_repository import FTPRepository
from document_engine.application.recovery_service import RecoveryService
from document_engine.settings import get_settings


def main() -> None:
    if len(sys.argv) != 2:
        print("Uso: python scripts/recover_jobs.py <batch_id>")
        raise SystemExit(1)
    batch_id = sys.argv[1]

    settings = get_settings()
    db = get_session_factory()()
    destination = FTPRepository(
        host=settings.ftp_host,
        port=settings.ftp_port,
        username=settings.ftp_username or "",
        password=settings.ftp_password or "",
        mode=settings.ftp_mode,
        passive=settings.ftp_passive,
        verify_tls=settings.ftp_verify_tls,
        timeout_seconds=settings.ftp_timeout_seconds,
        root_path=settings.ftp_root_path,
    )
    temp_storage = TempFileStorage(settings.temp_dir)

    recovered = RecoveryService(db, destination, temp_storage).recover_batch(batch_id)
    print(f"{len(recovered)} elementos recuperados en el lote {batch_id}")
    for item in recovered:
        print(f"  {item.source_path} -> {item.state}")


if __name__ == "__main__":
    main()
