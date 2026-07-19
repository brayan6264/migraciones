from __future__ import annotations

import hashlib


def compute_idempotency_key(
    *,
    snapshot_id: str,
    source_provider: str,
    source_item_id: str,
    source_version_or_modified_time: str,
    planned_destination_path: str,
    export_format: str = "",
) -> str:
    """Huella estable (sección 9.6) que la base de datos usa como restricción
    única para impedir dos finalizaciones del mismo elemento."""
    raw = "|".join(
        [
            snapshot_id,
            source_provider,
            source_item_id,
            source_version_or_modified_time,
            planned_destination_path,
            export_format,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
