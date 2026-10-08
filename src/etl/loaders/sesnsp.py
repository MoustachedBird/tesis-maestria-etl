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

import duckdb
import polars as pl

from src.etl.loaders import seeds
from src.etl.loaders.seeds import read_seed, seed_sqlalchemy

SESNSP_DELITOS_DDL = """
CREATE TABLE IF NOT EXISTS sesnsp_delitos (
    id INTEGER PRIMARY KEY,
    bien_juridico VARCHAR NOT NULL,
    tipo VARCHAR NOT NULL,
    subtipo VARCHAR NOT NULL,
    modalidad VARCHAR NOT NULL,
    UNIQUE (bien_juridico, tipo, subtipo, modalidad)
)
"""
"""DDL del catalogo de delitos SESNSP (~98 filas). El id es surrogate
append-only; el orden y los ids los define el seed parquet."""

SESNSP_INCIDENCIA_DDL = """
CREATE TABLE IF NOT EXISTS sesnsp_incidencia_municipal (
    cve_ent VARCHAR NOT NULL,
    cve_mun VARCHAR NOT NULL,
    delito_id INTEGER NOT NULL,
    fecha DATE NOT NULL,
    carpetas INTEGER NOT NULL,
    PRIMARY KEY (cve_ent, cve_mun, delito_id, fecha),
    FOREIGN KEY (delito_id) REFERENCES sesnsp_delitos(id),
    FOREIGN KEY (cve_ent) REFERENCES denue_entidades(cve_ent),
    FOREIGN KEY (cve_ent, cve_mun) REFERENCES denue_municipios(cve_ent, cve_mun)
)
"""
"""DDL de la tabla de hechos (~30M filas en formato largo): carpetas de
investigacion por municipio x delito x mes. `fecha` usa la convencion
DATE YYYY-MM-01 del proyecto (igual que fecha_alta_date/period en DENUE).
Cada corte mensual del SESNSP revisa cifras historicas, por lo que la
tabla se reconstruye completa en cada ingesta (ver transformers/sesnsp.py);
DuckDB reconstruye los indices PK/FK en el mismo INSERT masivo."""

SEED_DELITOS_FILE = "seed_sesnsp_delitos.parquet"
"""Nombre del parquet seed del catalogo de delitos dentro de data/seeds/."""

DELITOS_TABLE = "sesnsp_delitos"
"""Tabla del catalogo (poblada desde el seed en la migracion base SESNSP)."""

DELITO_KEY_COLUMNS = ["bien_juridico", "tipo", "subtipo", "modalidad"]
"""Clave natural del catalogo de delitos, en orden jerarquico SESNSP."""


def ensure_tables(conn: duckdb.DuckDBPyConnection) -> None:
    """Crea las tablas SESNSP en Silver si no existen (catalogo primero,
    porque la tabla de hechos la referencia por FK)."""
    conn.execute(SESNSP_DELITOS_DDL)
    conn.execute(SESNSP_INCIDENCIA_DDL)


def delitos_seed_path(seeds_dir: Path | None = None) -> Path:
    """Ruta del seed del catalogo (falla claro si no esta versionado)."""
    return seeds.seed_path(SEED_DELITOS_FILE, seeds_dir)


def build_delitos_rows(seeds_dir: Path | None = None) -> list[dict]:
    """Filas del catalogo leidas del seed parquet, ya validadas."""
    return read_seed(DELITOS_TABLE, SEED_DELITOS_FILE, seeds_dir).to_dicts()


def load_seeds_sqlalchemy(
    connection, seeds_dir: Path | None = None, truncate: bool = True
) -> dict[str, int]:
    """Siembra `sesnsp_delitos` desde el parquet (entry point de la migracion)."""
    df = read_seed(DELITOS_TABLE, SEED_DELITOS_FILE, seeds_dir)
    seed_sqlalchemy(connection, DELITOS_TABLE, df, truncate=truncate)
    return {DELITOS_TABLE: df.height}


def validate_delitos_del_csv(
    conn: duckdb.DuckDBPyConnection, distinct_delitos: pl.DataFrame
) -> None:
    """
    Fail-fast: toda combinacion del CSV debe existir ya en el catalogo.

    Misma politica que SCIAN, geografia y los dominios en DENUE: el seed es
    la fuente de verdad, asi que un delito nuevo de un corte anual se
    reporta con sus valores para darlo de alta en el parquet — no se
    auto-inventa un id, porque los ids son append-only y millones de hechos
    los referencian por FK.

    Args:
        conn: Conexion DuckDB a Silver.
        distinct_delitos: Combinaciones distintas leidas del CSV del corte.

    Raises:
        ValueError: Nombrando las combinaciones que faltan en el catalogo.
    """
    claves = distinct_delitos.select(DELITO_KEY_COLUMNS).unique()  # noqa: F841
    on = " AND ".join(f"k.{c} = d.{c}" for c in DELITO_KEY_COLUMNS)
    faltantes = conn.execute(
        f"SELECT {', '.join('k.' + c for c in DELITO_KEY_COLUMNS)} FROM claves k "
        f"LEFT JOIN {DELITOS_TABLE} d ON {on} "
        f"WHERE d.{DELITO_KEY_COLUMNS[0]} IS NULL LIMIT 10"
    ).fetchall()
    if faltantes:
        raise ValueError(
            f"❌ delitos fuera del catalogo {DELITOS_TABLE}: {faltantes} "
            f"(agregar a data/seeds/{SEED_DELITOS_FILE} con id nuevo al final, "
            f"y correr `make db-migrate`)."
        )


def validate_municipios_del_csv(
    conn: duckdb.DuckDBPyConnection, distinct_municipios: pl.DataFrame
) -> None:
    """
    Fail-fast: todo municipio del CSV debe existir ya en `denue_municipios`.

    Incluye los municipios sinteticos del SESNSP (cve_mun 998 "NO
    ESPECIFICADO" y 999 "OTROS MUNICIPIOS"), que viven en
    `seed_municipios.parquet` como filas normales del catalogo geografico
    para que la FK compuesta se satisfaga fisicamente.

    Raises:
        ValueError: Nombrando los (cve_ent, cve_mun) que faltan.
    """
    claves = distinct_municipios.select(["cve_ent", "cve_mun"]).unique()  # noqa: F841
    faltantes = conn.execute(
        "SELECT k.cve_ent, k.cve_mun FROM claves k "
        "LEFT JOIN denue_municipios m "
        "  ON k.cve_ent = m.cve_ent AND k.cve_mun = m.cve_mun "
        "WHERE m.cve_ent IS NULL LIMIT 10"
    ).fetchall()
    if faltantes:
        raise ValueError(
            f"❌ municipio fuera del catalogo denue_municipios: {faltantes} "
            "(agregar a data/seeds/seed_municipios.parquet y correr `make db-migrate`)."
        )
