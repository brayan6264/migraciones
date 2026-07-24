from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from document_engine.api.dependencies import get_db, get_destination_repository, require_api_key
from document_engine.api.schemas import ConnectivityTestOut, FtpBrowseItemOut
from document_engine.ports.destination_repository import DestinationRepositoryPort
from document_engine.settings import Settings, get_settings

router = APIRouter(tags=["health"])


@router.get("/health/live")
def live() -> dict:
    return {"status": "ok"}


@router.get("/health/ready")
def ready(db: Session = Depends(get_db)) -> dict:
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


@router.get("/capabilities", dependencies=[Depends(require_api_key)])
def capabilities(settings: Settings = Depends(get_settings)) -> dict:
    """Nunca expone secretos: solo banderas de configuración."""
    return {
        "ftp_mode": settings.ftp_mode,
        "ftp_passive": settings.ftp_passive,
        "ftp_verify_tls": settings.ftp_verify_tls,
        "openai_rename_enabled": settings.openai_rename_enabled and bool(settings.openai_api_key),
        "openai_model": settings.openai_rename_model,
        "validation_level": settings.validation_level,
        "max_item_retries": settings.max_item_retries,
    }


@router.post("/connections/google-drive/test", dependencies=[Depends(require_api_key)])
def test_google_drive(settings: Settings = Depends(get_settings)) -> ConnectivityTestOut:
    if settings.google_auth_mode == "api_key":
        if not settings.google_api_key:
            return ConnectivityTestOut(ok=False, detail="GOOGLE_API_KEY no configurado")
    elif not settings.google_service_account_file:
        return ConnectivityTestOut(ok=False, detail="GOOGLE_SERVICE_ACCOUNT_FILE no configurado")
    try:
        from document_engine.adapters.google_drive.client import build_drive_client, build_drive_client_api_key

        client = (
            build_drive_client_api_key(settings.google_api_key, timeout_seconds=settings.google_timeout_seconds)
            if settings.google_auth_mode == "api_key"
            else build_drive_client(settings.google_service_account_file, timeout_seconds=settings.google_timeout_seconds)
        )
        if settings.google_root_folder_id:
            client.files().get(fileId=settings.google_root_folder_id, fields="id,name").execute()
        return ConnectivityTestOut(ok=True, detail="Conexión y credenciales válidas")
    except Exception as exc:  # noqa: BLE001 - se sanitiza antes de exponer
        return ConnectivityTestOut(ok=False, detail=f"{type(exc).__name__}: {exc}")


@router.get("/ftp/browse", response_model=list[FtpBrowseItemOut], dependencies=[Depends(require_api_key)])
def browse_ftp(
    path: str = "",
    destination: DestinationRepositoryPort = Depends(get_destination_repository),
) -> list[FtpBrowseItemOut]:
    """Explora subdirectorios ya existentes en el FTP, para que el usuario
    pueda elegir un destino real del servidor (sección de selección visual)
    en vez de escribir una ruta a mano. Solo lectura: no crea nada."""
    base = path.strip("/")
    names = destination.list_directories(base)
    return [
        FtpBrowseItemOut(name=name, path=f"{base}/{name}" if base else name)
        for name in sorted(names)
    ]


@router.post("/connections/ftp/test", dependencies=[Depends(require_api_key)])
def test_ftp(settings: Settings = Depends(get_settings)) -> ConnectivityTestOut:
    if not settings.ftp_host:
        return ConnectivityTestOut(ok=False, detail="FTP_HOST no configurado")
    try:
        from document_engine.adapters.ftp.ftp_repository import FTPRepository

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
        report = repo.check_connectivity()
        return ConnectivityTestOut(ok=bool(report.get("connected")), detail=str(report))
    except Exception as exc:  # noqa: BLE001 - se sanitiza antes de exponer
        return ConnectivityTestOut(ok=False, detail=f"{type(exc).__name__}: {exc}")
