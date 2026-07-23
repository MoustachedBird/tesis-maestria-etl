"""
Personnel strata (per_ocu) seed loader.

Siembra `denue_estratos_personal` DESDE `data/seeds/seed_estratos_personal.
parquet`, que es la FUENTE DE VERDAD: id, etiqueta INEGI, cotas de empleados
y categoria de tamano viven en el archivo, no duplicadas en codigo Python.
Agregar o corregir un estrato es editar el parquet.

Surrogate key: `id INTEGER PRIMARY KEY` con `per_ocu` UNIQUE;
`establecimientos.per_ocu_id` lo referencia con FOREIGN KEY real. Por eso el
catalogo es append-only: millones de filas apuntan a esos ids.

La tabla SIEMPRE existe cuando estos loaders corren: la crea la migracion
base, que ademas es donde se siembra.

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

TABLE_NAME = "denue_estratos_personal"
DISPLAY_NAME = "personnel_strata"
SEED_FILE = "seed_estratos_personal.parquet"
"""Archivo seed en data/seeds (patron de geografia/SCIAN/dominios)."""


def _default_seeds_dir() -> Path:
    """data/seeds del proyecto (settings.SEEDS_DIR); import lazy para no
    acoplar el modulo a la config en import-time."""
    from src_etl.core.config import get_settings

    return Path(get_settings().SEEDS_DIR)


def seed_path(seeds_dir: Path | None = None) -> Path:
    """Ruta del parquet del catalogo; falla claro si no esta versionado."""
    path = (seeds_dir or _default_seeds_dir()) / SEED_FILE
    if not path.exists():
        raise FileNotFoundError(f"Seed requerido no encontrado: {path}")
    return path


def build_rows(seeds_dir: Path | None = None) -> list[dict]:
    """Filas del catalogo leidas del parquet, ya validadas."""
    path = seed_path(seeds_dir)
    return validate_seed(pl.read_parquet(path), TABLE_NAME, str(path)).to_dicts()


def load_seeds(conn, seeds_dir: Path | None = None, force: bool = False) -> dict[str, int]:
    """
    Siembra el catalogo via una conexion DuckDB nativa (idempotente).

    Inserta con INSERT OR IGNORE por PK: correr N veces es seguro y no
    dropea nada (el catalogo esta referenciado por FKs
    de `establecimientos`, que bloquearian un DELETE).

    Args:
        conn: DuckDB connection.
        seeds_dir: Directorio de seeds (default: settings.SEEDS_DIR).
        force: Sin efecto (la siembra ya es idempotente); se conserva por
            compatibilidad de firma con los demas loaders.

    Returns:
        dict: display_name -> numero de filas del catalogo.
    """
    rows = build_rows(seeds_dir)
    cols = list(rows[0].keys())
    placeholders = ", ".join(["?"] * len(cols))
    inserted = conn.executemany(
        f"INSERT OR IGNORE INTO {TABLE_NAME} ({', '.join(cols)}) VALUES ({placeholders})",
        [[r[c] for c in cols] for r in rows],
    ).fetchone()[0]
    if inserted:
        logger.info(f"   Seed loaded: {TABLE_NAME} +{inserted:,} rows")
    return {DISPLAY_NAME: len(rows)}


def load_seeds_sqlalchemy(
    connection: Connection,
    seeds_dir: Path | None = None,
    truncate: bool = True,
) -> dict[str, int]:
    """
    Siembra el catalogo via conexion SQLAlchemy (migraciones Alembic).

    Args:
        connection: SQLAlchemy connection.
        seeds_dir: Directorio de seeds (default: settings.SEEDS_DIR).
        truncate: Si True, vacia la tabla antes de sembrar cuando su
            contenido difiere del parquet.

    Returns:
        dict: display_name -> numero de filas del catalogo.
    """
    rows = build_rows(seeds_dir)
    cols = list(rows[0].keys())
    existing = connection.execute(text(f"SELECT {', '.join(cols)} FROM {TABLE_NAME}")).fetchall()
    if [tuple(r) for r in existing] != [tuple(r[c] for c in cols) for r in rows]:
        if truncate:
            connection.execute(text(f"DELETE FROM {TABLE_NAME}"))
        params = ", ".join(f":{c}" for c in cols)
        connection.execute(
            text(f"INSERT INTO {TABLE_NAME} ({', '.join(cols)}) VALUES ({params})"), rows
        )
        print(f"   Loaded {len(rows):,} {DISPLAY_NAME}")
    return {DISPLAY_NAME: len(rows)}
