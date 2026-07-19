from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class AINamingRequest:
    """Metadatos mínimos enviados al modelo (sección 6.2). Nunca incluye
    contenido del archivo ni credenciales."""

    item_type: str
    original_name: str
    normalized_name: str
    extension_or_mime: str
    ancestor_path: str
    local_context: str
    obtc_code: str | None
    date: str | None
    version: str | None
    category: str | None
    abbreviation_catalog: dict[str, str] = field(default_factory=dict)
    previous_errors: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class AINamingResponse:
    suggested_name: str
    reason: str
    confidence: float
    requires_review: bool
    tokens_used: int | None = None


class AINamingProviderPort(ABC):
    """Puerto hacia el proveedor de IA de renombramiento. Solo se invoca
    cuando el nombre normalizado supera 25 caracteres (sección 6.1)."""

    @abstractmethod
    def suggest_name(self, request: AINamingRequest) -> AINamingResponse:
        """Puede lanzar `TransientError` (timeout, 429, 5xx) o
        `PermanentError` (credenciales inválidas, respuesta irrecuperable)."""
