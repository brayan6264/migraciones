"""Motor de reglas de nomenclatura documental (sección 5 de la especificación).

Independiente de Google Drive y FTP: opera únicamente sobre strings y
diccionarios de configuración. No invoca IA; solo determina cuándo sería
necesaria (`needs_ai`), lo cual queda a cargo de `naming_service` (sprint 4).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import yaml

MAX_BASE_LENGTH = 25
NAME_PATTERN = re.compile(r"^[A-Z0-9]+(?:_[A-Z0-9]+)*$")

_INVALID_CHARS_PATTERN = re.compile(r"[^A-Z0-9]+")
_MULTI_UNDERSCORE_PATTERN = re.compile(r"_+")


def strip_diacritics(text: str) -> str:
    """Paso 2-3: normalización Unicode NFKD y eliminación de diacríticos."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def split_base_and_extension(filename: str) -> tuple[str, str]:
    """Paso 1: separa nombre base y extensión. La extensión se normaliza a minúscula."""
    if "." not in filename:
        return filename, ""
    base, _, ext = filename.rpartition(".")
    if base == "":
        # Nombres tipo ".gitignore": no hay extensión real que separar.
        return filename, ""
    return base, ext.lower()


def sanitize_base(text: str) -> str:
    """Pasos 4-7: mayúsculas, caracteres no permitidos a `_`, colapso y strip de `_`."""
    text = strip_diacritics(text)
    text = text.upper()
    text = _INVALID_CHARS_PATTERN.sub("_", text)
    text = _MULTI_UNDERSCORE_PATTERN.sub("_", text)
    return text.strip("_")


def apply_abbreviations(base: str, catalog: dict[str, str]) -> str:
    """Paso 8: reemplaza tokens completos según el catálogo configurable."""
    if not base:
        return base
    tokens = base.split("_")
    replaced = [catalog.get(token, token) for token in tokens]
    joined = "_".join(t for t in replaced if t)
    return _MULTI_UNDERSCORE_PATTERN.sub("_", joined).strip("_")


def validate_pattern(base: str) -> bool:
    """Paso 10: valida el patrón `^[A-Z0-9]+(?:_[A-Z0-9]+)*$`."""
    return bool(base) and bool(NAME_PATTERN.match(base))


def sanitize_token(text: str) -> str:
    """Sanitiza un componente confiable (OBTC, etc.) con las mismas reglas de
    caracteres que el nombre base, sin inventar ni truncar su contenido."""
    return sanitize_base(text)


def compose_base(
    *,
    descriptive: str,
    obtc_code: str | None = None,
    consecutive: int | None = None,
    version: int | None = None,
    date: str | None = None,
) -> str:
    """Paso 9: compone el nombre en el orden OBTC, descriptivo, consecutivo,
    versión, fecha (la fecha siempre al final). Ningún componente se inventa:
    deben provenir de metadatos confiables o quedar en `None`."""
    parts: list[str] = []
    if obtc_code:
        parts.append(sanitize_token(obtc_code))
    if descriptive:
        parts.append(descriptive)
    if consecutive is not None:
        parts.append(f"{consecutive:02d}")
    if version is not None:
        parts.append(f"V{version:02d}")
    if date:
        parts.append(date)
    joined = "_".join(p for p in parts if p)
    return _MULTI_UNDERSCORE_PATTERN.sub("_", joined).strip("_")


@dataclass(frozen=True, slots=True)
class NormalizedName:
    base: str
    extension: str
    valid: bool
    needs_ai: bool

    @property
    def full_name(self) -> str:
        return f"{self.base}.{self.extension}" if self.extension else self.base


def normalize_name(
    original_name: str,
    *,
    abbreviations: dict[str, str] | None = None,
    obtc_code: str | None = None,
    consecutive: int | None = None,
    version: int | None = None,
    date: str | None = None,
    is_folder: bool = False,
) -> NormalizedName:
    """Ejecuta los pasos 1-11 del pipeline de normalización (sección 5.2).

    No llama a IA: solo produce el nombre determinista y marca `needs_ai`
    cuando el resultado compuesto supera `MAX_BASE_LENGTH`.
    """
    abbreviations = abbreviations or {}
    if is_folder:
        # Las carpetas no tienen extensión: un "." en su nombre (p. ej.
        # "1. Preoperativo") no debe interpretarse como separador de
        # extensión de archivo.
        raw_base, extension = original_name, ""
    else:
        raw_base, extension = split_base_and_extension(original_name)

    descriptive = sanitize_base(raw_base)
    descriptive = apply_abbreviations(descriptive, abbreviations)

    base = compose_base(
        descriptive=descriptive,
        obtc_code=obtc_code,
        consecutive=consecutive,
        version=version,
        date=date,
    )

    valid = validate_pattern(base)
    needs_ai = len(base) > MAX_BASE_LENGTH

    return NormalizedName(base=base, extension=extension, valid=valid, needs_ai=needs_ai)


@dataclass(frozen=True, slots=True)
class CollisionResolution:
    final_base: str
    suffix_used: str | None
    requires_review: bool


def resolve_collision(
    candidate_base: str,
    existing_bases: set[str],
    *,
    max_length: int = MAX_BASE_LENGTH,
) -> CollisionResolution:
    """Resuelve colisiones de forma determinista y estable entre ejecuciones
    (sección 5.4).

    1. Si no hay colisión, devuelve el candidato sin cambios.
    2. Intenta sufijos consecutivos `_01`.._99`, reservando espacio dentro
       del límite de longitud.
    3. Si hay más de 99 colisiones o no cabe un sufijo válido, usa un sufijo
       corto derivado de una huella estable y marca `requires_review=True`.

    Nunca sobrescribe: el llamador debe usar el resultado en lugar del
    candidato original cuando hay colisión.
    """
    if candidate_base not in existing_bases:
        return CollisionResolution(final_base=candidate_base, suffix_used=None, requires_review=False)

    for n in range(1, 100):
        suffix = f"_{n:02d}"
        available = max_length - len(suffix)
        if available < 1:
            break
        trimmed = candidate_base[:available].rstrip("_")
        candidate = f"{trimmed}{suffix}"
        if candidate not in existing_bases:
            return CollisionResolution(final_base=candidate, suffix_used=suffix, requires_review=False)

    fingerprint = hashlib.sha256(candidate_base.encode("utf-8")).hexdigest()[:6].upper()
    suffix = f"_{fingerprint}"
    available = max(max_length - len(suffix), 0)
    trimmed = candidate_base[:available].rstrip("_")
    candidate = f"{trimmed}{suffix}" if trimmed else fingerprint[:max_length]
    return CollisionResolution(final_base=candidate, suffix_used=suffix, requires_review=True)


def deterministic_fallback(base: str, *, max_length: int = MAX_BASE_LENGTH) -> str:
    """Abreviación determinista segura usada cuando la IA falla o su salida
    sigue siendo inválida tras el reintento (sección 6.5, punto 5)."""
    truncated = base[:max_length].rstrip("_")
    return truncated or "SIN_NOMBRE"


def load_abbreviations(path: str | Path) -> dict[str, str]:
    """Carga el catálogo de abreviaturas desde YAML. Claves y valores se
    normalizan a MAYÚSCULAS para que coincidan con los tokens ya sanitizados."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {str(key).upper(): str(value).upper() for key, value in data.items()}


class NamingRulesEngine:
    """Fachada con estado (catálogo de abreviaturas ya cargado) sobre las
    funciones puras de este módulo."""

    def __init__(self, abbreviations: dict[str, str] | None = None):
        self._abbreviations = abbreviations or {}

    @property
    def abbreviations(self) -> dict[str, str]:
        return self._abbreviations

    @classmethod
    def from_yaml(cls, path: str | Path) -> "NamingRulesEngine":
        return cls(load_abbreviations(path))

    def normalize(
        self,
        original_name: str,
        *,
        obtc_code: str | None = None,
        consecutive: int | None = None,
        version: int | None = None,
        date: str | None = None,
        is_folder: bool = False,
    ) -> NormalizedName:
        return normalize_name(
            original_name,
            abbreviations=self._abbreviations,
            obtc_code=obtc_code,
            consecutive=consecutive,
            version=version,
            date=date,
            is_folder=is_folder,
        )

    def resolve_collision(
        self, candidate_base: str, existing_bases: set[str], *, max_length: int = MAX_BASE_LENGTH
    ) -> CollisionResolution:
        return resolve_collision(candidate_base, existing_bases, max_length=max_length)
