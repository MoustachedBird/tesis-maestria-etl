"""Escrituras masivas a PostgreSQL con `COPY ... FROM STDIN`.

Reemplaza al `INSERT` fila por fila como camino de carga del ETL: son 82.9M
filas y la diferencia no es marginal, es de orden de magnitud.

**Por que CSV y no el formato binario.** La decision abierta en
arquitectura.md Fase 3.3 planteaba binario (psycopg3) contra texto. Se
implementa CSV, y la razon es que el cuello no esta donde parecia:

- Binario obliga a `copy.write_row(tupla)` fila por fila, o sea un bucle
  Python sobre 82.9M tuplas. La adaptacion de cada valor es C, pero la
  iteracion y la construccion de tuplas no.
- CSV deja que Polars serialice el bloque entero en Rust, multihilo, y que
  psycopg mande los bytes de corrido. El cliente casi no participa; lo que
  se paga es el parseo del servidor, que es C y esta muy optimizado.

Se eligio psycopg3 igual porque soporta los dos caminos con la misma API de
bloques --si el parseo del servidor resulta ser el limite, cambiar a binario
es local a este modulo-- y porque psycopg2 esta en modo mantenimiento.

**De donde salen los errores.** El protocolo COPY exige el cursor del
driver, asi que las violaciones que detecta el servidor --una FK invalida,
una PK repetida, una fila sin particion-- llegan como excepciones de
`psycopg` y no envueltas en `sqlalchemy.exc`. Se deja asi a proposito: son
el error real, con su detalle completo y su propia jerarquia de tipos, y
envolverlas solo para uniformar el vocabulario perderia la distincion entre
un fallo de integridad y uno de transporte. Quien necesite atrapar ambos
mundos, `Exception`; quien quiera distinguir, `psycopg.errors`.

**NULL contra cadena vacia.** Los datos del DENUE vienen de encuesta y
traen campos vacios por todos lados, asi que distinguirlos importa. Polars
escribe ambos igual por default (nada entre comas), lo que los volveria
indistinguibles. Por eso se escribe con `quote_style="non_numeric"`: toda
cadena sale entrecomillada, de modo que `""` es cadena vacia y un hueco sin
comillas es NULL, que es exactamente como lo lee `COPY ... (FORMAT CSV)`.
"""

from __future__ import annotations

import io
from collections.abc import Iterator

import polars as pl
from sqlalchemy.engine import Connection

from src.shared.config import get_settings
from src.shared.db.base import DEFAULT_SCHEMA
from src.shared.logging import get_logger

logger = get_logger(__name__)

#: Estilo de comillas de Polars que hace distinguible NULL de "". No es
#: opcional: con cualquier otro, los campos vacios del DENUE se cargan como
#: NULL (o al reves) de forma silenciosa.
_QUOTE_STYLE = "non_numeric"


def _chunks(df: pl.DataFrame, chunk_rows: int) -> Iterator[pl.DataFrame]:
    """Parte un DataFrame en bloques de a lo mas `chunk_rows` filas.

    Args:
        df: DataFrame a partir.
        chunk_rows: filas por bloque; debe ser positivo.

    Yields:
        pl.DataFrame: vistas sucesivas del original, sin copiar los datos.
    """
    if chunk_rows <= 0:
        raise ValueError(f"chunk_rows debe ser positivo, se recibio {chunk_rows}")
    for offset in range(0, df.height, chunk_rows):
        yield df.slice(offset, chunk_rows)


def _csv_buffer(chunk: pl.DataFrame) -> bytes:
    """Serializa un bloque a CSV sin encabezado, listo para `COPY`.

    Args:
        chunk: bloque de filas.

    Returns:
        bytes: CSV con toda cadena entrecomillada (ver `_QUOTE_STYLE`).
    """
    buf = io.BytesIO()
    chunk.write_csv(buf, include_header=False, quote_style=_QUOTE_STYLE)
    return buf.getvalue()


def copy_dataframe(
    conn: Connection,
    df: pl.DataFrame,
    table: str,
    schema: str | None = DEFAULT_SCHEMA,
    columns: list[str] | None = None,
    chunk_rows: int | None = None,
) -> int:
    """Carga un DataFrame en una tabla con `COPY ... FROM STDIN`.

    Las columnas del `COPY` se toman del DataFrame, no de la tabla: el orden
    de `df.columns` es el que manda, y una columna que la tabla no tenga
    falla en el servidor en vez de desalinear los datos en silencio.

    No abre ni cierra transaccion. El llamador decide el alcance --que es lo
    que permite a `staging.py` cargar y validar antes de enganchar la
    particion.

    Args:
        conn: conexion SQLAlchemy sobre PostgreSQL.
        df: datos a cargar; sus columnas deben existir en la tabla destino.
        table: nombre de la tabla (sin calificar).
        schema: schema de la tabla; `None` deja el nombre sin calificar,
            que es como se alcanza una tabla temporal (viven en `pg_temp`,
            cuyo nombre cambia por sesion).
        columns: subconjunto y orden de columnas; default, todas las del df.
        chunk_rows: filas por bloque; default, `COPY_CHUNK_ROWS` de la config.

    Returns:
        int: filas cargadas (el alto del DataFrame).
    """
    if df.height == 0:
        logger.debug("copy_dataframe sin filas, no se emite COPY", table=table, schema=schema)
        return 0

    cols = columns if columns is not None else df.columns
    missing = set(cols) - set(df.columns)
    if missing:
        raise ValueError(f"columnas ausentes en el DataFrame: {sorted(missing)}")

    chunk_rows = chunk_rows if chunk_rows is not None else get_settings().COPY_CHUNK_ROWS
    quoted_cols = ", ".join(f'"{c}"' for c in cols)
    destino = f'"{schema}"."{table}"' if schema else f'"{table}"'
    statement = f"COPY {destino} ({quoted_cols}) FROM STDIN (FORMAT CSV, QUOTE '\"')"

    projected = df.select(cols)
    written = 0
    # `conn.connection` es el DBAPI wrapper de SQLAlchemy; `.driver_connection`
    # baja al objeto psycopg real, que es el que expone `.cursor().copy()`.
    raw = conn.connection.driver_connection
    with raw.cursor() as cur, cur.copy(statement) as copy:
        for chunk in _chunks(projected, chunk_rows):
            copy.write(_csv_buffer(chunk))
            written += chunk.height

    logger.info(
        "COPY completado",
        table=f"{schema}.{table}" if schema else table,
        rows=written,
        chunks=-(-projected.height // chunk_rows),
    )
    return written
