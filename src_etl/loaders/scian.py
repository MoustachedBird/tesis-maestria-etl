"""
SCIAN (Sistema de Clasificacion Industrial de America del Norte) seed loader.

Loads the SCIAN economic activity catalog from Parquet seed files
into the DuckDB warehouse.
"""

import logging
from pathlib import Path

import polars as pl
from sqlalchemy import Connection, text

logger = logging.getLogger(__name__)

# Seed file -> table mapping
SEED_FILE = "seed_actividades.parquet"
TABLE_NAME = "denue_actividades_economicas"
DISPLAY_NAME = "economic_activities"

# Table DDL. PK is codigo_act alone (nombre_act depends functionally on it);
# sector/subsector/rama materialize the SCIAN hierarchy from the code prefix.
TABLE_DDL = (
    "CREATE TABLE {table} ("
    "codigo_act VARCHAR PRIMARY KEY, nombre_act VARCHAR NOT NULL, "
    "sector VARCHAR NOT NULL, subsector VARCHAR NOT NULL, rama VARCHAR NOT NULL)"
)

# Expected columns for validation (seed parquet; hierarchy is derived at load)
EXPECTED_COLUMNS = ["codigo_act", "nombre_act"]


def _with_scian_hierarchy(df: pl.DataFrame) -> pl.DataFrame:
    """Derive sector/subsector/rama columns from the 6-digit SCIAN code."""
    return df.with_columns(
        pl.col("codigo_act").str.slice(0, 2).alias("sector"),
        pl.col("codigo_act").str.slice(0, 3).alias("subsector"),
        pl.col("codigo_act").str.slice(0, 4).alias("rama"),
    )


def ensure_table(conn, force: bool = False):
    """
    Ensure SCIAN activities table exists in DuckDB.

    Args:
        conn: DuckDB connection
        force: If True, drop and recreate table
    """
    if force:
        conn.execute(f"DROP TABLE IF EXISTS {TABLE_NAME} CASCADE")

    if not conn.execute(
        f"SELECT 1 FROM information_schema.tables WHERE table_name = '{TABLE_NAME}'"
    ).fetchone():
        conn.execute(TABLE_DDL.format(table=TABLE_NAME))


def load_seeds(conn, seeds_dir: Path, force: bool = False) -> dict[str, int]:
    """
    Load SCIAN activity seeds from Parquet into DuckDB.

    Args:
        conn: DuckDB connection
        seeds_dir: Path to directory containing seed Parquet files
        force: If True, drop table and reload

    Returns:
        dict: Row count for the activities table
    """
    ensure_table(conn, force=force)

    seed_path = seeds_dir / SEED_FILE

    if not seed_path.exists():
        raise FileNotFoundError(f"Required seed file not found: {seed_path}")

    row_count = pl.scan_parquet(seed_path).select(pl.len()).collect().item()
    if row_count == 0:
        raise ValueError(f"Seed file '{SEED_FILE}' has 0 records")

    logger.info(f"Seed validated: {SEED_FILE} ({row_count:,} records)")

    # Only load if table is empty or force mode
    existing = conn.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}").fetchone()[0]
    if existing == 0:
        conn.execute(f"""
            INSERT INTO {TABLE_NAME}
            SELECT codigo_act, nombre_act,
                   substr(codigo_act, 1, 2), substr(codigo_act, 1, 3), substr(codigo_act, 1, 4)
            FROM '{seed_path}'
        """)
        logger.info(f"Seed loaded: {TABLE_NAME} from {SEED_FILE}")

    return {DISPLAY_NAME: row_count}


def load_seeds_sqlalchemy(
    connection: Connection, seeds_dir: Path, truncate: bool = True
) -> dict[str, int]:
    """
    Load SCIAN seeds via SQLAlchemy connection (for Alembic migrations).

    Args:
        connection: SQLAlchemy connection
        seeds_dir: Path to seed files
        truncate: If True, delete existing data first

    Returns:
        dict: Row count for activities table
    """
    seed_path = seeds_dir / SEED_FILE

    if not seed_path.exists():
        raise FileNotFoundError(f"Seed file not found: {seed_path}")

    df = pl.read_parquet(seed_path)
    if len(df) == 0:
        return {DISPLAY_NAME: 0}

    # Schema-aware: the seed migration (dff9d0804542) replays against the
    # legacy 2-column table; the hierarchy columns only exist after the
    # normalization migration (173b3ded90cd) recreates the catalog.
    table_columns = {
        row[0]
        for row in connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :table"
            ),
            {"table": TABLE_NAME},
        )
    }
    if "sector" in table_columns:
        df = _with_scian_hierarchy(df)

    if truncate:
        connection.execute(text(f"DELETE FROM {TABLE_NAME}"))

    records = df.to_dicts()
    columns = df.columns
    columns_str = ", ".join(columns)
    values_str = ", ".join([f":{col}" for col in columns])
    insert_sql = f"INSERT INTO {TABLE_NAME} ({columns_str}) VALUES ({values_str})"
    connection.execute(text(insert_sql), records)

    print(f"   Loaded {len(records):,} economic activities")
    return {DISPLAY_NAME: len(records)}


def validate_seeds(seeds_dir: Path) -> bool:
    """
    Validate that the SCIAN seed file exists and has correct structure.

    Args:
        seeds_dir: Path to seed files

    Returns:
        True if validation passes

    Raises:
        FileNotFoundError: If seed file is missing
        ValueError: If seed file is empty or has wrong columns
    """
    seed_path = seeds_dir / SEED_FILE

    if not seed_path.exists():
        raise FileNotFoundError(f"Missing seed file: {SEED_FILE}")

    df = pl.read_parquet(seed_path)
    if len(df) == 0:
        raise ValueError(f"Seed file {SEED_FILE} is empty")

    expected = set(EXPECTED_COLUMNS)
    actual = set(df.columns)
    if not expected.issubset(actual):
        missing = expected - actual
        raise ValueError(f"Seed file {SEED_FILE} missing columns: {missing}")

    print(f"   {SEED_FILE}: {len(df):,} rows, {len(df.columns)} columns")
    return True
