from __future__ import annotations

import hashlib


def compute_idempotency_key(
    *,
    batch_id: str,
    snapshot_id: str,
    source_provider: str,
    source_item_id: str,
    source_version_or_modified_time: str,
    planned_destination_path: str,
    export_format: str = "",
) -> str:
    """Huella estable (sección 9.6) que la base de datos usa como restricción
    única por lote para impedir dos entradas del mismo elemento en el mismo lote.
    `batch_id` se incluye para que lotes distintos puedan planificar el mismo
    archivo de origen (con herencia de nombre o re-migración)."""
    raw = "|".join(
        [
            batch_id,
            snapshot_id,
            source_provider,
            source_item_id,
            source_version_or_modified_time,
            planned_destination_path,
            export_format,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
