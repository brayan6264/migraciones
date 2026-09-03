from __future__ import annotations

from collections.abc import Callable, Iterator
from functools import lru_cache

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from document_engine.adapters.database.session import get_session_factory as _get_session_factory
from document_engine.adapters.filesystem.temp_storage import TempFileStorage
from document_engine.domain.naming_rules import NamingRulesEngine
from document_engine.ports.ai_naming_provider import AINamingProviderPort
from document_engine.ports.destination_repository import DestinationRepositoryPort
from document_engine.ports.source_repository import SourceRepositoryPort
from document_engine.settings import Settings, get_settings


def get_db() -> Iterator[Session]:
    session_factory = _get_session_factory()
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


def get_db_session_factory():
    """La session factory activa, separada de `get_db` para que tareas en
    segundo plano (que viven más allá del request HTTP) puedan crear su
    propia sesión sin depender del generador `yield`/`finally` de `get_db`.
    Sobreescribible en tests igual que las demás dependencias, para que el
    background task use la misma base de datos aislada que el test."""
    return _get_session_factory()


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


def build_source_repository(settings: Settings) -> SourceRepositoryPort:
    """Construye un adaptador real de Google Drive nuevo (con su propia
    conexión). Cada worker en paralelo necesita el suyo: el pool de
    conexiones de httplib2 no es seguro para compartir entre hilos."""
    from document_engine.adapters.google_drive.drive_repository import GoogleDriveRepository

    client = _build_drive_client(settings)
    return GoogleDriveRepository(client, shared_drive_id=settings.google_shared_drive_id)


def get_source_repository(settings: Settings = Depends(get_settings)) -> SourceRepositoryPort:
    """Una instancia por request (browse/discovery). En pruebas se
    sobreescribe con `app.dependency_overrides`."""
    return build_source_repository(settings)


def get_source_factory(
    settings: Settings = Depends(get_settings),
) -> Callable[[], SourceRepositoryPort]:
    """Fábrica que crea repos de Drive frescos bajo demanda — para que cada
    worker en paralelo tenga su propia conexión. En pruebas se sobreescribe
    devolviendo un fake compartido."""
    return lambda: build_source_repository(settings)


@lru_cache
def _drive_credentials_for(service_account_file: str):
    """Cachea las credenciales OAuth por archivo (`@lru_cache` a nivel de
    proceso): con `worker_concurrency` workers procesando miles de
    elementos, cada uno llamaba antes a `_build_drive_client` -> nuevas
    credenciales -> un intercambio de token nuevo contra
    `oauth2.googleapis.com` POR ELEMENTO. Ese volumen de peticiones
    automatizadas repetidas desde la misma IP es lo que dispara el bloqueo
    anti-abuso de Google (la página HTML "Sorry...", sección
    `GoogleDriveRepository._is_rate_limit_error`). Reutilizar el mismo
    objeto de credenciales entre elementos reduce ese volumen a ~1 refresh
    por hora (la validez del token) en vez de uno por archivo."""
    from document_engine.adapters.google_drive.client import build_service_account_credentials

    return build_service_account_credentials(service_account_file)


def _build_drive_client(settings: Settings):
    from document_engine.adapters.google_drive.client import build_drive_client, build_drive_client_api_key

    if settings.google_auth_mode == "api_key":
        if not settings.google_api_key:
            raise HTTPException(status_code=503, detail="GOOGLE_API_KEY no configurado")
        return build_drive_client_api_key(settings.google_api_key, timeout_seconds=settings.google_timeout_seconds)
    if not settings.google_service_account_file:
        raise HTTPException(status_code=503, detail="GOOGLE_SERVICE_ACCOUNT_FILE no configurado")
    credentials = _drive_credentials_for(str(settings.google_service_account_file))
    return build_drive_client(credentials, timeout_seconds=settings.google_timeout_seconds)


def build_destination_repository(settings: Settings) -> DestinationRepositoryPort:
    """Construye un adaptador FTP/FTPS nuevo (con su propia conexión de
    control). Cada worker en paralelo necesita el suyo: una conexión ftplib
    es stateful y NO es segura para compartir entre hilos."""
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


def get_destination_repository(settings: Settings = Depends(get_settings)) -> DestinationRepositoryPort:
    """Una instancia por request (ftp browse/test). En pruebas se
    sobreescribe con `app.dependency_overrides`."""
    return build_destination_repository(settings)


def get_destination_factory(
    settings: Settings = Depends(get_settings),
) -> Callable[[], DestinationRepositoryPort]:
    """Fábrica que crea conexiones FTP frescas bajo demanda — una por worker
    en paralelo. En pruebas se sobreescribe devolviendo un fake compartido."""
    return lambda: build_destination_repository(settings)


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
