from __future__ import annotations

import socket

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from document_engine.api.routers import batches, discovery, execution, health, items, name_review
from document_engine.domain.errors import DocumentEngineError, InvalidStateTransition, PermanentError, TransientError
from document_engine.settings import get_settings

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
