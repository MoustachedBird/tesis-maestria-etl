"""
Geography seed loader.

Loads geographic catalog data (entidades, municipios, localidades)
from Parquet seed files into DuckDB tables.
"""

import logging
from pathlib import Path

import polars as pl
from sqlalchemy import Connection, text

logger = logging.getLogger(__name__)

# Seed file -> table mappings in FK dependency order
SEED_MAPPINGS = [
    ("seed_entidades.parquet", "denue_entidades", "states"),
    ("seed_municipios.parquet", "denue_municipios", "municipalities"),
    ("seed_localidades.parquet", "denue_localidades", "localities"),
]

# Table DDL definitions
TABLE_DDL = {
    "denue_entidades": ("CREATE TABLE {table} (cve_ent VARCHAR PRIMARY KEY, entidad VARCHAR)"),
    "denue_municipios": (
        "CREATE TABLE {table} (cve_ent VARCHAR, cve_mun VARCHAR, municipio VARCHAR, "
        "PRIMARY KEY (cve_ent, cve_mun))"
    ),
    "denue_localidades": (
        "CREATE TABLE {table} (cve_ent VARCHAR, cve_mun VARCHAR, cve_loc VARCHAR, "
        "localidad VARCHAR, PRIMARY KEY (cve_ent, cve_mun, cve_loc))"
    ),
}

# Expected columns for validation
EXPECTED_COLUMNS = {
    "seed_entidades.parquet": ["cve_ent", "entidad"],
    "seed_municipios.parquet": ["cve_ent", "cve_mun", "municipio"],
    "seed_localidades.parquet": ["cve_ent", "cve_mun", "cve_loc", "localidad"],
}


def ensure_tables(conn, force: bool = False):
    """
    Ensure geography tables exist in DuckDB.

    Nunca dropea: estas tablas son blanco de FKs de `establecimientos` Y de
    `sesnsp_incidencia_municipal`, y DuckDB rechaza el DROP de una tabla
    referenciada (su CASCADE no cubre FKs entrantes). Los cambios de esquema
    van por Alembic (regla de oro en AGENTS.md); la siembra es idempotente
    via INSERT OR IGNORE en load_seeds().

    Args:
        conn: DuckDB connection
        force: Ignorado para el DDL (se conserva por compatibilidad de firma)
    """
    for _, table, _ in SEED_MAPPINGS:
        if not conn.execute(
            f"SELECT 1 FROM information_schema.tables WHERE table_name = '{table}'"
        ).fetchone():
            conn.execute(TABLE_DDL[table].format(table=table))


def load_seeds(conn, seeds_dir: Path, force: bool = False) -> dict[str, int]:
    """
    Load geography seeds from Parquet files into DuckDB (idempotente).

    INSERT OR IGNORE por PK: inserta solo las filas del seed que falten en
    la tabla, sin dropear ni truncar (los catalogos son append-only y estan
    referenciados por FKs de las tablas de hechos). Correr N veces es
    seguro, con o sin force.

    Args:
        conn: DuckDB connection
        seeds_dir: Path to directory containing seed Parquet files
        force: Sin efecto en la siembra (idempotente siempre); se conserva
            por compatibilidad con los demas loaders de _bootstrap_seeds

    Returns:
        dict: Row counts per table
    """
    ensure_tables(conn, force=force)
    stats = {}

    for file_name, table, display_name in SEED_MAPPINGS:
        seed_path = seeds_dir / file_name

        if not seed_path.exists():
            raise FileNotFoundError(f"Required seed file not found: {seed_path}")

        row_count = pl.scan_parquet(seed_path).select(pl.len()).collect().item()
        if row_count == 0:
            raise ValueError(f"Seed file '{file_name}' has 0 records")

        logger.info(f"Seed validated: {file_name} ({row_count:,} records)")

        inserted = conn.execute(
            f"INSERT OR IGNORE INTO {table} SELECT * FROM '{seed_path}'"
        ).fetchone()[0]
        if inserted:
            logger.info(f"Seed loaded: {table} +{inserted:,} rows from {file_name}")

        stats[display_name] = row_count

    return stats


def load_seeds_sqlalchemy(
    connection: Connection, seeds_dir: Path, truncate: bool = True
) -> dict[str, int]:
    """
    Load geography seeds via SQLAlchemy connection (for Alembic migrations).

    Args:
        connection: SQLAlchemy connection
        seeds_dir: Path to seed files
        truncate: If True, delete existing data first

    Returns:
        dict: Row counts per table
    """
    stats = {}

    for file_name, table, display_name in SEED_MAPPINGS:
        seed_path = seeds_dir / file_name

        if not seed_path.exists():
            raise FileNotFoundError(f"Seed file not found: {seed_path}")

        df = pl.read_parquet(seed_path)
        if len(df) == 0:
            continue

        if truncate:
            connection.execute(text(f"DELETE FROM {table}"))

        records = df.to_dicts()
        columns = df.columns
        columns_str = ", ".join(columns)
        values_str = ", ".join([f":{col}" for col in columns])
        insert_sql = f"INSERT INTO {table} ({columns_str}) VALUES ({values_str})"
        connection.execute(text(insert_sql), records)

        stats[display_name] = len(records)
        print(f"   Loaded {len(records):,} {display_name}")

    return stats


def validate_seeds(seeds_dir: Path) -> bool:
    """
    Validate that all geography seed files exist and have correct structure.

    Args:
        seeds_dir: Path to seed files

    Returns:
        True if all validations pass

    Raises:
        FileNotFoundError: If a seed file is missing
        ValueError: If a seed file is empty or has wrong columns
    """
    for file_name, _, _ in SEED_MAPPINGS:
        seed_path = seeds_dir / file_name

        if not seed_path.exists():
            raise FileNotFoundError(f"Missing seed file: {file_name}")

        df = pl.read_parquet(seed_path)
        if len(df) == 0:
            raise ValueError(f"Seed file {file_name} is empty")

        expected = set(EXPECTED_COLUMNS[file_name])
        actual = set(df.columns)
        if not expected.issubset(actual):
            missing = expected - actual
            raise ValueError(f"Seed file {file_name} missing columns: {missing}")

        print(f"   {file_name}: {len(df):,} rows, {len(df.columns)} columns")

    return True
