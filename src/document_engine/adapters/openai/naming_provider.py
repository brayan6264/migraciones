from __future__ import annotations

import json
import threading
from contextlib import contextmanager

from openai import APIConnectionError, APITimeoutError, OpenAI, RateLimitError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from document_engine.adapters.openai.prompts import SYSTEM_PROMPT, build_user_prompt
from document_engine.domain.errors import PermanentError, TransientError
from document_engine.ports.ai_naming_provider import AINamingProviderPort, AINamingRequest, AINamingResponse

NAMING_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "suggested_name": {"type": "string", "minLength": 1, "maxLength": 25},
        "reason": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "requires_review": {"type": "boolean"},
    },
    "required": ["suggested_name", "reason", "confidence", "requires_review"],
    "additionalProperties": False,
}

_RETRYABLE_EXCEPTIONS = (APIConnectionError, APITimeoutError, RateLimitError)


class OpenAINamingProvider(AINamingProviderPort):
    """Adaptador sobre la Responses API de OpenAI con salida estructurada.

    Los reintentos de este decorador cubren fallas transitorias de red
    (timeout, 429, 5xx). El reintento de validación de contenido (cuando el
    modelo responde algo sintácticamente válido pero incorrecto para las
    reglas de nombres) es responsabilidad de `NamingAssistantService`.
    """

    def __init__(self, client: OpenAI, *, model: str, timeout_seconds: int = 30, max_network_retries: int = 3):
        self._client = client
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_network_retries = max_network_retries

    def suggest_name(self, request: AINamingRequest) -> AINamingResponse:
        try:
            return self._call_with_retry(request)
        except _RETRYABLE_EXCEPTIONS as exc:
            raise TransientError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - traducido a error de dominio
            raise PermanentError(str(exc), code="NAME_AI_INVALID_OUTPUT") from exc

    def _call_with_retry(self, request: AINamingRequest) -> AINamingResponse:
        @retry(
            reraise=True,
            stop=stop_after_attempt(self._max_network_retries),
            wait=wait_exponential_jitter(initial=1, max=10),
            retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
        )
        def _call() -> AINamingResponse:
            response = self._client.responses.create(
                model=self._model,
                timeout=self._timeout_seconds,
                input=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": build_user_prompt(request)},
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "naming_suggestion",
                        "schema": NAMING_JSON_SCHEMA,
                        "strict": True,
                    }
                },
            )
            payload = json.loads(response.output_text)
            tokens = None
            usage = getattr(response, "usage", None)
            if usage is not None:
                tokens = getattr(usage, "total_tokens", None)
            return AINamingResponse(
                suggested_name=payload["suggested_name"],
                reason=payload["reason"],
                confidence=float(payload["confidence"]),
                requires_review=bool(payload["requires_review"]),
                tokens_used=tokens,
            )

        return _call()


class ConcurrencyLimitedAINamingProvider(AINamingProviderPort):
    """Decorador que limita las llamadas concurrentes al proveedor de IA
    (`OPENAI_MAX_CONCURRENCY`)."""

    def __init__(self, inner: AINamingProviderPort, *, max_concurrency: int):
        self._inner = inner
        self._semaphore = threading.Semaphore(max_concurrency)

    @contextmanager
    def _slot(self):
        self._semaphore.acquire()
        try:
            yield
        finally:
            self._semaphore.release()

    def suggest_name(self, request: AINamingRequest) -> AINamingResponse:
        with self._slot():
            return self._inner.suggest_name(request)


def build_openai_naming_provider(
    api_key: str, *, model: str, timeout_seconds: int = 30, max_concurrency: int = 3
) -> AINamingProviderPort:
    client = OpenAI(api_key=api_key)
    base = OpenAINamingProvider(client, model=model, timeout_seconds=timeout_seconds)
    return ConcurrencyLimitedAINamingProvider(base, max_concurrency=max_concurrency)
