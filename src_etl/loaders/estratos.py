"""
Personnel strata (per_ocu) seed loader.

Seeds the `denue_estratos_personal` catalog from the `PersonalOcupado` enum.
The enum remains the single source of truth: rows are generated from
`get_range()` / `get_size_category()`, never duplicated by hand.

Surrogate key: `id INTEGER PRIMARY KEY` (posicion 1-based en el enum, ver
enums.domain_id) con `per_ocu` UNIQUE; `establecimientos.per_ocu_id` lo
referencia con FOREIGN KEY real. Igual que los dominios: enum append-only,
seed materializado en `data/seeds/seed_estratos_personal.parquet` (se
regenera si difiere del enum) y reseed solo cuando el contenido de la
tabla difiere (seguro con FKs entrantes).
"""

import logging
from pathlib import Path

import polars as pl
from sqlalchemy import Connection, text

from src_etl.db.enums import PersonalOcupado

logger = logging.getLogger(__name__)

TABLE_NAME = "denue_estratos_personal"
DISPLAY_NAME = "personnel_strata"
SEED_FILE = "seed_estratos_personal.parquet"
"""Archivo seed en data/seeds (patron de geografia/SCIAN)."""

TABLE_DDL = (
    "CREATE TABLE {table} ("
    "id INTEGER PRIMARY KEY, "
    "per_ocu VARCHAR NOT NULL UNIQUE, "
    "empleados_min INTEGER NOT NULL, empleados_max INTEGER NOT NULL, "
    "categoria VARCHAR NOT NULL)"
)
"""DDL del catalogo (mismo esquema que el modelo ORM EstratoPersonal)."""


def build_rows() -> list[dict]:
    """Generate catalog rows from the PersonalOcupado enum (ids 1-based)."""
    rows = []
    for i, estrato in enumerate(PersonalOcupado, start=1):
        emp_min, emp_max = estrato.get_range()
        rows.append(
            {
                "id": i,
                "per_ocu": estrato.value,
                "empleados_min": emp_min,
                "empleados_max": emp_max,
                "categoria": estrato.get_size_category(),
            }
        )
    return rows


def ensure_seed_parquet(seeds_dir: Path | None = None) -> Path:
    """
    Materializa el seed parquet desde el enum (el enum MANDA).

    Escribe `seed_estratos_personal.parquet` si no existe o si difiere del
    contenido derivado del enum.

    Returns:
        Path del parquet listo para cargar.
    """
    if seeds_dir is None:
        from src_etl.core.config import get_settings

        seeds_dir = Path(get_settings().SEEDS_DIR)
    seeds_dir.mkdir(parents=True, exist_ok=True)
    path = seeds_dir / SEED_FILE
    expected = pl.DataFrame(build_rows())
    if not path.exists() or not expected.equals(pl.read_parquet(path)):
        expected.write_parquet(path)
        logger.info(f"   Seed regenerado desde enum: {path.name} ({len(expected)} filas)")
    return path


def load_seeds(conn, seeds_dir: Path | None = None, force: bool = False) -> dict[str, int]:
    """
    Load personnel strata via native DuckDB connection (ingest bootstrap).

    Necesario en el bootstrap del ingest porque el DDL del writer referencia
    denue_estratos_personal(id) con FK real: el catalogo debe existir ANTES
    de crear la tabla de hechos. Mismo contrato que dominios.load_seeds
    (carga desde parquet; reseed solo si el contenido difiere).

    Args:
        conn: DuckDB connection.
        seeds_dir: Directorio de seeds (default: settings.SEEDS_DIR).
        force: If True, drop and recreate the table first.

    Returns:
        dict: Row count for the strata table.
    """
    if force:
        conn.execute(f"DROP TABLE IF EXISTS {TABLE_NAME} CASCADE")

    if not conn.execute(
        f"SELECT 1 FROM information_schema.tables WHERE table_name = '{TABLE_NAME}'"
    ).fetchone():
        conn.execute(TABLE_DDL.format(table=TABLE_NAME))

    rows = pl.read_parquet(ensure_seed_parquet(seeds_dir)).to_dicts()
    cols = list(rows[0].keys())
    existing = conn.execute(f"SELECT {', '.join(cols)} FROM {TABLE_NAME}").fetchall()
    if sorted(existing) != sorted(tuple(r.values()) for r in rows):
        conn.execute(f"DELETE FROM {TABLE_NAME}")
        placeholders = ", ".join(["?"] * len(cols))
        conn.executemany(
            f"INSERT INTO {TABLE_NAME} ({', '.join(cols)}) VALUES ({placeholders})",
            [[r[c] for c in cols] for r in rows],
        )
        logger.info(f"   Loaded {len(rows):,} {DISPLAY_NAME}")
    return {DISPLAY_NAME: len(rows)}


def load_seeds_sqlalchemy(
    connection: Connection,
    seeds_dir: Path | None = None,
    truncate: bool = True,
) -> dict[str, int]:
    """
    Load personnel strata via SQLAlchemy connection (for Alembic migrations).

    Carga DESDE el parquet de data/seeds (regenerado desde el enum si falta
    o difiere). Reseeds only when the table content differs (safe with
    incoming FKs from establecimientos.per_ocu_id).

    Args:
        connection: SQLAlchemy connection
        seeds_dir: Directorio de seeds (default: settings.SEEDS_DIR)
        truncate: If True, delete existing data before reseeding on mismatch

    Returns:
        dict: Row count for the strata table
    """
    rows = pl.read_parquet(ensure_seed_parquet(seeds_dir)).to_dicts()

    # The table may not exist yet when replaying migrations older than
    # bbe078d563ac (seed_catalog_tables also calls this loader from
    # dff9d0804542); creating it here keeps every replay point valid.
    table_exists = (
        connection.execute(
            text("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = :table"),
            {"table": TABLE_NAME},
        ).scalar()
        > 0
    )
    if not table_exists:
        connection.execute(text(TABLE_DDL.format(table=TABLE_NAME)))

    cols = list(rows[0].keys())
    existing = connection.execute(text(f"SELECT {', '.join(cols)} FROM {TABLE_NAME}")).fetchall()
    expected = sorted(tuple(r.values()) for r in rows)
    if sorted(tuple(r) for r in existing) != expected:
        if truncate:
            connection.execute(text(f"DELETE FROM {TABLE_NAME}"))
        params = ", ".join(f":{c}" for c in cols)
        connection.execute(
            text(f"INSERT INTO {TABLE_NAME} ({', '.join(cols)}) VALUES ({params})"),
            rows,
        )
        print(f"   Loaded {len(rows):,} personnel strata")

    return {DISPLAY_NAME: len(rows)}
