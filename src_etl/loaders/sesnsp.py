"""
Loader/DDL de las tablas SESNSP (incidencia delictiva municipal) en Silver.

Fuente: cifras de incidencia delictiva del fuero comun a nivel municipal
publicadas por el SESNSP (carpetas de investigacion por municipio, delito
y mes, 2015+). El CSV consolidado se descarga de datos.gob.mx (ver
extractors/sesnsp.py) y se normaliza a formato largo en Silver (ver
transformers/sesnsp.py).

Esquema relacional con FKs reales (misma politica que establecimientos):
- `sesnsp_delitos`: catalogo del delito (bien juridico -> tipo -> subtipo
  -> modalidad), ids surrogate append-only materializados en
  `data/seeds/seed_sesnsp_delitos.parquet` (comprometido en git, mismo
  patron que loaders/dominios.py). Append-only porque millones de filas de
  hechos referencian estos ids.
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
append-only asignado por orden de aparicion en el seed parquet."""

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

DELITO_KEY_COLUMNS = ["bien_juridico", "tipo", "subtipo", "modalidad"]
"""Clave natural del catalogo de delitos, en orden jerarquico SESNSP."""


def ensure_tables(conn: duckdb.DuckDBPyConnection) -> None:
    """Crea las tablas SESNSP en Silver si no existen (catalogo primero,
    porque la tabla de hechos la referencia por FK)."""
    conn.execute(SESNSP_DELITOS_DDL)
    conn.execute(SESNSP_INCIDENCIA_DDL)


def sync_delitos_seed(distinct_delitos: pl.DataFrame, seeds_dir: Path) -> pl.DataFrame:
    """
    Sincroniza el seed parquet del catalogo de delitos (append-only).

    Combinaciones nuevas encontradas en un corte se agregan al final con
    ids consecutivos (max(id)+1..), ordenadas por la clave natural para que
    la asignacion sea determinista. Las filas existentes NUNCA cambian de
    id: la tabla de hechos las referencia.

    Args:
        distinct_delitos: DataFrame con las combinaciones distintas del CSV
            (columnas DELITO_KEY_COLUMNS).
        seeds_dir: Directorio de seeds (data/seeds/).

    Returns:
        Catalogo completo (id + DELITO_KEY_COLUMNS) tras la sincronizacion.
    """
    seed_path = seeds_dir / SEED_DELITOS_FILE
    incoming = distinct_delitos.select(DELITO_KEY_COLUMNS).unique()

    if seed_path.exists():
        catalog = pl.read_parquet(seed_path)
        new_rows = incoming.join(catalog, on=DELITO_KEY_COLUMNS, how="anti")
        next_id = catalog["id"].max() + 1
    else:
        catalog = pl.DataFrame(schema={"id": pl.Int32, **{c: pl.Utf8 for c in DELITO_KEY_COLUMNS}})
        new_rows = incoming
        next_id = 1

    if new_rows.height > 0:
        new_rows = (
            new_rows.sort(DELITO_KEY_COLUMNS)
            .with_row_index("id", offset=next_id)
            .with_columns(pl.col("id").cast(pl.Int32))
            .select(["id", *DELITO_KEY_COLUMNS])
        )
        catalog = pl.concat([catalog, new_rows])
        catalog.write_parquet(seed_path)

    return catalog


def load_delitos_catalog(conn: duckdb.DuckDBPyConnection, seeds_dir: Path) -> int:
    """
    Carga en `sesnsp_delitos` las filas del seed parquet que falten.

    Idempotente y append-only: nunca borra ni modifica filas existentes
    (estan referenciadas por FK desde los hechos).

    Returns:
        Numero de filas insertadas.
    """
    seed_path = seeds_dir / SEED_DELITOS_FILE
    if not seed_path.exists():
        raise FileNotFoundError(f"Seed de delitos SESNSP no encontrado: {seed_path}")

    before = conn.execute("SELECT COUNT(*) FROM sesnsp_delitos").fetchone()[0]
    conn.execute(
        f"""
        INSERT INTO sesnsp_delitos
        SELECT s.* FROM '{seed_path}' s
        ANTI JOIN sesnsp_delitos d ON s.id = d.id
        """
    )
    after = conn.execute("SELECT COUNT(*) FROM sesnsp_delitos").fetchone()[0]
    return after - before


def sync_municipios_from_seed(conn: duckdb.DuckDBPyConnection, seeds_dir: Path) -> int:
    """
    Inserta en `denue_municipios` las filas del seed geografico que falten.

    Necesario en Silvers creados antes de agregar los municipios sinteticos
    del SESNSP (998/999) al seed. Idempotente: solo agrega, jamas borra.

    Returns:
        Numero de municipios insertados.
    """
    seed_path = seeds_dir / "seed_municipios.parquet"
    if not seed_path.exists():
        raise FileNotFoundError(f"Seed de municipios no encontrado: {seed_path}")

    before = conn.execute("SELECT COUNT(*) FROM denue_municipios").fetchone()[0]
    conn.execute(
        f"""
        INSERT INTO denue_municipios
        SELECT s.* FROM '{seed_path}' s
        ANTI JOIN denue_municipios m
            ON s.cve_ent = m.cve_ent AND s.cve_mun = m.cve_mun
        """
    )
    after = conn.execute("SELECT COUNT(*) FROM denue_municipios").fetchone()[0]
    return after - before
