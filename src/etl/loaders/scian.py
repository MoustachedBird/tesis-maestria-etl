"""
SCIAN (Sistema de Clasificacion Industrial de America del Norte) seed loader.

Fuente de verdad: `data/seeds/seed_actividades.parquet` (columnas
`codigo_act`, `nombre_act`). La jerarquia sector/subsector/rama se DERIVA del
prefijo del codigo al cargar, no vive en el archivo. La carga y validacion
las hace `loaders/seeds.py` (comun a todos los catalogos).
"""

from pathlib import Path

import polars as pl

from src.etl.loaders.seeds import read_seed, seed_duckdb, seed_sqlalchemy

SEED_FILE = "seed_actividades.parquet"
TABLE_NAME = "denue_actividades_economicas"


def _with_scian_hierarchy(df: pl.DataFrame) -> pl.DataFrame:
    """Deriva sector/subsector/rama del codigo SCIAN de 6 digitos."""
    return df.with_columns(
        pl.col("codigo_act").str.slice(0, 2).alias("sector"),
        pl.col("codigo_act").str.slice(0, 3).alias("subsector"),
        pl.col("codigo_act").str.slice(0, 4).alias("rama"),
    )


def build_rows(seeds_dir: Path | None = None) -> list[dict]:
    """Filas del catalogo (con jerarquia derivada), ya validadas."""
    return read_seed(TABLE_NAME, SEED_FILE, seeds_dir, transform=_with_scian_hierarchy).to_dicts()


def load_seeds(conn, seeds_dir: Path | None = None, force: bool = False) -> dict[str, int]:
    """Siembra el catalogo via DuckDB nativo (bootstrap del ingest)."""
    df = read_seed(TABLE_NAME, SEED_FILE, seeds_dir, transform=_with_scian_hierarchy)
    seed_duckdb(conn, TABLE_NAME, df)
    return {TABLE_NAME: df.height}


def load_seeds_sqlalchemy(
    connection, seeds_dir: Path | None = None, truncate: bool = True
) -> dict[str, int]:
    """Siembra el catalogo via SQLAlchemy (migraciones Alembic)."""
    df = read_seed(TABLE_NAME, SEED_FILE, seeds_dir, transform=_with_scian_hierarchy)
    seed_sqlalchemy(connection, TABLE_NAME, df, truncate=truncate)
    return {TABLE_NAME: df.height}
