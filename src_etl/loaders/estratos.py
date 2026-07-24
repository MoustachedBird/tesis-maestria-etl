"""
Personnel strata (per_ocu) seed loader.

Fuente de verdad: `data/seeds/seed_estratos_personal.parquet` — id, etiqueta
INEGI, cotas de empleados y categoria de tamano viven en el archivo. La
carga y validacion las hace `loaders/seeds.py` (comun a todos los
catalogos); aqui solo se declara qué tabla y qué archivo.

`establecimientos.per_ocu_id` referencia `id` con FOREIGN KEY real, por eso
el catalogo es append-only.
"""

from pathlib import Path

from src_etl.loaders import seeds
from src_etl.loaders.seeds import read_seed, seed_duckdb, seed_sqlalchemy

TABLE_NAME = "denue_estratos_personal"
SEED_FILE = "seed_estratos_personal.parquet"


def seed_path(seeds_dir: Path | None = None) -> Path:
    """Ruta del seed del catalogo (falla claro si no esta versionado)."""
    return seeds.seed_path(SEED_FILE, seeds_dir)


def build_rows(seeds_dir: Path | None = None) -> list[dict]:
    """Filas del catalogo leidas del parquet, ya validadas."""
    return read_seed(TABLE_NAME, SEED_FILE, seeds_dir).to_dicts()


def load_seeds(conn, seeds_dir: Path | None = None, force: bool = False) -> dict[str, int]:
    """Siembra el catalogo via DuckDB nativo (bootstrap del ingest)."""
    df = read_seed(TABLE_NAME, SEED_FILE, seeds_dir)
    seed_duckdb(conn, TABLE_NAME, df)
    return {TABLE_NAME: df.height}


def load_seeds_sqlalchemy(
    connection, seeds_dir: Path | None = None, truncate: bool = True
) -> dict[str, int]:
    """Siembra el catalogo via SQLAlchemy (migraciones Alembic)."""
    df = read_seed(TABLE_NAME, SEED_FILE, seeds_dir)
    seed_sqlalchemy(connection, TABLE_NAME, df, truncate=truncate)
    return {TABLE_NAME: df.height}
