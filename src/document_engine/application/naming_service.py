from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from document_engine.adapters.database.models import JournalEvent, NameDecision
from document_engine.adapters.database.models import MigrationItem as MigrationItemModel
from document_engine.domain.enums import MigrationItemState, RenameMethod
from document_engine.domain.errors import DocumentEngineError
from document_engine.domain.naming_rules import (
    MAX_BASE_LENGTH,
    NAME_PATTERN,
    NamingRulesEngine,
    deterministic_fallback,
    sanitize_token,
)
from document_engine.domain.state_machine import transition
from document_engine.ports.ai_naming_provider import AINamingProviderPort, AINamingRequest

MAX_VALIDATION_ATTEMPTS = 2  # 1 intento + 1 reintento (sección 6.5, punto 4)


def compute_prompt_fingerprint(
    *,
    item_type: str,
    original_name: str,
    normalized_name: str,
    extension_or_mime: str,
    ancestor_path: str,
    obtc_code: str | None,
    date: str | None,
    version: str | None,
    category: str | None,
    abbreviation_catalog: dict[str, str],
) -> str:
    payload = {
        "item_type": item_type,
        "original_name": original_name,
        "normalized_name": normalized_name,
        "extension_or_mime": extension_or_mime,
        "ancestor_path": ancestor_path,
        "obtc_code": obtc_code,
        "date": date,
        "version": version,
        "category": category,
        "abbreviation_catalog": sorted(abbreviation_catalog.items()),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_ai_suggestion(suggestion: str, *, obtc_code: str | None, date: str | None) -> list[str]:
    """Validación posterior obligatoria (sección 6.5): nunca se confía
    directamente en la salida del modelo."""
    errors: list[str] = []
    if not (1 <= len(suggestion) <= MAX_BASE_LENGTH):
        errors.append(f"longitud inválida ({len(suggestion)} caracteres, máximo {MAX_BASE_LENGTH})")
    if not NAME_PATTERN.match(suggestion):
        errors.append(f"no cumple el patrón {NAME_PATTERN.pattern}")
    if obtc_code:
        expected_prefix = sanitize_token(obtc_code)
        if not (suggestion == expected_prefix or suggestion.startswith(f"{expected_prefix}_")):
            errors.append("no conserva el código OBTC al inicio")
    if date:
        if not suggestion.endswith(date):
            errors.append("no conserva la fecha al final")
    return errors


class NamingAssistantService:
    """Orquesta la asistencia de IA para nombres que superan 25 caracteres
    (sección 6). Nunca confía en la salida del modelo sin validarla, cachea
    decisiones por huella estable y aplica un fallback determinista cuando
    la IA falla o su salida sigue siendo inválida."""

    def __init__(
        self,
        db: Session,
        ai_provider: AINamingProviderPort,
        naming_engine: NamingRulesEngine,
        *,
        ai_model_name: str = "gpt-4o-mini",
    ):
        self._db = db
        self._ai_provider = ai_provider
        self._naming = naming_engine
        self._ai_model_name = ai_model_name

    def _cached_decision(self, fingerprint: str) -> NameDecision | None:
        stmt = (
            select(NameDecision)
            .where(NameDecision.input_fingerprint == fingerprint)
            .where(NameDecision.method == RenameMethod.AI_ASSISTED.value)
            .where(NameDecision.requires_review.is_(False))
            .order_by(NameDecision.created_at.desc())
        )
        return self._db.execute(stmt).scalars().first()

    def _existing_sibling_bases(self, item: MigrationItemModel, extension: str) -> set[str]:
        if not item.planned_destination_path or "/" not in item.planned_destination_path:
            parent_path = ""
        else:
            parent_path = item.planned_destination_path.rsplit("/", 1)[0]

        siblings = (
            self._db.execute(
                select(MigrationItemModel)
                .where(MigrationItemModel.batch_id == item.batch_id)
                .where(MigrationItemModel.id != item.id)
            )
            .scalars()
            .all()
        )
        bases: set[str] = set()
        for sibling in siblings:
            if not sibling.planned_destination_name:
                continue
            sibling_parent = (
                sibling.planned_destination_path.rsplit("/", 1)[0]
                if sibling.planned_destination_path and "/" in sibling.planned_destination_path
                else ""
            )
            if sibling_parent != parent_path:
                continue
            sibling_ext = sibling.extension or ""
            if sibling_ext != extension:
                continue
            base = sibling.planned_destination_name.rsplit(".", 1)[0] if sibling_ext else sibling.planned_destination_name
            bases.add(base)
        return bases

    def resolve_item(
        self,
        item_id: str,
        *,
        obtc_code: str | None = None,
        date: str | None = None,
        version: str | None = None,
        category: str | None = None,
        local_context: str = "",
        force: bool = False,
    ) -> MigrationItemModel:
        item = self._db.get(MigrationItemModel, item_id)
        if item is None:
            raise DocumentEngineError(f"Elemento {item_id} no existe", code="NOT_FOUND")

        if not force and item.rename_method != RenameMethod.AI_ASSISTED.value:
            raise DocumentEngineError(
                "regenerate-ai-name solo aplica a nombres que superan 25 caracteres, salvo orden explícita",
                code="NAME_AI_NOT_APPLICABLE",
            )

        normalized = self._naming.normalize(item.source_name, obtc_code=obtc_code, date=date)
        extension = item.extension or normalized.extension
        # Ruta de ancestros de origen: solo para dar contexto al modelo y la huella de caché.
        ancestor_path = item.source_path.rsplit("/", 1)[0] if "/" in item.source_path else ""
        # Carpeta destino ya planificada: es la que se usa para reconstruir la ruta final.
        dest_parent_path = (
            item.planned_destination_path.rsplit("/", 1)[0]
            if item.planned_destination_path and "/" in item.planned_destination_path
            else ""
        )

        fingerprint = compute_prompt_fingerprint(
            item_type=item.item_type,
            original_name=item.source_name,
            normalized_name=normalized.base,
            extension_or_mime=extension or (item.source_mime_type or ""),
            ancestor_path=ancestor_path,
            obtc_code=obtc_code,
            date=date,
            version=version,
            category=category,
            abbreviation_catalog=self._naming.abbreviations,
        )

        cached = None if force else self._cached_decision(fingerprint)
        tokens_used = None
        fallback_reason = None
        ai_reason = None
        ai_confidence = None
        requires_review = False

        if cached is not None:
            final_base = cached.suggested_name.rsplit(".", 1)[0] if extension else cached.suggested_name
            ai_reason = cached.ai_reason
            ai_confidence = cached.ai_confidence
        else:
            suggestion_text = ""
            previous_errors: tuple[str, ...] = ()
            for attempt in range(1, MAX_VALIDATION_ATTEMPTS + 1):
                request = AINamingRequest(
                    item_type=item.item_type,
                    original_name=item.source_name,
                    normalized_name=normalized.base,
                    extension_or_mime=extension or (item.source_mime_type or ""),
                    ancestor_path=ancestor_path,
                    local_context=local_context,
                    obtc_code=obtc_code,
                    date=date,
                    version=version,
                    category=category,
                    abbreviation_catalog=self._naming.abbreviations,
                    previous_errors=previous_errors,
                )
                try:
                    response = self._ai_provider.suggest_name(request)
                except DocumentEngineError as exc:
                    fallback_reason = f"AI_PROVIDER_ERROR:{exc.code}"
                    suggestion_text = ""
                    break

                validation_errors = validate_ai_suggestion(
                    response.suggested_name, obtc_code=obtc_code, date=date
                )
                tokens_used = response.tokens_used
                ai_reason = response.reason
                ai_confidence = response.confidence
                if not validation_errors:
                    suggestion_text = response.suggested_name
                    requires_review = response.requires_review
                    break
                previous_errors = tuple(validation_errors)
                if attempt == MAX_VALIDATION_ATTEMPTS:
                    fallback_reason = "NAME_AI_INVALID_OUTPUT:" + "; ".join(validation_errors)

            if not suggestion_text:
                suggestion_text = deterministic_fallback(normalized.base)
                requires_review = True
                fallback_reason = fallback_reason or "NAME_AI_INVALID_OUTPUT"

            final_base = suggestion_text

        existing_bases = self._existing_sibling_bases(item, extension)
        resolution = self._naming.resolve_collision(final_base, existing_bases)
        method = RenameMethod.AI_ASSISTED
        if resolution.suffix_used:
            method = RenameMethod.COLLISION_RESOLUTION
        if resolution.requires_review:
            requires_review = True

        final_name = f"{resolution.final_base}.{extension}" if extension else resolution.final_base
        final_path = f"{dest_parent_path}/{final_name}" if dest_parent_path else final_name

        previous_state = item.state
        item.planned_destination_name = final_name
        item.planned_destination_path = final_path
        item.rename_method = method.value
        if requires_review:
            if item.state != MigrationItemState.WAITING_REVIEW.value:
                item.state = transition(MigrationItemState(item.state), MigrationItemState.WAITING_REVIEW).value
        elif item.state == MigrationItemState.WAITING_REVIEW.value:
            item.state = transition(MigrationItemState.WAITING_REVIEW, MigrationItemState.READY).value

        if cached is not None:
            ai_model = cached.ai_model
        elif fallback_reason:
            ai_model = None
        else:
            ai_model = self._ai_model_name

        self._db.add(
            NameDecision(
                migration_item_id=item.id,
                method=method.value,
                input_fingerprint=fingerprint,
                suggested_name=final_name,
                ai_model=ai_model,
                ai_reason=ai_reason,
                ai_confidence=ai_confidence,
                requires_review=requires_review,
                fallback_reason=fallback_reason,
            )
        )
        self._db.add(
            JournalEvent(
                batch_id=item.batch_id,
                migration_item_id=item.id,
                event_type="NAME_AI_RESOLVED",
                previous_state=previous_state,
                new_state=item.state,
                operation="AI_SUGGEST",
                result="FALLBACK" if fallback_reason else "OK",
                original_name=item.source_name,
                final_name=final_name,
                name_decision_source=method.value,
                error_code=fallback_reason,
                metadata_json={"tokens_used": tokens_used, "cached": cached is not None},
            )
        )
        self._db.commit()
        return item
