"""
Elimina lotes en estado DRAFT o PLANNED, junto con todos sus datos dependientes.
Los lotes RUNNING y COMPLETED (y sus ítems) NO se tocan.

Uso:
    # Desde la raíz del proyecto backend (donde está .env o con la variable ya exportada):
    python scripts/purge_draft_planned.py

    # Dry-run — muestra qué se borraría sin tocar nada:
    python scripts/purge_draft_planned.py --dry-run

    # Sin pedir confirmación (útil en CI/scripts):
    python scripts/purge_draft_planned.py --yes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Permite ejecutar el script desde cualquier directorio
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sqlalchemy import text

from document_engine.adapters.database.session import get_engine
from document_engine.settings import get_settings


PURGEABLE_BATCH_STATES = ("DRAFT", "PLANNED")

# ── helpers ──────────────────────────────────────────────────────────────────


def _count(conn, table: str, where: str, params: dict) -> int:
    row = conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {where}"), params).one()
    return row[0]


def _fmt(n: int) -> str:
    return f"{n:,}"


# ── main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Solo muestra lo que se borraría, sin borrar nada.")
    parser.add_argument("--yes", "-y", action="store_true", help="No pide confirmación.")
    args = parser.parse_args()

    settings = get_settings()
    print(f"Base de datos: {settings.database_url!r}\n")

    engine = get_engine()
    states_tuple = tuple(PURGEABLE_BATCH_STATES)

    with engine.connect() as conn:
        # ── 1. Identificar lotes a borrar ────────────────────────────────────
        in_clause = ", ".join(f"'{s}'" for s in states_tuple)
        batch_rows = conn.execute(
            text(f"SELECT id, name, status FROM migration_batches WHERE status IN ({in_clause})")
        ).all()

        if not batch_rows:
            print("No hay lotes en estado DRAFT o PLANNED. Nada que hacer.")
            return

        batch_ids = [r[0] for r in batch_rows]
        bid_list = ", ".join(f"'{b}'" for b in batch_ids)

        # ── 2. Contar registros dependientes ──────────────────────────────────
        n_batches = len(batch_ids)

        item_rows = conn.execute(
            text(f"SELECT id FROM migration_items WHERE batch_id IN ({bid_list})")
        ).all()
        item_ids = [r[0] for r in item_rows]
        n_items = len(item_ids)

        iid_list = ", ".join(f"'{i}'" for i in item_ids) if item_ids else "''"

        n_leases      = _count(conn, "worker_leases",       f"migration_item_id IN ({iid_list})",  {}) if item_ids else 0
        n_checkpoints = _count(conn, "transfer_checkpoints", f"migration_item_id IN ({iid_list})", {}) if item_ids else 0
        n_validations = _count(conn, "validation_results",   f"migration_item_id IN ({iid_list})", {}) if item_ids else 0
        n_decisions   = _count(conn, "name_decisions",       f"migration_item_id IN ({iid_list})", {}) if item_ids else 0
        n_events_item = _count(conn, "journal_events",       f"migration_item_id IN ({iid_list})", {}) if item_ids else 0
        n_events_batch= _count(conn, "journal_events",       f"batch_id IN ({bid_list})",          {})
        n_plans       = _count(conn, "migration_plans",      f"batch_id IN ({bid_list})",          {})
        n_selectors   = _count(conn, "batch_selectors",      f"batch_id IN ({bid_list})",          {})

        n_events = max(n_events_item, n_events_batch)  # pueden solaparse; borraremos por batch_id

        # ── 3. Resumen ────────────────────────────────────────────────────────
        print("═" * 60)
        print(f"  Lotes a eliminar ({_fmt(n_batches)}):")
        for bid, bname, bstatus in batch_rows:
            print(f"    [{bstatus}]  {bname}  ({bid})")
        print()
        print("  Registros dependientes que se borrarán:")
        print(f"    migration_items      : {_fmt(n_items)}")
        print(f"    name_decisions       : {_fmt(n_decisions)}")
        print(f"    transfer_checkpoints : {_fmt(n_checkpoints)}")
        print(f"    validation_results   : {_fmt(n_validations)}")
        print(f"    worker_leases        : {_fmt(n_leases)}")
        print(f"    journal_events       : {_fmt(n_events)}")
        print(f"    migration_plans      : {_fmt(n_plans)}")
        print(f"    batch_selectors      : {_fmt(n_selectors)}")
        print("═" * 60)

        if args.dry_run:
            print("\n[dry-run] Nada fue borrado.")
            return

        if not args.yes:
            resp = input("\n¿Confirmar el borrado? Escribe 'si' para continuar: ").strip().lower()
            if resp not in ("si", "sí"):
                print("Operación cancelada.")
                return

        # ── 4. Borrar en orden (hijos primero) ───────────────────────────────
        if item_ids:
            conn.execute(text(f"DELETE FROM worker_leases       WHERE migration_item_id IN ({iid_list})"))
            conn.execute(text(f"DELETE FROM transfer_checkpoints WHERE migration_item_id IN ({iid_list})"))
            conn.execute(text(f"DELETE FROM validation_results   WHERE migration_item_id IN ({iid_list})"))
            conn.execute(text(f"DELETE FROM name_decisions        WHERE migration_item_id IN ({iid_list})"))
            conn.execute(text(f"DELETE FROM journal_events        WHERE migration_item_id IN ({iid_list})"))

        conn.execute(text(f"DELETE FROM journal_events   WHERE batch_id IN ({bid_list})"))
        conn.execute(text(f"DELETE FROM migration_items  WHERE batch_id IN ({bid_list})"))
        conn.execute(text(f"DELETE FROM migration_plans  WHERE batch_id IN ({bid_list})"))
        conn.execute(text(f"DELETE FROM batch_selectors  WHERE batch_id IN ({bid_list})"))
        conn.execute(text(f"DELETE FROM migration_batches WHERE id IN ({bid_list})"))

        conn.commit()

        print(f"\nListo. {_fmt(n_batches)} lote(s) y todos sus datos eliminados.")


if __name__ == "__main__":
    main()
