"""Prueba rápida y aislada de conectividad FTP/FTPS usando las credenciales
del .env. Pensado para correr desde la red real (sin NAT intermedio como el
de un entorno sandbox), donde el servidor pueda alcanzarse directamente.

Uso:
    python scripts/test_ftp_connection.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from document_engine.adapters.ftp.ftp_repository import FTPRepository
from document_engine.settings import get_settings


def main() -> None:
    settings = get_settings()
    print(f"Probando {settings.ftp_mode.upper()} a {settings.ftp_host}:{settings.ftp_port} "
          f"(pasivo={settings.ftp_passive}, verificar_tls={settings.ftp_verify_tls})")

    repo = FTPRepository(
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
    try:
        report = repo.check_connectivity()
        print("\nOK:")
        for key, value in report.items():
            print(f"  {key}: {value}")
    except Exception as exc:  # noqa: BLE001 - diagnóstico manual
        print(f"\nFALLO: {type(exc).__name__}: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
