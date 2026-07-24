"""
Domain-type catalogs seed loader (tipo_vial / tipo_asent / tipounieco).

Fuente de verdad: los parquet de `data/seeds` (id, etiqueta INEGI y
descripcion viven en el archivo). Dar de alta un valor nuevo es editar el
parquet y correr `make db-migrate`, sin tocar codigo.

Surrogate keys: cada catalogo tiene `id INTEGER PRIMARY KEY` y el label como
UNIQUE. `establecimientos` los referencia con FOREIGN KEYs REALES por id, por
eso son append-only. La carga y validacion las hace `loaders/seeds.py`
(comun a todos los catalogos); aqui solo se declara el mapeo tabla -> archivo.
"""

from pathlib import Path

from src_etl.loaders import seeds
from src_etl.loaders.seeds import read_seed, seed_duckdb, seed_sqlalchemy

# (tabla, columna label) de cada catalogo de dominio, en orden de carga.
TABLES = [
    ("denue_tipos_vialidad", "tipo_vial"),
    ("denue_tipos_asentamiento", "tipo_asent"),
    ("denue_tipos_unidad_economica", "tipounieco"),
]
"""Catalogos de dominio que este loader administra."""

SEED_FILES = {table: f"seed_{table.removeprefix('denue_')}.parquet" for table, _ in TABLES}
"""Archivo parquet por catalogo en data/seeds."""


def seed_path(table: str, seeds_dir: Path | None = None) -> Path:
    """Ruta del seed de un catalogo (falla claro si no esta versionado)."""
    return seeds.seed_path(SEED_FILES[table], seeds_dir)


def build_rows(seeds_dir: Path | None = None) -> dict[str, list[dict]]:
    """Filas de cada catalogo leidas de su parquet, ya validadas."""
    return {
        table: read_seed(table, SEED_FILES[table], seeds_dir).to_dicts() for table, _ in TABLES
    }


def load_seeds(conn, seeds_dir: Path | None = None, force: bool = False) -> dict[str, int]:
    """Siembra los catalogos via DuckDB nativo (bootstrap del ingest)."""
    stats: dict[str, int] = {}
    for table, _label in TABLES:
        df = read_seed(table, SEED_FILES[table], seeds_dir)
        seed_duckdb(conn, table, df)
        stats[table] = df.height
    return stats


def load_seeds_sqlalchemy(
    connection, seeds_dir: Path | None = None, truncate: bool = True
) -> dict[str, int]:
    """Siembra los catalogos via SQLAlchemy (migraciones Alembic)."""
    stats: dict[str, int] = {}
    for table, _label in TABLES:
        df = read_seed(table, SEED_FILES[table], seeds_dir)
        seed_sqlalchemy(connection, table, df, truncate=truncate)
        stats[table] = df.height
    return stats
