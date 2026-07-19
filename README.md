# Document Engine

Motor de migración documental: explora Google Drive (solo lectura), construye
snapshots inmutables, permite priorizar y planificar una migración, y transfiere
archivo por archivo hacia un servidor FTP/FTPS de forma idempotente y reanudable.

Ver especificación completa en `AGENTE_DOCUMENT_ENGINE.md`.

## Estado actual: MVP funcional de punta a punta

Todas las pruebas: **98/98 pasan** (`pytest tests/ -q`). Ver [DECISIONS.md](DECISIONS.md)
para decisiones técnicas y limitaciones conocidas, y la sección
["Arquitectura e implementación"](#arquitectura-e-implementación) al final
de este documento para el detalle completo de cada componente.

## Requisitos

- Python >= 3.10
- Una cuenta de servicio de Google Cloud con acceso de lectura a la carpeta raíz
  a migrar (Google Drive API habilitada).

## Instalación

```bash
python -m venv .venv
.venv/Scripts/activate       # Windows
pip install -e ".[dev]"
cp .env.example .env         # completar credenciales
```

## Base de datos

```bash
alembic upgrade head
```

## Ejecutar pruebas

```bash
pytest tests/ -q                    # unitarias + integración de la API (98 pruebas)
pytest tests/unit -q                # solo unitarias
pytest tests/integration -q         # solo la API (FastAPI TestClient)
```

## Configuración de Google Drive

1. Crear una cuenta de servicio en Google Cloud, habilitar la Drive API.
2. Compartir la carpeta raíz (o unidad compartida) con el correo de la cuenta
   de servicio, con permiso de lector.
3. Descargar el JSON de credenciales y apuntar `GOOGLE_SERVICE_ACCOUNT_FILE`
   a su ruta. Configurar `GOOGLE_ROOT_FOLDER_ID` (y `GOOGLE_SHARED_DRIVE_ID`
   si aplica).

## Configuración de FTP/FTPS

Completar en `.env`: `FTP_MODE` (`ftps` recomendado, `ftp` solo si se
documenta y acepta el riesgo), `FTP_HOST`, `FTP_PORT`, `FTP_USERNAME`,
`FTP_PASSWORD`, `FTP_ROOT_PATH` (raíz de destino; el usuario FTP debe estar
limitado a ella), `FTP_PASSIVE`, `FTP_VERIFY_TLS` (mantener `true` en
producción). Antes de producción, ejecutar la prueba de conectividad:

```bash
curl -X POST http://localhost:8000/connections/ftp/test
```

que valida host/puerto, autenticación, escritura, lectura de tamaño,
renombramiento, eliminación de un temporal y soporte de reanudación (ver
`FTPRepository.check_connectivity` en
`src/document_engine/adapters/ftp/ftp_repository.py`).

## Configuración de OpenAI

`OPENAI_API_KEY`, `OPENAI_RENAME_ENABLED=true|false` (se puede apagar por
completo), `OPENAI_RENAME_MODEL` (por defecto `gpt-4o-mini`),
`OPENAI_TIMEOUT_SECONDS`, `OPENAI_MAX_CONCURRENCY`. Solo se invoca cuando un
nombre normalizado supera 25 caracteres (ver [DECISIONS.md](DECISIONS.md)
sobre qué metadatos se envían).

## Ejecución de la API

```bash
alembic upgrade head
uvicorn document_engine.main:app --reload
# OpenAPI interactivo en http://localhost:8000/docs
```

Ver [examples.http](examples.http) para el flujo completo de solicitudes
(discovery → batch → selectores → plan → preview → revisión de nombres →
start → status → report).

## Ejecución del worker (proceso independiente)

Para volúmenes grandes, en vez de `POST /migration-batches/{id}/start`
(síncrono, pensado para demos y lotes pequeños), usar el worker como
proceso de larga duración — puede correr en paralelo con otros:

```bash
python scripts/run_worker.py <batch_id> --max-items 500
```

Y para recuperar tras una caída o reiniciar el servicio:

```bash
python scripts/recover_jobs.py <batch_id>
python scripts/verify_destination.py <batch_id> --level STRONG
```

## Docker Compose (desarrollo)

```bash
docker compose up
```

Levanta PostgreSQL, un FTP de prueba (`fauria/vsftpd`, solo para desarrollo
local — nunca usar esas credenciales en producción) y la API.

## Flujo de dry-run, aprobación y migración

1. `POST /discovery-runs` → snapshot inmutable del origen (nunca se
   modifica Drive).
2. `POST /migration-batches` + `POST .../selectors` → seleccionar y
   priorizar.
3. `POST /migration-batches/{id}/plan` → genera el plan. **Esto ya es el
   dry-run**: no escribe nada en FTP.
4. `GET /migration-batches/{id}/preview` → revisar conteos, nombres
   propuestos, bloqueados y colisiones antes de continuar.
5. `GET /migration-batches/{id}/name-reviews` → revisar nombres asistidos
   por IA o con colisión no resuelta; `PATCH .../destination-name` o
   `POST .../approve-name` según corresponda.
6. `POST /migration-batches/{id}/start` (o `scripts/run_worker.py`) →
   recién aquí se escribe en el destino, archivo por archivo, de forma
   incremental e idempotente.

## Recuperación después de fallos

Un fallo de red o un reinicio del proceso nunca repite un elemento ya
`COMPLETED`, ni obliga a reiniciar el lote completo. Al reiniciar la
aplicación (o mediante `scripts/recover_jobs.py` /
`POST /migration-batches/{id}/recover`), `RecoveryService` revisa los
elementos con lease vencido, decide si el destino ya refleja éxito, si
puede reanudarse desde el temporal local, o si debe reiniciarse — ver
["Arquitectura e implementación"](#arquitectura-e-implementación) más abajo
y [DECISIONS.md](DECISIONS.md).

## Principios no negociables (recordatorio)

- Nunca se modifica, mueve, borra ni renombra nada en Google Drive.
- Cada snapshot es inmutable; una nueva exploración no altera las anteriores.
- No se descarga el árbol completo de una vez: procesamiento incremental
  archivo por archivo (Builder, `src/document_engine/application/migration_service.py`).
- Un archivo solo se marca `COMPLETED` después de crear el temporal remoto,
  validar su tamaño y renombrarlo atómicamente — nunca se sobrescribe un
  destino existente en silencio.

## Arquitectura e implementación

Detalle de cada componente del sistema, organizado por capa (Clean
Architecture / Ports & Adapters):

### Base y persistencia

- Estructura del proyecto, `Settings` vía Pydantic Settings (`.env`).
- Modelo de datos completo (`src/document_engine/adapters/database/models.py`)
  y migraciones de Alembic.
- Dominio: entidades, enums, máquina de estados de `MigrationItem`, errores
  tipados.

### Discovery (origen)

- Puerto `SourceRepositoryPort` y adaptador de Google Drive
  (`adapters/google_drive`): exploración paginada, DFS con detección de
  ciclos, exclusión de papelera, descarga y exportación de tipos nativos de
  Workspace.
- `DiscoveryService`: snapshot completo (toda la raíz) y parcial
  (subcarpetas por ID). Cada corrida crea un snapshot nuevo e inmutable.
- `SnapshotSearchService`: búsqueda por texto, prefijo de ruta, id, tipo,
  mime, fechas, tamaño y carpeta padre.

### Motor de reglas de nombres

`NamingRulesEngine` (`src/document_engine/domain/naming_rules.py`):

- Pipeline determinista de normalización (pasos 1-11 de la sección 5.2):
  separar extensión, NFKD, quitar diacríticos, mayúsculas, caracteres
  inválidos a `_`, colapso/strip de `_`, catálogo de abreviaturas
  (`config/abbreviations.yml`, editable sin tocar código), composición con
  OBTC/consecutivo/versión/fecha (fecha siempre al final, nunca inventados),
  validación contra `^[A-Z0-9]+(?:_[A-Z0-9]+)*$` y bandera `needs_ai` cuando
  el resultado supera 25 caracteres.
- `resolve_collision`: sufijos `_01`.._99` deterministas y estables, con
  fallback por huella hash y `requires_review=True` tras 99 colisiones; nunca
  sobrescribe silenciosamente.

### Planning y prioridades

`src/document_engine/application/planning_service.py`:

- `BatchService`: crea `MigrationBatch` y sus `BatchSelector`
  (`EXPLICIT_IDS`, `FOLDER_RECURSIVE`, `PATH_PREFIX`, `SEARCH_RESULT`), con
  include/exclude y prioridad por selector (o heredada del lote). Prioridad
  solo se puede cambiar antes de iniciar o en pausa (`set_batch_priority`).
- `PlanningService.resolve_selection`: aplica selectores, exclusiones, e
  incluye implícitamente las carpetas ancestras necesarias para cada archivo
  seleccionado.
- `order_by_priority_dfs`: mayor prioridad primero; dentro de la misma
  prioridad, DFS estable (orden lexicográfico de `logical_path`, que
  completa una rama antes de pasar a la siguiente).
- `generate_plan`: crea `MigrationItem` sin escribir en ningún repositorio
  (Planning **es** el dry-run). Resuelve exportación de tipos nativos de
  Google Workspace (`config/export_formats.yml`), bloquea ZIP/RAR y archivos
  sin permiso de descarga, resuelve colisiones por carpeta destino, marca
  `WAITING_REVIEW` cuando el nombre supera 25 caracteres (pendiente de
  asistencia de IA), y es re-ejecutable sin duplicar elementos (usa la clave
  de idempotencia).
- `preview`: conteos, tamaños, bloqueados, pendientes de revisión y
  colisiones resueltas — vista previa antes de tocar el destino.
- `NameReviewService`: sobrescritura manual de nombre destino y aprobación,
  auditadas en `NameDecision` y `JournalEvent`.

### Integración con OpenAI

- `ports/ai_naming_provider.py`: puerto `AINamingProviderPort` con
  `AINamingRequest`/`AINamingResponse` (sin contenido de archivos ni
  credenciales, solo metadatos).
- `adapters/openai/`: `OpenAINamingProvider` sobre la Responses API con
  salida estructurada (JSON schema estricto), timeout y reintentos de red
  con backoff exponencial (`tenacity`); `ConcurrencyLimitedAINamingProvider`
  aplica `OPENAI_MAX_CONCURRENCY`; `prompts.py` construye el prompt base de
  la sección 6.6.
- `application/naming_service.py` (`NamingAssistantService`): nunca confía
  en la salida del modelo — valida longitud, patrón, conservación de OBTC y
  fecha; hace **como máximo un reintento** enviando los errores de
  validación; si sigue fallando (o el proveedor lanza un error de
  red/permanente), aplica un fallback determinista y marca
  `requires_review=True`. Cachea decisiones exitosas por huella estable
  (`NameDecision.input_fingerprint`) para no pagar dos veces por la misma
  entrada. Resuelve colisiones contra los hermanos ya planificados en la
  misma carpeta destino. Todo queda auditado en `NameDecision` y
  `JournalEvent` (modelo usado, razón, confianza, motivo de fallback,
  tokens si la API los devuelve).

### Adaptador de destino y Builder

- `ports/destination_repository.py`: puerto `DestinationRepositoryPort`
  (`ensure_directory`, `exists`, `get_size`, `upload`, `rename`, `delete`,
  `supports_resume`, `list_dir`, `download_to`).
- `adapters/ftp/ftp_repository.py` (`FTPRepository`): FTP o FTPS explícito
  por configuración, modo pasivo, verificación TLS, workaround NAT-safe para
  servidores detrás de NAT/DDNS (ignora la IP privada devuelta en `PASV` y
  reutiliza el host de control, igual que hacen el Explorador de Windows o
  FileZilla), `mkdir` recursivo idempotente, subida por chunks con `REST`
  cuando el servidor lo soporta, rename atómico (`RNFR/RNTO`), reconexión
  automática (`NOOP` + reconectar), detección de capacidades vía `FEAT`,
  resolución de rutas que rechaza path traversal, y `check_connectivity()`
  como prueba de conectividad previa a producción.
- `adapters/filesystem/temp_storage.py` (`TempFileStorage`): ruta temporal
  local estable por `migration_item_id`, SHA-256 calculado mientras se
  escribe el stream.
- `application/migration_service.py` (`Builder`): procesa un `MigrationItem`
  en estado `READY`/`RETRY_PENDING` de punta a punta — crea carpetas
  destino, descarga o exporta (tipos nativos de Google Workspace) a un
  temporal local, sube con nombre `.NOMBRE_DESTINO.partial.<migration_item_id>`,
  valida que el tamaño remoto coincida con el descargado, **nunca
  sobrescribe** un destino ya existente, renombra atómicamente al nombre
  final, marca `COMPLETED` y limpia el temporal local. Es idempotente (un
  elemento `COMPLETED` es un no-op) y transiciona errores transitorios a
  `RETRY_PENDING` y permanentes a `FAILED`, todo vía la máquina de estados y
  auditado en `JournalEvent`.

### Recuperación y validación

- `worker/lease_manager.py`: `claim_next_item` (lease temporal +
  prioridad/DFS), `heartbeat`, `release_lease`.
- El Builder reanuda una descarga parcial por rango de bytes
  (`TempFileStorage.append_stream`), reutiliza un temporal local ya completo
  sin volver a descargar, y reanuda una carga FTP con `REST` cuando el
  servidor lo soporta y hay un temporal remoto parcial.
- `application/recovery_service.py` (`RecoveryService`): al iniciar, busca
  elementos con lease vencido, decide si el destino ya refleja éxito
  (`COMPLETED` directo), si puede reanudarse desde el temporal local
  (`RETRY_PENDING` con `downloaded_bytes` corregido), o si debe reiniciarse
  desde cero — todo auditado en `JournalEvent`.
- `application/validation_service.py`: `ValidationService` con niveles
  `BASIC` (tamaño), `STRONG` (+ hash remoto si el servidor lo expone) y
  `STRICT` (+ re-descarga y comparación SHA-256); `generate_batch_report`
  con los conteos de la sección 10.
- `COMPLETED` es una transición válida desde cualquier estado "en vuelo"
  (necesario para que la recuperación pueda saltar directo a `COMPLETED`
  cuando el destino ya refleja éxito tras una caída).

### API FastAPI

- `api/dependencies.py`, `api/schemas.py`, `api/routers/` (health,
  discovery, batches, name_review, execution, items), `main.py`.
- 35 endpoints: salud/capacidades, pruebas de conexión (sin exponer
  secretos), discovery/snapshots/búsqueda, lotes/selectores/plan/preview,
  revisión de nombres (override/approve/regenerate-ai-name), ejecución
  (start/pause/resume/cancel/retry-failed/status/events/report/recover),
  operaciones sobre elementos (retry/skip/reprocess).
- Autenticación por `X-API-Key` (`require_api_key`), opcional si
  `INTERNAL_API_KEY` no está configurada (documentado como modo desarrollo).
- Manejadores de excepción globales traducen `InvalidStateTransition` (409),
  `TransientError` (503) y `PermanentError`/`DocumentEngineError` (400) a
  respuestas HTTP consistentes con `error_code`.

### Cobertura de pruebas

- `tests/unit/test_discovery_and_search.py`: discovery y búsqueda.
- `tests/unit/test_naming_rules.py`: 33 pruebas del motor de nombres,
  incluida una basada en propiedades con Hypothesis.
- `tests/unit/test_planning_service.py`: 17 pruebas sobre un árbol con
  colisiones, nombre largo, ZIP, doc nativo de Google y exclusiones.
- `tests/unit/test_naming_service.py`: 21 pruebas con un
  `FakeAINamingProvider` programable (respuesta válida, inválida con
  reintento, fallback, caché, colisión entre sugerencias de IA).
- `tests/unit/test_migration_service.py`: 12 pruebas con un
  `FakeDestinationRepository` en memoria (ciclo completo, nombre temporal,
  rename atómico, no sobrescritura, idempotencia).
- `tests/unit/test_recovery_and_validation.py`: 19 pruebas con inyección de
  fallos (leases, reanudación de descarga/carga, recuperación en cada
  estado en vuelo, niveles de validación, reporte de lote).
- `tests/integration/test_api.py`: 7 pruebas de la API completa con
  `TestClient` (flujo discovery → batch → plan → preview → start → status →
  report → events, autenticación, errores 404/409/422).
