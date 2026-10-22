"""
Loader/DDL de las tablas SESNSP (incidencia delictiva municipal) en Silver.

Fuente: cifras de incidencia delictiva del fuero comun a nivel municipal
publicadas por el SESNSP (carpetas de investigacion por municipio, delito
y mes, 2015+). El CSV consolidado se descarga de datos.gob.mx (ver
extractors/sesnsp.py) y se normaliza a formato largo en Silver (ver
transformers/sesnsp.py).

Esquema relacional con FKs reales (misma politica que establecimientos):
- `sesnsp_delitos`: catalogo del delito (bien juridico -> tipo -> subtipo
  -> modalidad). Su FUENTE DE VERDAD es
  `data/seeds/seed_sesnsp_delitos.parquet` (versionado en git, mismo patron
  que los catalogos DENUE): la migracion base SESNSP lo siembra desde ahi.
  Los ids son append-only porque millones de hechos los referencian por FK,
  asi que un delito nuevo se agrega AL FINAL del parquet.
- `sesnsp_incidencia_municipal`: hechos mensuales; comparte las claves
  geograficas de DENUE (`cve_ent`/`cve_mun` zero-padded) para poder cruzar
  establecimientos con incidencia delictiva por municipio y periodo.

Los municipios sinteticos del SESNSP (cve_mun 998 "NO ESPECIFICADO" y 999
"OTROS MUNICIPIOS") viven en `seed_municipios.parquet` como filas normales
del catalogo geografico, de modo que la FK compuesta a denue_municipios se
satisface fisicamente.
"""

from pathlib import Path

import polars as pl
from sqlalchemy import select
from sqlalchemy.engine import Connection

from src.etl.loaders import seeds
from src.etl.loaders.seeds import read_seed, seed_table
from src.etl.transformers.catalogs import CatalogoDesactualizado
from src.shared.db.base import get_table

SEED_DELITOS_FILE = "seed_sesnsp_delitos.parquet"
"""Nombre del parquet seed del catalogo de delitos dentro de data/seeds/."""

DELITOS_TABLE = "sesnsp_delitos"
"""Tabla del catalogo (poblada desde el seed en la migracion base SESNSP)."""

DELITO_KEY_COLUMNS = ["bien_juridico", "tipo", "subtipo", "modalidad"]
"""Clave natural del catalogo de delitos, en orden jerarquico SESNSP."""


def delitos_seed_path(seeds_dir: Path | None = None) -> Path:
    """Ruta del seed del catalogo (falla claro si no esta versionado)."""
    return seeds.seed_path(SEED_DELITOS_FILE, seeds_dir)


def build_delitos_rows(seeds_dir: Path | None = None) -> list[dict]:
    """Filas del catalogo leidas del seed parquet, ya validadas."""
    return read_seed(DELITOS_TABLE, SEED_DELITOS_FILE, seeds_dir).to_dicts()


def load_seeds(connection, seeds_dir: Path | None = None, truncate: bool = True) -> dict[str, int]:
    """Siembra `sesnsp_delitos` desde el parquet (entry point de la migracion)."""
    df = read_seed(DELITOS_TABLE, SEED_DELITOS_FILE, seeds_dir)
    seed_table(connection, DELITOS_TABLE, df, truncate=truncate)
    return {DELITOS_TABLE: df.height}


def validate_delitos_del_csv(conn: Connection, distinct_delitos: pl.DataFrame) -> None:
    """
    Fail-fast: toda combinacion del CSV debe existir ya en el catalogo.

    Misma politica que SCIAN, geografia y los dominios de DENUE: el seed es
    la fuente de verdad, asi que un delito nuevo de un corte se reporta con
    sus valores para darlo de alta en el parquet. No se auto-inventa un id,
    porque los ids son append-only y millones de hechos los referencian.

    Args:
        conn: conexion de lectura.
        distinct_delitos: combinaciones distintas leidas del CSV.

    Raises:
        CatalogoDesactualizado: nombrando las que faltan.
    """
    catalogo = get_table(DELITOS_TABLE)
    conocidos = {
        tuple(fila)
        for fila in conn.execute(select(*[catalogo.c[c] for c in DELITO_KEY_COLUMNS])).all()
    }
    presentes = {
        tuple(fila) for fila in distinct_delitos.select(DELITO_KEY_COLUMNS).unique().iter_rows()
    }
    faltantes = sorted(presentes - conocidos)
    if faltantes:
        raise CatalogoDesactualizado(
            f"delitos fuera de {DELITOS_TABLE}: {faltantes[:10]}. Agregar a "
            f"data/seeds/{SEED_DELITOS_FILE} con id nuevo al final y re-migrar."
        )


def validate_municipios_del_csv(conn: Connection, distinct_municipios: pl.DataFrame) -> None:
    """
    Fail-fast: todo municipio del CSV debe existir ya en `denue_municipios`.

    Incluye los municipios sinteticos del SESNSP --998 "NO ESPECIFICADO" y
    999 "OTROS MUNICIPIOS"--, que viven en `seed_municipios.parquet` como
    filas normales para que la FK compuesta se satisfaga fisicamente.

    Args:
        conn: conexion de lectura.
        distinct_municipios: claves distintas leidas del CSV.

    Raises:
        CatalogoDesactualizado: nombrando las que faltan.
    """
    catalogo = get_table("denue_municipios")
    conocidos = {
        tuple(fila)
        for fila in conn.execute(select(catalogo.c["cve_ent"], catalogo.c["cve_mun"])).all()
    }
    presentes = {
        tuple(fila)
        for fila in distinct_municipios.select(["cve_ent", "cve_mun"]).unique().iter_rows()
    }
    faltantes = sorted(presentes - conocidos)
    if faltantes:
        raise CatalogoDesactualizado(
            f"municipio fuera de denue_municipios: {faltantes[:10]}. Agregar a "
            "data/seeds/seed_municipios.parquet y re-migrar."
        )
