from __future__ import annotations

import httplib2
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import Resource, build
from google.oauth2 import service_account

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

FIELDS = (
    "nextPageToken, files(id, name, parents, mimeType, size, createdTime, "
    "modifiedTime, md5Checksum, trashed, capabilities/canDownload, "
    "shortcutDetails/targetId, shortcutDetails/targetMimeType)"
)

# httplib2 no tiene timeout por defecto: un stall de red (frecuente con
# archivos grandes, p. ej. video) deja la descarga colgada para siempre en
# vez de fallar y dejar que el mecanismo de reintentos/lease actúe.
_DEFAULT_TIMEOUT_SECONDS = 120


def build_service_account_credentials(service_account_file: str) -> service_account.Credentials:
    """Carga las credenciales de la cuenta de servicio UNA sola vez para
    reutilizarlas entre elementos (sección de rendimiento/cuota de Drive).

    `Credentials.from_service_account_file` no hace ninguna llamada de red
    por sí sola, pero el objeto que devuelve cachea el access token y solo
    lo renueva cuando expira (~1h) — si en cambio se crea una instancia
    NUEVA por cada elemento (como se hacía antes), cada una arranca sin
    token y fuerza su propio intercambio OAuth contra
    `oauth2.googleapis.com` en la primera llamada. Con miles de elementos
    eso multiplica el volumen de peticiones automatizadas desde la misma
    IP hacia dominios de Google, y es lo que dispara el bloqueo
    anti-abuso de su front-end (la página HTML "Sorry...", ver
    `GoogleDriveRepository._is_rate_limit_error`) — no solo contra la API
    de Drive en sí. Es seguro compartir esta instancia entre hilos:
    `google-auth` serializa su propio refresh internamente."""
    return service_account.Credentials.from_service_account_file(service_account_file, scopes=SCOPES)


def build_drive_client(
    credentials: service_account.Credentials, *, timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS
) -> Resource:
    """Construye un cliente de Drive NUEVO (socket/pool de httplib2 propio,
    necesario para el aislamiento entre workers en paralelo y para que
    abandonar un hilo colgado no deje conexiones envenenadas) pero sobre
    unas credenciales YA autenticadas y compartidas — ver
    `build_service_account_credentials`."""
    http = AuthorizedHttp(credentials, http=httplib2.Http(timeout=timeout_seconds))
    return build("drive", "v3", http=http, cache_discovery=False)


def build_drive_client_api_key(api_key: str, *, timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS) -> Resource:
    """Cliente de solo lectura autenticado por API key (sin OAuth). Solo
    puede acceder a archivos/carpetas compartidos públicamente ("cualquiera
    con el enlace"); no sirve para contenido restringido a usuarios/cuentas
    específicas."""
    http = httplib2.Http(timeout=timeout_seconds)
    return build("drive", "v3", developerKey=api_key, http=http, cache_discovery=False)
