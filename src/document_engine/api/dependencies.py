from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from document_engine.adapters.database.session import get_session_factory
from document_engine.adapters.filesystem.temp_storage import TempFileStorage
from document_engine.domain.naming_rules import NamingRulesEngine
from document_engine.ports.ai_naming_provider import AINamingProviderPort
from document_engine.ports.destination_repository import DestinationRepositoryPort
from document_engine.ports.source_repository import SourceRepositoryPort
from document_engine.settings import Settings, get_settings


def get_db() -> Iterator[Session]:
    session_factory = get_session_factory()
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


def require_api_key(
    x_api_key: str | None = Header(default=None), settings: Settings = Depends(get_settings)
) -> None:
    """Autenticación interna por API key (sección 12.2). Si
    `INTERNAL_API_KEY` no está configurada, el servidor se considera en modo
    desarrollo y no exige encabezado (debe configurarse antes de producción)."""
    if not settings.internal_api_key:
        return
    if x_api_key != settings.internal_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key inválida o ausente")


@lru_cache
def _naming_engine_for(abbreviations_file: str) -> NamingRulesEngine:
    return NamingRulesEngine.from_yaml(abbreviations_file)


def get_naming_engine(settings: Settings = Depends(get_settings)) -> NamingRulesEngine:
    return _naming_engine_for(str(settings.abbreviations_file))


def get_temp_storage(settings: Settings = Depends(get_settings)) -> TempFileStorage:
    return TempFileStorage(settings.temp_dir)


def get_source_repository(settings: Settings = Depends(get_settings)) -> SourceRepositoryPort:
    """Construye el adaptador real de Google Drive a partir de `Settings`.
    En pruebas se sobreescribe con `app.dependency_overrides`."""
    from document_engine.adapters.google_drive.client import build_drive_client
    from document_engine.adapters.google_drive.drive_repository import GoogleDriveRepository

    if not settings.google_service_account_file:
        raise HTTPException(status_code=503, detail="GOOGLE_SERVICE_ACCOUNT_FILE no configurado")
    client = build_drive_client(settings.google_service_account_file)
    return GoogleDriveRepository(client, shared_drive_id=settings.google_shared_drive_id)


def get_destination_repository(settings: Settings = Depends(get_settings)) -> DestinationRepositoryPort:
    """Construye el adaptador real de FTP/FTPS a partir de `Settings`.
    En pruebas se sobreescribe con `app.dependency_overrides`."""
    from document_engine.adapters.ftp.ftp_repository import FTPRepository

    if not settings.ftp_host:
        raise HTTPException(status_code=503, detail="FTP_HOST no configurado")
    return FTPRepository(
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


def get_ai_naming_provider(settings: Settings = Depends(get_settings)) -> AINamingProviderPort | None:
    if not settings.openai_rename_enabled or not settings.openai_api_key:
        return None
    from document_engine.adapters.openai.naming_provider import build_openai_naming_provider

    return build_openai_naming_provider(
        settings.openai_api_key,
        model=settings.openai_rename_model,
        timeout_seconds=settings.openai_timeout_seconds,
        max_concurrency=settings.openai_max_concurrency,
    )
