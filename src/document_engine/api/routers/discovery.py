from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from document_engine.adapters.database.models import MigrationItem as MigrationItemModel
from document_engine.adapters.database.models import RepositoryItem as RepositoryItemModel
from document_engine.adapters.database.models import RepositorySnapshot as RepositorySnapshotModel
from document_engine.api.dependencies import get_db, get_source_repository, require_api_key
from document_engine.api.schemas import (
    DiscoveryRunCreate,
    DriveBrowseItemOut,
    FileStatusItem,
    MigrationCheckRequest,
    MigrationCheckResponse,
    MigrationCheckStatus,
    RepositoryItemOut,
    SnapshotMigrationStatusOut,
    SnapshotOut,
)
from document_engine.application.discovery_service import DiscoveryService
from document_engine.application.search_service import SnapshotSearchService
from document_engine.domain.enums import ItemType
from document_engine.ports.source_repository import SourceRepositoryPort
from document_engine.settings import Settings, get_settings

router = APIRouter(tags=["discovery"], dependencies=[Depends(require_api_key)])


@router.get("/drive/browse", response_model=list[DriveBrowseItemOut])
def browse_drive(
    folder_id: str | None = None,
    settings: Settings = Depends(get_settings),
    source: SourceRepositoryPort = Depends(get_source_repository),
):
    """Lista en vivo el contenido de una carpeta de Drive, sin persistir
    nada (a diferencia de /discovery-runs). Pensado para poblar un
    explorador de carpetas en la UI sin que el usuario conozca IDs."""
    target = folder_id or settings.google_root_folder_id
    if not target:
        raise HTTPException(400, "GOOGLE_ROOT_FOLDER_ID no configurado y no se indicó folder_id")
    children = [item for item in source.list_children(target) if item.item_type != ItemType.SHORTCUT]
    children.sort(key=lambda item: (item.item_type != ItemType.FOLDER, item.name.lower()))
    return [
        DriveBrowseItemOut(
            id=item.source_item_id,
            name=item.name,
            type=item.item_type.value,
            mime_type=item.mime_type,
            size=item.size,
        )
        for item in children
    ]


@router.post("/discovery-runs", response_model=SnapshotOut)
def create_discovery_run(
    payload: DiscoveryRunCreate,
    db: Session = Depends(get_db),
    source: SourceRepositoryPort = Depends(get_source_repository),
) -> RepositorySnapshotModel:
    """Ejecuta el discovery de forma síncrona (MVP). Para repositorios muy
    grandes, usar `scripts/run_worker.py` o un job en segundo plano en lugar
    de esta llamada HTTP bloqueante."""
    service = DiscoveryService(source, db)
    if payload.folder_ids:
        return service.run_partial_snapshot(payload.folder_ids)
    if not payload.root_folder_id:
        raise HTTPException(400, "Debe indicar root_folder_id o folder_ids")
    return service.run_full_snapshot(payload.root_folder_id)


@router.post("/discovery-runs/{run_id}/pause")
def pause_discovery_run(run_id: str) -> None:
    raise HTTPException(501, "Discovery corre de forma síncrona en este MVP; pausar no aplica")


@router.post("/discovery-runs/{run_id}/resume")
def resume_discovery_run(run_id: str) -> None:
    raise HTTPException(501, "Discovery corre de forma síncrona en este MVP; resumir no aplica")


@router.get("/discovery-runs/{run_id}", response_model=SnapshotOut)
def get_discovery_run(run_id: str, db: Session = Depends(get_db)) -> RepositorySnapshotModel:
    snapshot = db.get(RepositorySnapshotModel, run_id)
    if snapshot is None:
        raise HTTPException(404, "No encontrado")
    return snapshot


@router.get("/snapshots", response_model=list[SnapshotOut])
def list_snapshots(db: Session = Depends(get_db), limit: int = Query(default=50, le=200), offset: int = 0):
    stmt = select(RepositorySnapshotModel).order_by(RepositorySnapshotModel.started_at.desc()).offset(offset).limit(limit)
    return db.execute(stmt).scalars().all()


@router.get("/snapshots/{snapshot_id}", response_model=SnapshotOut)
def get_snapshot(snapshot_id: str, db: Session = Depends(get_db)) -> RepositorySnapshotModel:
    snapshot = db.get(RepositorySnapshotModel, snapshot_id)
    if snapshot is None:
        raise HTTPException(404, "No encontrado")
    return snapshot


@router.get("/snapshots/{snapshot_id}/items/search", response_model=list[RepositoryItemOut])
def search_snapshot_items(
    snapshot_id: str,
    db: Session = Depends(get_db),
    text: str | None = None,
    path_prefix: str | None = None,
    item_type: str | None = None,
    mime_type: str | None = None,
    parent_source_id: str | None = None,
    limit: int = Query(default=100, le=500),
    offset: int = 0,
):
    service = SnapshotSearchService(db)
    return service.search(
        snapshot_id,
        text=text,
        path_prefix=path_prefix,
        item_type=item_type,
        mime_type=mime_type,
        parent_source_id=parent_source_id,
        limit=limit,
        offset=offset,
    )


@router.get("/snapshots/{snapshot_id}/migration-status", response_model=SnapshotMigrationStatusOut)
def snapshot_migration_status(
    snapshot_id: str,
    db: Session = Depends(get_db),
    status_filter: str | None = Query(default=None, description="COMPLETED | IN_PROGRESS | PENDING"),
    item_type_filter: str | None = Query(default=None, alias="item_type"),
    search: str | None = Query(default=None, description="Busca en nombre o ruta"),
    limit: int = Query(default=200, le=1000),
    offset: int = 0,
) -> SnapshotMigrationStatusOut:
    """Devuelve qué archivos del snapshot ya se migraron y cuáles faltan.

    La comparación usa los nombres/rutas originales de Drive (source_path,
    source_name), no el nombre de destino renombrado. Soporta seleccionar un
    drive con la misma estructura: si hay una migración COMPLETED en cualquier
    batch con la misma ruta original, el archivo se marca como ya migrado
    (match_type=by_path).
    """
    snapshot = db.get(RepositorySnapshotModel, snapshot_id)
    if snapshot is None:
        raise HTTPException(404, "Snapshot no encontrado")

    # 1. Fetch all repository items for this snapshot
    ri_stmt = (
        select(RepositoryItemModel)
        .where(RepositoryItemModel.snapshot_id == snapshot_id)
        .order_by(RepositoryItemModel.logical_path)
    )
    repo_items = db.execute(ri_stmt).scalars().all()

    if not repo_items:
        return SnapshotMigrationStatusOut(
            snapshot_id=snapshot_id,
            total_items=0,
            total_files=0,
            total_folders=0,
            completed=0,
            in_progress=0,
            pending=0,
            percent_completed=0.0,
            items=[],
        )

    # 2. Build lookup structures
    source_ids = [ri.source_item_id for ri in repo_items]
    logical_paths = [ri.logical_path for ri in repo_items]

    # 3. Query migration_items for direct ID matches (any state)
    mi_direct_stmt = (
        select(MigrationItemModel)
        .where(MigrationItemModel.source_item_id.in_(source_ids))
    )
    mi_direct_rows = db.execute(mi_direct_stmt).scalars().all()

    # Best direct match per source_item_id: prefer COMPLETED, then latest updated_at
    direct_by_id: dict[str, MigrationItemModel] = {}
    for mi in mi_direct_rows:
        existing = direct_by_id.get(mi.source_item_id)
        if existing is None:
            direct_by_id[mi.source_item_id] = mi
        elif mi.state == "COMPLETED" and existing.state != "COMPLETED":
            direct_by_id[mi.source_item_id] = mi
        elif mi.state == existing.state and mi.updated_at > existing.updated_at:
            direct_by_id[mi.source_item_id] = mi

    # 4. Query COMPLETED migration_items for path matches (same logical_path,
    #    different source_item_id — handles same-structure drives)
    mi_path_stmt = (
        select(MigrationItemModel)
        .where(
            MigrationItemModel.source_path.in_(logical_paths),
            MigrationItemModel.state == "COMPLETED",
        )
    )
    mi_path_rows = db.execute(mi_path_stmt).scalars().all()

    # Best path match per logical_path: latest completed_at
    path_by_logical: dict[str, MigrationItemModel] = {}
    for mi in mi_path_rows:
        existing = path_by_logical.get(mi.source_path)
        if existing is None or (mi.completed_at and (not existing.completed_at or mi.completed_at > existing.completed_at)):
            path_by_logical[mi.source_path] = mi

    # 5. Compute status for each repo item
    all_items: list[FileStatusItem] = []
    counts = {"COMPLETED": 0, "IN_PROGRESS": 0, "PENDING": 0}
    type_counts = {"FILE": 0, "FOLDER": 0, "SHORTCUT": 0}

    for ri in repo_items:
        itype = ri.item_type.upper() if isinstance(ri.item_type, str) else str(ri.item_type).upper()
        type_counts[itype] = type_counts.get(itype, 0) + 1

        direct_mi = direct_by_id.get(ri.source_item_id)
        path_mi = path_by_logical.get(ri.logical_path)

        # Determine status
        if direct_mi is not None and direct_mi.state == "COMPLETED":
            status = "COMPLETED"
            match_type = "direct"
            best_mi = direct_mi
        elif path_mi is not None and (direct_mi is None or direct_mi.source_item_id != path_mi.source_item_id):
            # Path-matched COMPLETED from a different drive
            status = "COMPLETED"
            match_type = "by_path"
            best_mi = path_mi
        elif direct_mi is not None:
            status = "IN_PROGRESS"
            match_type = "direct"
            best_mi = direct_mi
        else:
            status = "PENDING"
            match_type = None
            best_mi = None

        counts[status] = counts.get(status, 0) + 1

        all_items.append(FileStatusItem(
            source_item_id=ri.source_item_id,
            name=ri.name,
            item_type=itype,
            logical_path=ri.logical_path,
            size=ri.size,
            migration_status=status,
            match_type=match_type,
            migration_item_id=best_mi.id if best_mi else None,
            migration_state=best_mi.state if best_mi else None,
            batch_id=best_mi.batch_id if best_mi else None,
            completed_at=best_mi.completed_at if best_mi else None,
        ))

    total = len(all_items)
    total_files = type_counts.get("FILE", 0) + type_counts.get("SHORTCUT", 0)
    total_folders = type_counts.get("FOLDER", 0)
    completed_count = counts.get("COMPLETED", 0)
    pct = round(completed_count / total * 100, 1) if total > 0 else 0.0

    # 6. Apply filters in Python
    filtered = all_items

    if status_filter and status_filter.upper() != "ALL":
        filtered = [i for i in filtered if i.migration_status == status_filter.upper()]

    if item_type_filter:
        filtered = [i for i in filtered if i.item_type == item_type_filter.upper()]

    if search:
        q = search.lower()
        filtered = [i for i in filtered if q in i.name.lower() or q in i.logical_path.lower()]

    # 7. Paginate
    page_items = filtered[offset: offset + limit]

    return SnapshotMigrationStatusOut(
        snapshot_id=snapshot_id,
        total_items=total,
        total_files=total_files,
        total_folders=total_folders,
        completed=completed_count,
        in_progress=counts.get("IN_PROGRESS", 0),
        pending=counts.get("PENDING", 0),
        percent_completed=pct,
        items=page_items,
    )


def _nfd(s: str) -> str:
    """Normaliza a NFD para comparar con rutas almacenadas en la BD en forma descompuesta."""
    import unicodedata
    return unicodedata.normalize("NFD", s)


@router.post("/migration-items/check-status", response_model=MigrationCheckResponse)
def check_items_migration_status(
    payload: MigrationCheckRequest,
    db: Session = Depends(get_db),
) -> MigrationCheckResponse:
    """Dado un listado de ítems de Drive (con su ruta lógica y tipo), indica el
    estado de migración de cada uno.

    Para archivos: COMPLETED (migrado por ID o por ruta) o NOT_MIGRATED.
    Para carpetas: COMPLETED (todos sus hijos migrados), PARTIAL (algunos) o
    NOT_MIGRATED (ninguno completado). Usa match por ID directo y por ruta para
    soportar comparar con un drive con la misma estructura de carpetas.
    """
    if not payload.items:
        return MigrationCheckResponse(results={})

    source_ids = [i.source_item_id for i in payload.items]

    # ── 1. Direct ID matches (COMPLETED) – aplica a todo tipo de ítem ────────
    direct_stmt = (
        select(MigrationItemModel)
        .where(
            MigrationItemModel.source_item_id.in_(source_ids),
            MigrationItemModel.state == "COMPLETED",
        )
    )
    direct_rows = db.execute(direct_stmt).scalars().all()
    direct_by_id: dict[str, MigrationItemModel] = {}
    for mi in direct_rows:
        existing = direct_by_id.get(mi.source_item_id)
        if existing is None or (
            mi.completed_at and (not existing.completed_at or mi.completed_at > existing.completed_at)
        ):
            direct_by_id[mi.source_item_id] = mi

    already_direct_ids = set(direct_by_id.keys())

    # ── 2. Para archivos: by_path COMPLETED ───────────────────────────────────
    # Usa coincidencia exacta Y por sufijo para soportar drives con distinto prefijo raíz.
    # Ejemplo: lógica "2. Diagnostícate/file.mp4" coincide con
    #          "Migración/2. Diagnostícate/file.mp4" almacenado en la BD.
    file_items = [i for i in payload.items if i.item_type.upper() != "FOLDER" and i.source_item_id not in already_direct_ids]
    file_paths = [i.logical_path for i in file_items]

    path_by_logical: dict[str, MigrationItemModel] = {}
    if file_paths:
        from sqlalchemy import or_

        file_conds = or_(
            *[
                or_(
                    MigrationItemModel.source_path == _nfd(path),
                    MigrationItemModel.source_path.like("%/" + _nfd(path)),
                )
                for path in file_paths
            ]
        )
        path_stmt = (
            select(MigrationItemModel)
            .where(
                file_conds,
                MigrationItemModel.state == "COMPLETED",
            )
        )
        for mi in db.execute(path_stmt).scalars().all():
            if mi.source_item_id in source_ids:
                continue  # mismo Drive, ya lo captura el direct check
            # mi.source_path puede estar en NFD; comparar con NFD de cada fp
            for fp in file_paths:
                nfd_fp = _nfd(fp)
                if mi.source_path == nfd_fp or mi.source_path.endswith("/" + nfd_fp):
                    existing = path_by_logical.get(fp)
                    if existing is None or (
                        mi.completed_at and (not existing.completed_at or mi.completed_at > existing.completed_at)
                    ):
                        path_by_logical[fp] = mi
                    break

    # ── 3. Para carpetas: deduplicación por sub-ruta relativa (SQL) ───────────────
    # El mismo archivo puede aparecer en múltiples lotes con distintos prefijos de raíz
    # (p. ej. "Migración/2. Diagnostícate/f.mp4" y "2. Diagnostícate/f.mp4").
    # PostgreSQL extrae la sub-ruta relativa dentro de cada carpeta y cuenta DISTINCT,
    # eliminando duplicados entre lotes. Un archivo = migrado si tiene COMPLETED o WAITING_REVIEW.
    folder_items = [i for i in payload.items if i.item_type.upper() == "FOLDER" and i.source_item_id not in already_direct_ids]

    # folder_stats[logical_path] = {"total": int, "completed": int}
    folder_stats: dict[str, dict[str, int]] = {}

    if folder_items:
        from sqlalchemy import text as sa_text

        # Una subquery por carpeta (UNION ALL). PostgreSQL extrae la sub-ruta relativa
        # con CASE + substring/strpos, luego el outer SELECT agrega con COUNT DISTINCT.
        sub_sqls: list[str] = []
        bind: dict[str, object] = {}

        for i, f in enumerate(folder_items):
            nfd_p = _nfd(f.logical_path)
            # offset 1-based en chars (NFD puede tener más code-points que NFC)
            off = len(nfd_p) + 2  # len(p) + len("/") + 1 para base-1
            sep = "/" + nfd_p + "/"

            bind.update({
                f"lbl{i}": f.logical_path,          # etiqueta NFC para lookup en Python
                f"p{i}": nfd_p,                      # ruta exacta (NFD)
                f"pre{i}": nfd_p + "/%",             # prefijo: p/...
                f"sex{i}": "%/" + nfd_p,             # sufijo exacto: .../p
                f"ssl{i}": "%/" + nfd_p + "/%",      # sufijo con hijos: .../p/...
                f"sep{i}": sep,                       # separador /p/ para strpos
                f"off{i}": off,                       # offset de substring
            })

            # Cada subquery une dos fuentes:
            # 1) repository_items: fuente de verdad del total de archivos existentes.
            #    Incluye archivos nunca planificados (invisible para migration_items).
            # 2) migration_items: estado real de cada migración.
            # La unión garantiza que archivos no migrados aparezcan en el total aunque
            # no tengan ningún registro en migration_items.
            sub_sqls.append(f"""
                SELECT
                    :lbl{i} AS folder_path,
                    CASE
                        WHEN logical_path = :p{i} THEN ''
                        WHEN logical_path LIKE :pre{i} THEN substring(logical_path, :off{i})
                        WHEN logical_path LIKE :sex{i} THEN ''
                        WHEN logical_path LIKE :ssl{i} THEN
                            substring(logical_path, strpos(logical_path, :sep{i}) + :off{i})
                    END AS relative_path,
                    NULL AS state
                FROM repository_items
                WHERE logical_path = :p{i}
                   OR logical_path LIKE :pre{i}
                   OR logical_path LIKE :sex{i}
                   OR logical_path LIKE :ssl{i}

                UNION ALL

                SELECT
                    :lbl{i} AS folder_path,
                    CASE
                        WHEN source_path = :p{i} THEN ''
                        WHEN source_path LIKE :pre{i} THEN substring(source_path, :off{i})
                        WHEN source_path LIKE :sex{i} THEN ''
                        WHEN source_path LIKE :ssl{i} THEN
                            substring(source_path, strpos(source_path, :sep{i}) + :off{i})
                    END AS relative_path,
                    state
                FROM migration_items
                WHERE source_path = :p{i}
                   OR source_path LIKE :pre{i}
                   OR source_path LIKE :sex{i}
                   OR source_path LIKE :ssl{i}
            """)

        combined = " UNION ALL ".join(f"({s})" for s in sub_sqls)
        agg_sql = f"""
            SELECT
                folder_path,
                COUNT(DISTINCT relative_path) AS total,
                COUNT(DISTINCT CASE WHEN state IN ('COMPLETED', 'WAITING_REVIEW')
                                    THEN relative_path END) AS completed
            FROM ({combined}) _sub
            GROUP BY folder_path
        """

        for row in db.execute(sa_text(agg_sql), bind).all():
            folder_stats[row.folder_path] = {
                "total": row.total,
                "completed": row.completed or 0,
            }

    # ── 4. Combinar resultados ────────────────────────────────────────────────
    results: dict[str, MigrationCheckStatus] = {}
    for item in payload.items:
        iid = item.source_item_id

        # Direct match always wins
        if iid in direct_by_id:
            results[iid] = MigrationCheckStatus(
                status="COMPLETED",
                match_type="direct",
                completed_at=direct_by_id[iid].completed_at,
            )
            continue

        if item.item_type.upper() == "FOLDER":
            stats = folder_stats.get(item.logical_path, {"total": 0, "completed": 0})
            total = stats["total"]
            completed = stats["completed"]
            if total == 0:
                # Never batched — fallback to single by_path check on the folder itself
                path_mi = path_by_logical.get(item.logical_path)
                if path_mi is not None:
                    results[iid] = MigrationCheckStatus(
                        status="COMPLETED",
                        match_type="by_path",
                        completed_at=path_mi.completed_at,
                    )
                else:
                    results[iid] = MigrationCheckStatus(
                        status="NOT_MIGRATED",
                        total_count=0,
                        completed_count=0,
                    )
            elif completed == total:
                results[iid] = MigrationCheckStatus(
                    status="COMPLETED",
                    match_type="aggregate",
                    total_count=total,
                    completed_count=completed,
                )
            elif completed > 0:
                results[iid] = MigrationCheckStatus(
                    status="PARTIAL",
                    match_type="aggregate",
                    total_count=total,
                    completed_count=completed,
                )
            else:
                results[iid] = MigrationCheckStatus(
                    status="NOT_MIGRATED",
                    match_type="aggregate",
                    total_count=total,
                    completed_count=0,
                )
        else:
            # File: by_path fallback
            path_mi = path_by_logical.get(item.logical_path)
            if path_mi is not None:
                results[iid] = MigrationCheckStatus(
                    status="COMPLETED",
                    match_type="by_path",
                    completed_at=path_mi.completed_at,
                )
            else:
                results[iid] = MigrationCheckStatus(status="NOT_MIGRATED")

    return MigrationCheckResponse(results=results)
