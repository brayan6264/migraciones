"""Worker independiente: reclama y procesa elementos de un lote en un bucle,
como alternativa al endpoint síncrono POST /migration-batches/{id}/start.

Uso:
    python scripts/run_worker.py <batch_id> [--max-items N] [--lease-seconds N]
"""
from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from document_engine.adapters.database.session import get_session_factory
from document_engine.adapters.filesystem.temp_storage import TempFileStorage
from document_engine.adapters.ftp.ftp_repository import FTPRepository
from document_engine.adapters.google_drive.client import build_drive_client
from document_engine.adapters.google_drive.drive_repository import GoogleDriveRepository
from document_engine.application.migration_service import Builder
from document_engine.settings import get_settings
from document_engine.worker.lease_manager import claim_next_item


def main() -> None:
    parser = argparse.ArgumentParser(description="Worker de migración Document Engine")
    parser.add_argument("batch_id")
    parser.add_argument("--max-items", type=int, default=None, help="Límite de elementos a procesar (por defecto, sin límite)")
    parser.add_argument("--lease-seconds", type=int, default=None)
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Segundos de espera cuando no hay trabajo pendiente")
    args = parser.parse_args()

    settings = get_settings()
    db = get_session_factory()()
    source = GoogleDriveRepository(
        build_drive_client(settings.google_service_account_file), shared_drive_id=settings.google_shared_drive_id
    )
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
        chunk_size_bytes=settings.transfer_chunk_size_mb * 1024 * 1024,
    )
    temp_storage = TempFileStorage(settings.temp_dir)
    builder = Builder(db, source, destination, temp_storage)

    worker_owner = f"worker-{uuid.uuid4().hex[:8]}"
    lease_seconds = args.lease_seconds or settings.worker_lease_seconds
    processed = 0

    print(f"[{worker_owner}] procesando lote {args.batch_id}")
    while args.max_items is None or processed < args.max_items:
        item = claim_next_item(db, args.batch_id, worker_owner=worker_owner, lease_seconds=lease_seconds)
        if item is None:
            print("Sin elementos pendientes, esperando...")
            time.sleep(args.poll_interval)
            continue
        resolved = builder.process_item(item.id)
        processed += 1
        print(f"[{processed}] {resolved.source_path} -> {resolved.state}")


if __name__ == "__main__":
    main()
