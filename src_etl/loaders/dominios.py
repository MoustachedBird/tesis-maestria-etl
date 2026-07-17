"""
Domain-type catalogs seed loader (tipo_vial / tipo_asent / tipounieco).

Seeds the `denue_tipos_vialidad`, `denue_tipos_asentamiento` and
`denue_tipos_unidad_economica` catalog tables from the Python enums in
`src_etl/db/enums.py` (single source of truth, same pattern as
`estratos.py` for per_ocu). These catalogs replace the old DuckDB ENUM
column types as the domain of the corresponding `establecimientos`
columns: the enum types baked the domain into the column TYPE (not
evolvable in DuckDB), while a catalog row is plain data.

Two entry points, same rows:
  - `load_seeds(conn, force)`: native DuckDB connection (ingest bootstrap).
  - `load_seeds_sqlalchemy(connection)`: SQLAlchemy connection (Alembic).
"""

import logging

from sqlalchemy import Connection, text

from src_etl.db.enums import TipoAsentamiento, TipoUnidadEconomica, TipoVial

logger = logging.getLogger(__name__)

# (tabla, columna PK, nombre para stats/logs)
TABLES = [
    ("denue_tipos_vialidad", "tipo_vial", "road_types"),
    ("denue_tipos_asentamiento", "tipo_asent", "settlement_types"),
    ("denue_tipos_unidad_economica", "tipounieco", "economic_unit_types"),
]
"""Catalogos de dominio que este loader administra, en orden de carga."""

TABLE_DDL = {
    "denue_tipos_vialidad": ("CREATE TABLE {table} (tipo_vial VARCHAR PRIMARY KEY)"),
    "denue_tipos_asentamiento": ("CREATE TABLE {table} (tipo_asent VARCHAR PRIMARY KEY)"),
    "denue_tipos_unidad_economica": (
        "CREATE TABLE {table} (tipounieco VARCHAR PRIMARY KEY, descripcion VARCHAR NOT NULL)"
    ),
}
"""DDL por tabla (mismo esquema que los modelos ORM de models.py)."""


def build_rows() -> dict[str, list[dict]]:
    """
    Generate catalog rows from the enums (never duplicated by hand).

    Returns:
        Dict tabla -> lista de filas (dicts columna -> valor).
    """
    return {
        "denue_tipos_vialidad": [{"tipo_vial": t.value} for t in TipoVial],
        "denue_tipos_asentamiento": [{"tipo_asent": t.value} for t in TipoAsentamiento],
        "denue_tipos_unidad_economica": [
            {"tipounieco": t.value, "descripcion": t.get_descripcion()}
            for t in TipoUnidadEconomica
        ],
    }


def load_seeds(conn, force: bool = False) -> dict[str, int]:
    """
    Load domain catalogs via a native DuckDB connection (ingest bootstrap).

    Idempotent: creates missing tables and reseeds (delete + insert) so the
    catalogs always mirror the enums.

    Args:
        conn: DuckDB connection.
        force: If True, drop and recreate the tables first.

    Returns:
        dict: Row count per catalog (display name -> rows).
    """
    rows_by_table = build_rows()
    stats = {}

    for table, _pk, display_name in TABLES:
        if force:
            conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

        if not conn.execute(
            f"SELECT 1 FROM information_schema.tables WHERE table_name = '{table}'"
        ).fetchone():
            conn.execute(TABLE_DDL[table].format(table=table))

        rows = rows_by_table[table]
        cols = list(rows[0].keys())
        placeholders = ", ".join(["?"] * len(cols))
        conn.execute(f"DELETE FROM {table}")
        conn.executemany(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
            [[r[c] for c in cols] for r in rows],
        )
        stats[display_name] = len(rows)
        logger.info(f"   Loaded {len(rows):,} {display_name}")

    return stats


def load_seeds_sqlalchemy(connection: Connection, truncate: bool = True) -> dict[str, int]:
    """
    Load domain catalogs via SQLAlchemy connection (for Alembic migrations).

    Creates missing tables (keeps older replay points valid, same pattern
    as estratos.load_seeds_sqlalchemy) and reseeds from the enums.

    Args:
        connection: SQLAlchemy connection.
        truncate: If True, delete existing rows first.

    Returns:
        dict: Row count per catalog (display name -> rows).
    """
    rows_by_table = build_rows()
    stats = {}

    for table, _pk, display_name in TABLES:
        table_exists = (
            connection.execute(
                text("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = :table"),
                {"table": table},
            ).scalar()
            > 0
        )
        if not table_exists:
            connection.execute(text(TABLE_DDL[table].format(table=table)))

        if truncate:
            connection.execute(text(f"DELETE FROM {table}"))

        rows = rows_by_table[table]
        cols = list(rows[0].keys())
        params = ", ".join(f":{c}" for c in cols)
        connection.execute(
            text(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({params})"),
            rows,
        )
        stats[display_name] = len(rows)
        print(f"   Loaded {len(rows):,} {display_name}")

    return stats
