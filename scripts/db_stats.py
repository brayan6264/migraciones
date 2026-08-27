"""
Muestra el tamaño de cada tabla y las dead tuples acumuladas.
En PostgreSQL también ofrece correr VACUUM ANALYZE para limpiarlas.

Uso:
    python scripts/db_stats.py            # solo estadísticas
    python scripts/db_stats.py --vacuum   # estadísticas + VACUUM ANALYZE
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sqlalchemy import text

from document_engine.adapters.database.session import get_engine
from document_engine.settings import get_settings


def _bar(ratio: float, width: int = 20) -> str:
    filled = round(ratio * width)
    return "█" * filled + "░" * (width - filled)


def stats_postgres(conn, vacuum: bool) -> None:
    rows = conn.execute(text("""
        SELECT
            s.relname                                                      AS tabla,
            pg_total_relation_size(c.oid)                                  AS bytes_total,
            pg_relation_size(c.oid)                                        AS bytes_datos,
            pg_total_relation_size(c.oid) - pg_relation_size(c.oid)       AS bytes_extra,
            COALESCE(s.n_live_tup, 0)                                      AS live,
            COALESCE(s.n_dead_tup, 0)                                      AS dead,
            COALESCE(s.last_vacuum::text, '—')                             AS last_vacuum,
            COALESCE(s.last_autovacuum::text, '—')                         AS last_autovacuum
        FROM pg_stat_user_tables s
        JOIN pg_class c ON c.relname = s.relname
        WHERE c.relkind = 'r'
        ORDER BY bytes_total DESC
    """)).all()

    if not rows:
        print("No se encontraron tablas de usuario.")
        return

    total_bytes = sum(r.bytes_total for r in rows)
    max_bytes   = rows[0].bytes_total if rows else 1

    col_w = 28
    print(f"\n{'Tabla':<{col_w}}  {'Total':>9}  {'Datos':>9}  {'Idx+TOAST':>9}  {'Live':>8}  {'Dead':>8}  {'Ratio dead':>10}  {'Distribución'}")
    print("─" * 120)

    for r in rows:
        ratio_size = r.bytes_total / max_bytes if max_bytes else 0
        ratio_dead = r.dead / (r.live + r.dead) if (r.live + r.dead) > 0 else 0.0

        def fmt_bytes(b: int) -> str:
            for unit in ("B", "KB", "MB", "GB"):
                if b < 1024:
                    return f"{b:.1f} {unit}"
                b /= 1024
            return f"{b:.1f} TB"

        print(
            f"{r.tabla:<{col_w}}"
            f"  {fmt_bytes(r.bytes_total):>9}"
            f"  {fmt_bytes(r.bytes_datos):>9}"
            f"  {fmt_bytes(r.bytes_extra):>9}"
            f"  {r.live:>8,}"
            f"  {r.dead:>8,}"
            f"  {ratio_dead:>9.1%}"
            f"  {_bar(ratio_size)}"
        )

    print("─" * 120)
    print(f"{'TOTAL':<{col_w}}  {fmt_bytes(total_bytes):>9}")

    total_dead = sum(r.dead for r in rows)
    total_live = sum(r.live for r in rows)
    print(f"\nTuplas vivas: {total_live:,}   |   Dead tuples: {total_dead:,}")

    if total_dead > 0:
        print("\n⚠  Hay dead tuples acumuladas. El espacio físico no se libera hasta hacer VACUUM.")
        if not vacuum:
            print("   Corre con --vacuum para ejecutar VACUUM ANALYZE automáticamente.")

    if vacuum:
        print("\nEjecutando VACUUM ANALYZE en todas las tablas...")
        # VACUUM no puede correr dentro de una transacción, hay que usar isolation_level=None
        raw = conn.connection
        old_iso = raw.isolation_level
        raw.set_isolation_level(0)  # AUTOCOMMIT
        cursor = raw.cursor()
        for r in rows:
            print(f"  VACUUM ANALYZE {r.tabla} ...", end=" ", flush=True)
            cursor.execute(f"VACUUM ANALYZE {r.tabla}")
            print("ok")
        cursor.close()
        raw.set_isolation_level(old_iso)

        print("\nEstadísticas después del VACUUM:")
        stats_postgres(conn, vacuum=False)


def stats_sqlite(conn) -> None:
    page_size = conn.execute(text("PRAGMA page_size")).scalar()
    tables = [r[0] for r in conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )).all()]

    print(f"\n{'Tabla':<35}  {'Páginas':>8}  {'Tamaño aprox.':>14}  {'Distribución'}")
    print("─" * 85)

    sizes = []
    for t in tables:
        pages = conn.execute(text(f"SELECT COUNT(*) FROM (SELECT * FROM {t})")).scalar() or 0
        # SQLite no tiene stats de bloat; usamos dbstat si está disponible
        try:
            pages = conn.execute(text(f"SELECT SUM(pageno) FROM dbstat WHERE name='{t}'")).scalar() or 0
        except Exception:
            pages = 0
        sizes.append((t, pages * page_size))

    sizes.sort(key=lambda x: x[1], reverse=True)
    max_sz = sizes[0][1] if sizes else 1

    def fmt_bytes(b: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if b < 1024:
                return f"{b:.1f} {unit}"
            b /= 1024
        return f"{b:.1f} TB"

    for t, sz in sizes:
        print(f"{t:<35}  {sz // page_size:>8,}  {fmt_bytes(sz):>14}  {_bar(sz / max_sz if max_sz else 0)}")

    total = sum(s for _, s in sizes)
    print("─" * 85)
    print(f"{'TOTAL':<35}  {'':>8}  {fmt_bytes(total):>14}")
    print("\nSQLite no tiene dead tuples — VACUUM integrado no es necesario aquí.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vacuum", action="store_true", help="Ejecuta VACUUM ANALYZE tras mostrar las stats (solo PostgreSQL).")
    args = parser.parse_args()

    settings = get_settings()
    print(f"Base de datos: {settings.database_url!r}")

    engine = get_engine()
    with engine.connect() as conn:
        if engine.dialect.name == "postgresql":
            stats_postgres(conn, vacuum=args.vacuum)
        else:
            if args.vacuum:
                print("--vacuum solo aplica a PostgreSQL, ignorado.")
            stats_sqlite(conn)


if __name__ == "__main__":
    main()
