"""
Domain-type catalogs seed loader (tipo_vial / tipo_asent / tipounieco).

Siembra `denue_tipos_vialidad`, `denue_tipos_asentamiento` y
`denue_tipos_unidad_economica` DESDE los parquet de `data/seeds`, que son la
FUENTE DE VERDAD: id, etiqueta INEGI y descripcion viven en el archivo, no
duplicados en enums de Python. Dar de alta un valor nuevo (INEGI cambia sus
etiquetas entre cortes) es editar el parquet, sin tocar codigo.

Surrogate keys: cada catalogo tiene `id INTEGER PRIMARY KEY` y el label como
UNIQUE. `establecimientos` los referencia con FOREIGN KEYs REALES por id
(decision del usuario 2026-07-17: relaciones fisicas, no joins por string),
por eso los catalogos son append-only: millones de filas apuntan a esos ids.

La validacion del ingest tampoco duplica el dominio: lee estas tablas
(`EstablecimientoWriter.validate_integrity`), que es exactamente lo que
exigen los FKs.

Las tablas SIEMPRE existen cuando estos loaders corren: las crea la
migracion base, que ademas es donde se siembran. Estos entry points no las
crean ni toleran su ausencia.

Dos entry points, las mismas filas:
  - `load_seeds(conn, seeds_dir)`: DuckDB nativo (bootstrap del ingest).
  - `load_seeds_sqlalchemy(connection, seeds_dir)`: conexion SQLAlchemy
    (migraciones Alembic).
"""

import logging
from pathlib import Path

import polars as pl
from sqlalchemy import Connection, text

from src_etl.loaders.seed_validation import validate_seed

logger = logging.getLogger(__name__)

# (tabla, columna label, nombre para stats/logs)
TABLES = [
    ("denue_tipos_vialidad", "tipo_vial", "road_types"),
    ("denue_tipos_asentamiento", "tipo_asent", "settlement_types"),
    ("denue_tipos_unidad_economica", "tipounieco", "economic_unit_types"),
]
"""Catalogos de dominio que este loader administra, en orden de carga."""

SEED_FILES = {table: f"seed_{table.removeprefix('denue_')}.parquet" for table, *_ in TABLES}
"""Archivo parquet por catalogo en data/seeds."""


def _default_seeds_dir() -> Path:
    """data/seeds del proyecto (settings.SEEDS_DIR); import lazy para no
    acoplar el modulo a la config en import-time."""
    from src_etl.core.config import get_settings

    return Path(get_settings().SEEDS_DIR)


def seed_path(table: str, seeds_dir: Path | None = None) -> Path:
    """Ruta del parquet de un catalogo; falla claro si no esta versionado."""
    path = (seeds_dir or _default_seeds_dir()) / SEED_FILES[table]
    if not path.exists():
        raise FileNotFoundError(f"Seed requerido no encontrado: {path}")
    return path


def build_rows(seeds_dir: Path | None = None) -> dict[str, list[dict]]:
    """Filas de cada catalogo leidas de su parquet, ya validadas.

    La validacion corre aqui (no en el INSERT) para que un seed mal formado
    se detecte con un mensaje accionable antes de tocar la base.
    """
    rows: dict[str, list[dict]] = {}
    for table, *_ in TABLES:
        path = seed_path(table, seeds_dir)
        rows[table] = validate_seed(pl.read_parquet(path), table, str(path)).to_dicts()
    return rows


def load_seeds(conn, seeds_dir: Path | None = None, force: bool = False) -> dict[str, int]:
    """
    Siembra los catalogos via una conexion DuckDB nativa (idempotente).

    Inserta con INSERT OR IGNORE por PK: correr N veces es seguro y no
    dropea nada (los catalogos estan referenciados por
    FKs de `establecimientos`, que bloquearian un DELETE).

    Args:
        conn: DuckDB connection.
        seeds_dir: Directorio de seeds (default: settings.SEEDS_DIR).
        force: Sin efecto (la siembra ya es idempotente); se conserva por
            compatibilidad de firma con los demas loaders.

    Returns:
        dict: display_name -> numero de filas del catalogo.
    """
    rows_by_table = build_rows(seeds_dir)
    stats: dict[str, int] = {}

    for table, _label, display_name in TABLES:
        rows = rows_by_table[table]
        cols = list(rows[0].keys())
        placeholders = ", ".join(["?"] * len(cols))
        inserted = conn.executemany(
            f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
            [[r[c] for c in cols] for r in rows],
        ).fetchone()[0]
        if inserted:
            logger.info(f"   Seed loaded: {table} +{inserted:,} rows")
        stats[display_name] = len(rows)

    return stats


def load_seeds_sqlalchemy(
    connection: Connection,
    seeds_dir: Path | None = None,
    truncate: bool = True,
) -> dict[str, int]:
    """
    Siembra los catalogos via conexion SQLAlchemy (migraciones Alembic).

    Args:
        connection: SQLAlchemy connection.
        seeds_dir: Directorio de seeds (default: settings.SEEDS_DIR).
        truncate: Si True, vacia la tabla antes de sembrar cuando su
            contenido difiere del parquet.

    Returns:
        dict: display_name -> numero de filas del catalogo.
    """
    rows_by_table = build_rows(seeds_dir)
    stats: dict[str, int] = {}

    for table, _label, display_name in TABLES:
        rows = rows_by_table[table]
        cols = list(rows[0].keys())
        existing = connection.execute(text(f"SELECT {', '.join(cols)} FROM {table}")).fetchall()
        if [tuple(r) for r in existing] != [tuple(r[c] for c in cols) for r in rows]:
            if truncate:
                connection.execute(text(f"DELETE FROM {table}"))
            params = ", ".join(f":{c}" for c in cols)
            connection.execute(
                text(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({params})"), rows
            )
            print(f"   Loaded {len(rows):,} {display_name}")
        stats[display_name] = len(rows)

    return stats
