from __future__ import annotations

from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from document_engine.adapters.database.models import BatchSelector as BatchSelectorModel
from document_engine.adapters.database.models import JournalEvent
from document_engine.adapters.database.models import MigrationBatch as MigrationBatchModel
from document_engine.adapters.database.models import MigrationItem as MigrationItemModel
from document_engine.adapters.database.models import MigrationPlan as MigrationPlanModel
from document_engine.adapters.database.models import NameDecision
from document_engine.adapters.database.models import RepositoryItem as RepositoryItemModel
from document_engine.domain.enums import (
    ItemType,
    MigrationItemState,
    PlannedAction,
    Priority,
    RenameMethod,
    SelectorKind,
)
from document_engine.domain.enums import PRIORITY_VALUES
from document_engine.domain.errors import PermanentError
from document_engine.domain.idempotency import compute_idempotency_key
from document_engine.domain.naming_rules import (
    NAME_PATTERN,
    MAX_BASE_LENGTH,
    NamingRulesEngine,
    split_base_and_extension,
)
from document_engine.domain.state_machine import transition

GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
COMPRESSED_EXTENSIONS = {"zip", "rar"}


def normalize_priority(value: "Priority | int") -> int:
    if isinstance(value, Priority):
        return PRIORITY_VALUES[value]
    value = int(value)
    if not 0 <= value <= 100:
        raise PermanentError(f"La prioridad debe estar entre 0 y 100, recibido {value}")
    return value


def load_export_formats(path: str | Path) -> dict[str, dict[str, str]]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return data


class BatchService:
    """Crea lotes (`MigrationBatch`) y administra sus selectores."""

    def __init__(self, db: Session):
        self._db = db

    def create_batch(
        self,
        *,
        snapshot_id: str,
        name: str,
        priority: "Priority | int" = Priority.NORMAL,
        destination_base_path: str | None = None,
    ) -> MigrationBatchModel:
        batch = MigrationBatchModel(
            snapshot_id=snapshot_id,
            name=name,
            priority=normalize_priority(priority),
            status="DRAFT",
            destination_base_path=(destination_base_path or "").strip("/") or None,
        )
        self._db.add(batch)
        self._db.commit()
        return batch

    def add_selector(
        self,
        batch_id: str,
        *,
        kind: SelectorKind,
        value: str,
        include: bool = True,
        priority: "Priority | int | None" = None,
    ) -> BatchSelectorModel:
        selector = BatchSelectorModel(
            batch_id=batch_id,
            kind=kind.value,
            value=value,
            include=include,
            priority=normalize_priority(priority) if priority is not None else None,
        )
        self._db.add(selector)
        self._db.commit()
        return selector

    def remove_selector(self, selector_id: str) -> None:
        selector = self._db.get(BatchSelectorModel, selector_id)
        if selector is not None:
            self._db.delete(selector)
            self._db.commit()

    def set_batch_priority(self, batch_id: str, priority: "Priority | int") -> MigrationBatchModel:
        batch = self._db.get(MigrationBatchModel, batch_id)
        if batch is None:
            raise PermanentError(f"Lote {batch_id} no existe")
        if batch.status not in ("DRAFT", "PLANNED", "PAUSED"):
            raise PermanentError("Solo se puede cambiar la prioridad antes de iniciar o mientras está en pausa")
        batch.priority = normalize_priority(priority)
        self._db.commit()
        return batch


class PlanningService:
    """Resuelve selecciones, ordena por prioridad+DFS y genera el
    `MigrationPlan` sin escribir en ningún repositorio (dry-run por diseño:
    Planning nunca toca el destino)."""

    def __init__(
        self,
        db: Session,
        naming_engine: NamingRulesEngine,
        *,
        export_formats: dict[str, dict[str, str]] | None = None,
        block_compressed: bool = False,
    ):
        self._db = db
        self._naming = naming_engine
        self._export_formats = export_formats or {}
        self._block_compressed = block_compressed

    # ---- selección -----------------------------------------------------

    def _snapshot_items(self, snapshot_id: str) -> dict[str, RepositoryItemModel]:
        stmt = select(RepositoryItemModel).where(RepositoryItemModel.snapshot_id == snapshot_id)
        items = self._db.execute(stmt).scalars().all()
        return {item.source_item_id: item for item in items}

    @staticmethod
    def _children_map(items_by_id: dict[str, RepositoryItemModel]) -> dict[str, list[str]]:
        children: dict[str, list[str]] = {}
        for item in items_by_id.values():
            if item.parent_source_id:
                children.setdefault(item.parent_source_id, []).append(item.source_item_id)
        return children

    @staticmethod
    def _descendants(root_id: str, children_map: dict[str, list[str]]) -> set[str]:
        result: set[str] = set()
        stack = [root_id]
        while stack:
            current = stack.pop()
            for child_id in children_map.get(current, []):
                if child_id not in result:
                    result.add(child_id)
                    stack.append(child_id)
        return result

    def _resolve_selector_ids(
        self,
        selector: BatchSelectorModel,
        items_by_id: dict[str, RepositoryItemModel],
        children_map: dict[str, list[str]],
    ) -> set[str]:
        kind = SelectorKind(selector.kind)
        if kind in (SelectorKind.EXPLICIT_IDS, SelectorKind.SEARCH_RESULT):
            ids = {v.strip() for v in selector.value.split(",") if v.strip()}
            return {i for i in ids if i in items_by_id}
        if kind == SelectorKind.FOLDER_RECURSIVE:
            folder_id = selector.value.strip()
            if folder_id not in items_by_id:
                return set()
            return {folder_id} | self._descendants(folder_id, children_map)
        if kind == SelectorKind.PATH_PREFIX:
            prefix = selector.value
            return {
                item.source_item_id
                for item in items_by_id.values()
                if item.logical_path.startswith(prefix)
            }
        return set()

    def resolve_selection(self, batch: MigrationBatchModel) -> tuple[dict[str, int], set[str]]:
        """Devuelve (prioridad_por_id, ids_de_carpetas_ancestras_implícitas)."""
        items_by_id = self._snapshot_items(batch.snapshot_id)
        children_map = self._children_map(items_by_id)

        selectors = (
            self._db.execute(select(BatchSelectorModel).where(BatchSelectorModel.batch_id == batch.id))
            .scalars()
            .all()
        )

        priority_by_id: dict[str, int] = {}
        excluded_ids: set[str] = set()

        for selector in selectors:
            ids = self._resolve_selector_ids(selector, items_by_id, children_map)
            if not selector.include:
                excluded_ids |= ids
                continue
            selector_priority = selector.priority if selector.priority is not None else batch.priority
            for item_id in ids:
                priority_by_id[item_id] = max(priority_by_id.get(item_id, 0), selector_priority)

        for item_id in excluded_ids:
            priority_by_id.pop(item_id, None)

        # Un archivo seleccionado incluye implícitamente sus carpetas ancestras.
        implicit_folder_ids: set[str] = set()
        for item_id in list(priority_by_id.keys()):
            item = items_by_id[item_id]
            parent_id = item.parent_source_id
            while parent_id and parent_id in items_by_id and parent_id not in excluded_ids:
                if parent_id not in priority_by_id:
                    implicit_folder_ids.add(parent_id)
                    priority_by_id[parent_id] = priority_by_id[item_id]
                parent_id = items_by_id[parent_id].parent_source_id

        return priority_by_id, implicit_folder_ids

    @staticmethod
    def order_by_priority_dfs(
        priority_by_id: dict[str, int], items_by_id: dict[str, RepositoryItemModel]
    ) -> list[str]:
        """Mayor prioridad primero; dentro de la misma prioridad, DFS estable
        (el orden lexicográfico de `logical_path` completa una rama antes de
        pasar a la siguiente, porque el separador `/` ordena antes que
        cualquier carácter alfanumérico)."""
        return sorted(
            priority_by_id.keys(),
            key=lambda item_id: (-priority_by_id[item_id], items_by_id[item_id].logical_path),
        )

    # ---- planning --------------------------------------------------------

    def _resolve_export(self, mime_type: str | None) -> tuple[str | None, str | None]:
        """Devuelve (extensión_destino, warning). Si el mime es nativo de
        Google Workspace sin formato configurado, retorna (None, warning)."""
        if not mime_type or not mime_type.startswith("application/vnd.google-apps"):
            return None, None
        if mime_type in (GOOGLE_FOLDER_MIME, GOOGLE_SHORTCUT_MIME):
            return None, None
        entry = self._export_formats.get(mime_type)
        if entry is None:
            return None, "DRIVE_EXPORT_UNSUPPORTED"
        return entry["extension"], None

    def generate_plan(self, batch_id: str) -> MigrationPlanModel:
        batch = self._db.get(MigrationBatchModel, batch_id)
        if batch is None:
            raise PermanentError(f"Lote {batch_id} no existe")

        items_by_id = self._snapshot_items(batch.snapshot_id)
        priority_by_id, _ = self.resolve_selection(batch)
        ordered_ids = self.order_by_priority_dfs(priority_by_id, items_by_id)

        existing_items = {
            mi.idempotency_key: mi
            for mi in self._db.execute(
                select(MigrationItemModel).where(MigrationItemModel.batch_id == batch_id)
            )
            .scalars()
            .all()
        }

        destination_path_by_source_id: dict[str, str] = {}
        used_names_by_parent: dict[str, dict[str, set[str]]] = {}

        if batch.destination_base_path:
            # Ruta ya existente en el FTP elegida por el usuario (sección de
            # selección visual): se usa tal cual, sin pasar por el motor de
            # normalización de nombres — a diferencia de las carpetas que sí
            # vienen de Drive, esta ya existe en el servidor con ese nombre
            # exacto, y normalizarla crearía una carpeta distinta en vez de
            # escribir dentro de la elegida.
            destination_path_by_source_id[""] = batch.destination_base_path

        for source_id in ordered_ids:
            item = items_by_id[source_id]
            priority = priority_by_id[source_id]
            parent_dest_path = destination_path_by_source_id.get(item.parent_source_id or "")

            warnings: list[str] = []
            action = PlannedAction.CREATE_FOLDER if item.item_type == ItemType.FOLDER.value else PlannedAction.DOWNLOAD
            state = MigrationItemState.PLANNED
            rename_method = RenameMethod.RULE_BASED
            extension = ""

            if item.item_type == ItemType.SHORTCUT.value:
                action = PlannedAction.BLOCK
                state = MigrationItemState.BLOCKED
                warnings.append("Los accesos directos no se resuelven en este MVP")
            elif item.item_type == ItemType.FILE.value:
                export_ext, export_warning = self._resolve_export(item.mime_type)
                if export_warning:
                    action = PlannedAction.BLOCK
                    state = MigrationItemState.BLOCKED
                    warnings.append(export_warning)
                elif export_ext is not None:
                    action = PlannedAction.EXPORT
                    extension = export_ext
                else:
                    _, extension = split_base_and_extension(item.name)
                    if not item.can_download:
                        action = PlannedAction.BLOCK
                        state = MigrationItemState.BLOCKED
                        warnings.append("DRIVE_PERMISSION_DENIED")
                    elif extension in COMPRESSED_EXTENSIONS and self._block_compressed:
                        action = PlannedAction.BLOCK
                        state = MigrationItemState.BLOCKED
                        warnings.append("Archivos comprimidos no soportados como soporte documental")

            normalized = self._naming.normalize(item.name, is_folder=item.item_type == ItemType.FOLDER.value)
            if extension:
                normalized_full_ext = extension
            else:
                normalized_full_ext = normalized.extension

            if action == PlannedAction.BLOCK:
                destination_path_by_source_id[source_id] = parent_dest_path or ""
                planned_name = None
                planned_path = None
            else:
                if normalized.needs_ai:
                    state = MigrationItemState.WAITING_REVIEW
                    rename_method = RenameMethod.AI_ASSISTED
                    warnings.append("Nombre supera 25 caracteres: requiere asistencia de IA (sprint 4)")
                    candidate_base = normalized.base[:25]
                else:
                    candidate_base = normalized.base
                    if candidate_base == (item.name.rsplit(".", 1)[0] if "." in item.name else item.name):
                        rename_method = RenameMethod.UNCHANGED

                if not candidate_base:
                    candidate_base = "SIN_NOMBRE"
                    warnings.append("El nombre normalizado quedó vacío; requiere revisión manual")
                    state = MigrationItemState.WAITING_REVIEW

                parent_key = parent_dest_path or ""
                registry = used_names_by_parent.setdefault(parent_key, {})
                existing_bases = registry.setdefault(normalized_full_ext, set())
                resolution = self._naming.resolve_collision(candidate_base, existing_bases)
                existing_bases.add(resolution.final_base)
                if resolution.suffix_used:
                    rename_method = RenameMethod.COLLISION_RESOLUTION
                    warnings.append(f"Colisión resuelta con sufijo {resolution.suffix_used}")
                if resolution.requires_review:
                    state = MigrationItemState.WAITING_REVIEW
                    warnings.append("Colisión no resuelta de forma determinista: requiere revisión")

                planned_name = (
                    f"{resolution.final_base}.{normalized_full_ext}" if normalized_full_ext else resolution.final_base
                )
                planned_path = f"{parent_dest_path}/{planned_name}" if parent_dest_path else planned_name
                destination_path_by_source_id[source_id] = planned_path

            idempotency_key = compute_idempotency_key(
                snapshot_id=batch.snapshot_id,
                source_provider="google_drive",
                source_item_id=source_id,
                source_version_or_modified_time=str(item.modified_time),
                planned_destination_path=planned_path or "",
                export_format=extension,
            )

            if idempotency_key in existing_items:
                existing = existing_items[idempotency_key]
                if existing.state in (MigrationItemState.DISCOVERED.value, MigrationItemState.PLANNED.value, MigrationItemState.WAITING_REVIEW.value):
                    existing.priority = priority
                continue

            migration_item = MigrationItemModel(
                batch_id=batch_id,
                source_item_id=source_id,
                source_path=item.logical_path,
                source_name=item.name,
                source_mime_type=item.mime_type,
                source_size=item.size,
                source_version=str(item.modified_time) if item.modified_time else None,
                item_type=item.item_type,
                priority=priority,
                planned_destination_path=planned_path,
                planned_destination_name=planned_name,
                extension=normalized_full_ext or None,
                rename_method=rename_method.value,
                state=state.value,
                idempotency_key=idempotency_key,
            )
            self._db.add(migration_item)
            self._db.flush()

            self._db.add(
                NameDecision(
                    migration_item_id=migration_item.id,
                    method=rename_method.value,
                    suggested_name=planned_name,
                    requires_review=state == MigrationItemState.WAITING_REVIEW,
                )
            )
            self._db.add(
                JournalEvent(
                    batch_id=batch_id,
                    migration_item_id=migration_item.id,
                    event_type="ITEM_PLANNED",
                    previous_state=MigrationItemState.DISCOVERED.value,
                    new_state=state.value,
                    operation=action.value,
                    result="OK" if action != PlannedAction.BLOCK else "BLOCKED",
                    original_name=item.name,
                    final_name=planned_name,
                    name_decision_source=rename_method.value,
                    metadata_json={"warnings": warnings} if warnings else {},
                )
            )

        plan_count = self._db.execute(
            select(MigrationPlanModel).where(MigrationPlanModel.batch_id == batch_id)
        ).scalars().all()
        plan = MigrationPlanModel(batch_id=batch_id, version=len(plan_count) + 1)
        self._db.add(plan)
        batch.status = "PLANNED"
        self._db.commit()
        return plan

    # ---- preview / dry-run -------------------------------------------------

    def preview(self, batch_id: str) -> dict:
        items = (
            self._db.execute(select(MigrationItemModel).where(MigrationItemModel.batch_id == batch_id))
            .scalars()
            .all()
        )
        blocked = [i for i in items if i.state == MigrationItemState.BLOCKED.value]
        needs_review = [i for i in items if i.state == MigrationItemState.WAITING_REVIEW.value]
        conflicts = [i for i in items if i.rename_method == RenameMethod.COLLISION_RESOLUTION.value]
        total_size = sum(i.source_size or 0 for i in items)

        return {
            "total_items": len(items),
            "folders": sum(1 for i in items if i.item_type == ItemType.FOLDER.value),
            "files": sum(1 for i in items if i.item_type == ItemType.FILE.value),
            "total_size_bytes": total_size,
            "blocked_count": len(blocked),
            "needs_review_count": len(needs_review),
            "collisions_resolved": len(conflicts),
            "items": [
                {
                    "source_path": i.source_path,
                    "planned_destination_path": i.planned_destination_path,
                    "item_type": i.item_type,
                    "state": i.state,
                    "rename_method": i.rename_method,
                    "priority": i.priority,
                }
                for i in items
            ],
        }


def cascade_folder_rename(db: Session, batch_id: str, old_path: str, new_path: str) -> None:
    """Si una carpeta cambia de ruta destino después de que sus hijos ya
    fueron planificados (revisión manual o asistida por IA), propaga el
    nuevo prefijo a los descendientes ya planificados en el mismo lote.
    Sin esto, quedarían huérfanos apuntando a la ruta vieja de la carpeta."""
    if not old_path or old_path == new_path:
        return
    old_prefix = f"{old_path}/"
    new_prefix = f"{new_path}/"
    descendants = (
        db.execute(
            select(MigrationItemModel).where(
                MigrationItemModel.batch_id == batch_id,
                MigrationItemModel.planned_destination_path.like(f"{old_prefix}%"),
            )
        )
        .scalars()
        .all()
    )
    for descendant in descendants:
        if descendant.planned_destination_path and descendant.planned_destination_path.startswith(old_prefix):
            descendant.planned_destination_path = new_prefix + descendant.planned_destination_path[len(old_prefix) :]


class NameReviewService:
    """Revisión manual de nombres destino: sobrescritura y aprobación,
    ambas auditadas en `NameDecision` y `JournalEvent` (sección 4.4)."""

    def __init__(self, db: Session):
        self._db = db

    def override_destination_name(
        self, item_id: str, new_base_name: str, *, changed_by: str
    ) -> MigrationItemModel:
        item = self._db.get(MigrationItemModel, item_id)
        if item is None:
            raise PermanentError(f"Elemento {item_id} no existe")

        candidate = new_base_name.strip().upper()
        if not (1 <= len(candidate) <= MAX_BASE_LENGTH) or not NAME_PATTERN.match(candidate):
            raise PermanentError(
                f"Nombre inválido: debe cumplir {NAME_PATTERN.pattern} y tener entre 1 y {MAX_BASE_LENGTH} caracteres",
                code="NAME_AI_INVALID_OUTPUT",
            )

        previous_state = item.state
        old_path = item.planned_destination_path
        final_name = f"{candidate}.{item.extension}" if item.extension else candidate
        parent_path = item.planned_destination_path.rsplit("/", 1)[0] if item.planned_destination_path and "/" in item.planned_destination_path else ""
        item.planned_destination_name = final_name
        item.planned_destination_path = f"{parent_path}/{final_name}" if parent_path else final_name
        item.rename_method = RenameMethod.MANUAL_OVERRIDE.value
        if item.item_type == ItemType.FOLDER.value:
            cascade_folder_rename(self._db, item.batch_id, old_path, item.planned_destination_path)
        if item.state == MigrationItemState.WAITING_REVIEW.value:
            item.state = transition(MigrationItemState.WAITING_REVIEW, MigrationItemState.READY).value

        self._db.add(
            NameDecision(
                migration_item_id=item.id,
                method=RenameMethod.MANUAL_OVERRIDE.value,
                suggested_name=final_name,
                requires_review=False,
            )
        )
        self._db.add(
            JournalEvent(
                batch_id=item.batch_id,
                migration_item_id=item.id,
                event_type="NAME_MANUAL_OVERRIDE",
                previous_state=previous_state,
                new_state=item.state,
                operation="RENAME",
                result="OK",
                final_name=final_name,
                name_decision_source=RenameMethod.MANUAL_OVERRIDE.value,
                changed_by_user=changed_by,
            )
        )
        self._db.commit()
        return item

    def approve_name(self, item_id: str, *, changed_by: str) -> MigrationItemModel:
        item = self._db.get(MigrationItemModel, item_id)
        if item is None:
            raise PermanentError(f"Elemento {item_id} no existe")

        previous_state = item.state
        item.state = transition(MigrationItemState(item.state), MigrationItemState.READY).value

        self._db.add(
            JournalEvent(
                batch_id=item.batch_id,
                migration_item_id=item.id,
                event_type="NAME_APPROVED",
                previous_state=previous_state,
                new_state=item.state,
                operation="APPROVE",
                result="OK",
                final_name=item.planned_destination_name,
                changed_by_user=changed_by,
            )
        )
        self._db.commit()
        return item
