"""
Geography seed loader (entidades / municipios / localidades).

Fuente de verdad: los parquet de `data/seeds`. La carga y validacion las
hace `loaders/seeds.py` (comun a todos los catalogos); aqui solo se declara
el mapeo archivo -> tabla, en orden de dependencia de FKs.
"""

from pathlib import Path

from src.etl.loaders.seeds import read_seed, seed_duckdb, seed_sqlalchemy

# (archivo seed, tabla), en orden de FKs: entidades <- municipios <- localidades.
SEED_MAPPINGS = [
    ("seed_entidades.parquet", "denue_entidades"),
    ("seed_municipios.parquet", "denue_municipios"),
    ("seed_localidades.parquet", "denue_localidades"),
]
"""Catalogos geograficos, en orden de carga (los hijos tras los padres)."""


def build_rows(seeds_dir: Path | None = None) -> dict[str, list[dict]]:
    """Filas de cada catalogo geografico, ya validadas."""
    return {table: read_seed(table, f, seeds_dir).to_dicts() for f, table in SEED_MAPPINGS}


def load_seeds(conn, seeds_dir: Path | None = None, force: bool = False) -> dict[str, int]:
    """Siembra los catalogos via DuckDB nativo (bootstrap del ingest)."""
    stats: dict[str, int] = {}
    for file_name, table in SEED_MAPPINGS:
        df = read_seed(table, file_name, seeds_dir)
        seed_duckdb(conn, table, df)
        stats[table] = df.height
    return stats


def load_seeds_sqlalchemy(
    connection, seeds_dir: Path | None = None, truncate: bool = True
) -> dict[str, int]:
    """Siembra los catalogos via SQLAlchemy (migraciones Alembic)."""
    stats: dict[str, int] = {}
    for file_name, table in SEED_MAPPINGS:
        df = read_seed(table, file_name, seeds_dir)
        seed_sqlalchemy(connection, table, df, truncate=truncate)
        stats[table] = df.height
    return stats
