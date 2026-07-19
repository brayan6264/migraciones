"""Valida los elementos COMPLETED de un lote contra el destino real
(sección 10). Uso:

    python scripts/verify_destination.py <batch_id> [--level BASIC|STRONG|STRICT]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import select

from document_engine.adapters.database.models import MigrationItem as MigrationItemModel
from document_engine.adapters.database.session import get_session_factory
from document_engine.adapters.filesystem.temp_storage import TempFileStorage
from document_engine.adapters.ftp.ftp_repository import FTPRepository
from document_engine.application.validation_service import ValidationService, generate_batch_report
from document_engine.domain.enums import MigrationItemState
from document_engine.settings import get_settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("batch_id")
    parser.add_argument("--level", default=None, choices=["BASIC", "STRONG", "STRICT"])
    args = parser.parse_args()

    settings = get_settings()
    level = args.level or settings.validation_level
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
    service = ValidationService(db, destination, level=level, temp_storage=temp_storage)

    stmt = (
        select(MigrationItemModel)
        .where(MigrationItemModel.batch_id == args.batch_id)
        .where(MigrationItemModel.state == MigrationItemState.COMPLETED.value)
    )
    items = db.execute(stmt).scalars().all()

    failures = 0
    for item in items:
        outcome = service.validate_item(item.id)
        status = "OK" if outcome.passed else "FALLO"
        print(f"[{status}] {item.planned_destination_path}")
        if not outcome.passed:
            failures += 1
            print(f"        detalles: {outcome.details}")

    report = generate_batch_report(db, args.batch_id)
    print("\nReporte del lote:")
    for key, value in report.items():
        print(f"  {key}: {value}")

    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
