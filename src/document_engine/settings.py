from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"
    database_url: str = "sqlite:///./document_engine.db"
    temp_dir: Path = Path("./data/tmp")

    google_auth_mode: str = "service_account"
    google_service_account_file: str | None = None
    google_api_key: str | None = None
    google_root_folder_id: str | None = None
    google_shared_drive_id: str | None = None
    google_timeout_seconds: int = 120


    ftp_mode: str = "ftps"
    ftp_host: str | None = None
    ftp_port: int = 21
    ftp_username: str | None = None
    ftp_password: str | None = None
    ftp_root_path: str = "/DOCUMENT_ENGINE"
    ftp_passive: bool = True
    ftp_verify_tls: bool = True
    ftp_timeout_seconds: int = 60

    openai_api_key: str | None = None
    openai_rename_enabled: bool = True
    openai_rename_model: str = "gpt-4o-mini"
    openai_timeout_seconds: int = 30
    openai_max_concurrency: int = 3

    transfer_chunk_size_mb: int = 8
    max_item_retries: int = 5
    retry_base_seconds: int = 2
    worker_lease_seconds: int = 120
    worker_heartbeat_seconds: int = 30
    validation_level: str = "BASIC"
    # Cuántos elementos procesa en paralelo `POST .../run`. Conservador por
    # defecto: cada worker abre su propia conexión al FTP y muchos servidores
    # limitan el nº de conexiones simultáneas (el nuestro: 10). Subir con
    # cuidado según lo que aguante el servidor de destino.
    worker_concurrency: int = 3

    internal_api_key: str | None = None

    frontend_origins: str = "http://localhost:5173"

    abbreviations_file: Path = Path("config/abbreviations.yml")
    export_formats_file: Path = Path("config/export_formats.yml")


@lru_cache
def get_settings() -> Settings:
    return Settings()
