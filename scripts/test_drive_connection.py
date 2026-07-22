"""Prueba rápida de conectividad a Google Drive (service_account o api_key,
según GOOGLE_AUTH_MODE) usando las credenciales del .env: obtiene la carpeta
raíz y lista su contenido inmediato.

Uso:
    python scripts/test_drive_connection.py
"""
from __future__ import annotations

import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from document_engine.adapters.google_drive.client import build_drive_client, build_drive_client_api_key
from document_engine.adapters.google_drive.drive_repository import GoogleDriveRepository
from document_engine.settings import get_settings


def main() -> None:
    settings = get_settings()
    print(f"Probando Google Drive (modo={settings.google_auth_mode}, "
          f"carpeta_raiz={settings.google_root_folder_id})")

    if settings.google_auth_mode == "api_key":
        if not settings.google_api_key:
            print("\nFALLO: GOOGLE_API_KEY no configurado")
            raise SystemExit(1)
        client = build_drive_client_api_key(settings.google_api_key)
    else:
        if not settings.google_service_account_file:
            print("\nFALLO: GOOGLE_SERVICE_ACCOUNT_FILE no configurado")
            raise SystemExit(1)
        client = build_drive_client(settings.google_service_account_file)

    if not settings.google_root_folder_id:
        print("\nFALLO: GOOGLE_ROOT_FOLDER_ID no configurado")
        raise SystemExit(1)

    repo = GoogleDriveRepository(client, shared_drive_id=settings.google_shared_drive_id)
    try:
        root = repo.get_item(settings.google_root_folder_id)
        print(f"\nOK: carpeta raíz = '{root.name}' ({root.source_item_id})")
        children = list(repo.list_children(settings.google_root_folder_id))
        print(f"Elementos directos: {len(children)}")
        for item in children[:20]:
            print(f"  [{item.item_type.value}] {item.name}")
        if len(children) > 20:
            print(f"  ... y {len(children) - 20} más")
    except Exception as exc:  # noqa: BLE001 - diagnóstico manual
        print(f"\nFALLO: {type(exc).__name__}: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
