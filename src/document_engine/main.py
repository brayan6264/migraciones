from __future__ import annotations

import logging
import socket

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import inspect as sa_inspect, select, text

from document_engine.api.routers import batches, discovery, execution, health, items, name_review
from document_engine.domain.errors import DocumentEngineError, InvalidStateTransition, PermanentError, TransientError
from document_engine.settings import get_settings

# Sin esto, los `logger.exception(...)` de los workers en segundo plano
# (background_runner, migration_service, etc.) dependían del "handler de
# último recurso" de Python — que solo actúa si NINGÚN logger en toda la
# jerarquía tiene un handler propio, algo frágil según cómo se lance el
# proceso (uvicorn, docker, systemd). Se configura explícito para que los
# fallos de migración siempre queden en stdout/stderr del backend.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Red de seguridad de último recurso contra descargas colgadas: se
# comprobó en vivo (con `sys._current_frames()`) que el timeout explícito
# configurado en el cliente de Drive (`httplib2.Http(timeout=...)`) no se
# aplicaba a un socket SSL reusado — el hilo del worker en segundo plano
# quedaba bloqueado para siempre en `ssl.py: self._sslobj.read(...)` sin
# ningún error, dejando el lote "colgado" sin ninguna señal visible.
# `setdefaulttimeout` actúa a nivel de la librería estándar: todo socket
# nuevo que no reciba su propio timeout explícito hereda este por
# construcción, sin depender de que cada librería lo configure bien.
socket.setdefaulttimeout(180)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Document Engine",
        description="Motor de migración documental de Google Drive a FTP/FTPS",
        version="0.1.0",
    )

    origins = [origin.strip() for origin in get_settings().frontend_origins.split(",") if origin.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    def _run_schema_migrations() -> None:
        from document_engine.adapters.database.session import get_engine
        engine = get_engine()
        with engine.connect() as conn:
            insp = sa_inspect(engine)
            # Guard: if the table doesn't exist yet (fresh database, alembic
            # upgrade head has not been run), there is nothing to do — Alembic
            # will create it with all columns including skip_existing.
            if not insp.has_table("migration_batches"):
                return
            existing = {c["name"] for c in insp.get_columns("migration_batches")}
            if "skip_existing" not in existing:
                default = "0" if engine.dialect.name == "sqlite" else "FALSE"
                conn.execute(text(
                    f"ALTER TABLE migration_batches ADD COLUMN skip_existing BOOLEAN NOT NULL DEFAULT {default}"
                ))
                conn.commit()

    @app.on_event("startup")
    def _recover_stuck_items() -> None:
        """`RecoveryService` (sección 9.5) documenta que corre al iniciar la
        app, pero hasta ahora solo era alcanzable a mano vía `/recover` o
        `scripts/recover_jobs.py`. Un reinicio/caída del servidor a mitad de
        un lote deja elementos en un estado "en vuelo"
        (`CREATING_DIRECTORIES`..`VALIDATING`) con lease propio de ese
        proceso muerto; sin este barrido, esos elementos quedan invisibles
        para `claim_next_item` hasta que ese lease venza solo (hasta varias
        horas), igual que el bug de la sección de fondo con excepciones no
        traducidas."""
        from document_engine.adapters.database.session import get_engine, get_session_factory
        from document_engine.adapters.database.models import MigrationItem as MigrationItemModel
        from document_engine.application.recovery_service import IN_FLIGHT_STATES, RecoveryService
        from document_engine.adapters.filesystem.temp_storage import TempFileStorage

        engine = get_engine()
        insp = sa_inspect(engine)
        if not insp.has_table("migration_items"):
            return

        settings = get_settings()
        session_factory = get_session_factory()
        db = session_factory()
        try:
            batch_ids = (
                db.execute(
                    select(MigrationItemModel.batch_id)
                    .where(MigrationItemModel.state.in_(IN_FLIGHT_STATES))
                    .distinct()
                )
                .scalars()
                .all()
            )
            if not batch_ids:
                return
            try:
                from document_engine.api.dependencies import build_destination_repository
                destination = build_destination_repository(settings)
            except Exception:  # noqa: BLE001 - sin FTP configurado (p. ej. en dev) no hay nada que recuperar
                return
            temp_storage = TempFileStorage(settings.temp_dir)
            for batch_id in batch_ids:
                try:
                    recovered = RecoveryService(db, destination, temp_storage).recover_batch(batch_id)
                    if recovered:
                        logger.info(
                            "Recuperados %s elementos en vuelo del lote %s al arrancar",
                            len(recovered),
                            batch_id,
                        )
                except Exception:  # noqa: BLE001 - un lote problemático no debe tumbar el arranque
                    db.rollback()
        finally:
            db.close()

    app.include_router(health.router)
    app.include_router(discovery.router)
    app.include_router(batches.router)
    app.include_router(name_review.router)
    app.include_router(execution.router)
    app.include_router(items.router)

    @app.exception_handler(InvalidStateTransition)
    def _invalid_transition_handler(request: Request, exc: InvalidStateTransition) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error_code": exc.code, "detail": str(exc)})

    @app.exception_handler(TransientError)
    def _transient_error_handler(request: Request, exc: TransientError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"error_code": exc.code, "detail": str(exc)})

    @app.exception_handler(PermanentError)
    def _permanent_error_handler(request: Request, exc: PermanentError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error_code": exc.code, "detail": str(exc)})

    @app.exception_handler(DocumentEngineError)
    def _domain_error_handler(request: Request, exc: DocumentEngineError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error_code": exc.code, "detail": str(exc)})

    return app


app = create_app()
