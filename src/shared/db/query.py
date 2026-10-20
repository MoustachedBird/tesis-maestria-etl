"""Lecturas analiticas que devuelven DataFrames.

El ETL consulta de dos formas distintas y conviene no confundirlas. Para
leer o escribir *entidades* --una corrida, un catalogo-- el ORM es lo
correcto. Para las consultas analiticas --cobertura por periodo, cadenas de
identidad, la muestra del eval-- lo que se quiere es SQL legible y un
DataFrame de vuelta, y envolverlas en el ORM solo las haria ilegibles.

Este modulo es ese segundo camino: una funcion, para no repetir el puente
entre el cursor y Polars en cada sitio que lo necesita.
"""

from __future__ import annotations

from typing import Any

import polars as pl
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.sql.elements import TextClause


def fetch_df(
    conn: Connection,
    sql: str | TextClause,
    params: dict[str, Any] | None = None,
) -> pl.DataFrame:
    """Ejecuta una consulta y devuelve el resultado como DataFrame.

    Los parametros van con nombre (`:periodo`), no posicionales: una
    consulta analitica suele repetir el mismo valor en varios sitios, y
    nombrarlos evita contar interrogaciones.

    Un resultado vacio devuelve un DataFrame vacio pero CON las columnas de
    la consulta, no uno sin esquema: asi el codigo que sigue puede
    seleccionar columnas sin ramificar por el caso vacio.

    Args:
        conn: conexion SQLAlchemy.
        sql: consulta, como texto o `text()`.
        params: parametros nombrados.

    Returns:
        pl.DataFrame: filas del resultado.
    """
    resultado = conn.execute(text(sql) if isinstance(sql, str) else sql, params or {})
    columnas = list(resultado.keys())
    filas = resultado.all()
    if not filas:
        return pl.DataFrame(schema=dict.fromkeys(columnas, pl.Null))
    return pl.DataFrame(filas, schema=columnas, orient="row")
