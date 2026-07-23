"""
Validacion de los seeds parquet contra las restricciones reales de la tabla.

Los parquet de `data/seeds` son la fuente de verdad de los catalogos, asi que
un archivo mal formado (id repetido, etiqueta duplicada, columna obligatoria
en NULL) es un error de datos que debe detenerse EN LA MIGRACION, con un
mensaje que diga archivo, columna y valores ofensores — no convertirse en un
error criptico de constraint a media carga, ni peor: desaparecer en silencio
porque la siembra usa INSERT OR IGNORE.

Las restricciones NO se declaran aqui: se leen del modelo ORM
(`Base.metadata`), que es la definicion canonica del esquema. Asi esta
validacion no puede desincronizarse de la tabla que valida.
"""

import polars as pl
from sqlalchemy import UniqueConstraint


def _table(table_name: str):
    """Objeto Table del metadata del ORM para la tabla dada."""
    from src_etl.db.base import Base
    from src_etl.db.models import Establecimiento  # noqa: F401  (puebla el metadata)

    return Base.metadata.tables[table_name]


def _unique_key_sets(table) -> list[tuple[str, ...]]:
    """Conjuntos de columnas que deben ser unicos: PK, UNIQUE de tabla y
    columnas marcadas unique=True."""
    keys: list[tuple[str, ...]] = []
    pk = tuple(c.name for c in table.primary_key.columns)
    if pk:
        keys.append(pk)
    for con in table.constraints:
        if isinstance(con, UniqueConstraint):
            cols = tuple(c.name for c in con.columns)
            if cols and cols not in keys:
                keys.append(cols)
    for col in table.columns:
        if col.unique and (col.name,) not in keys:
            keys.append((col.name,))
    return keys


def validate_seed(df: pl.DataFrame, table_name: str, source: str) -> pl.DataFrame:
    """
    Valida un seed contra las restricciones de su tabla; devuelve el df.

    Comprueba, en este orden: que existan las columnas obligatorias, que no
    haya NULLs donde la tabla exige NOT NULL, y que cada clave unica (PK,
    UNIQUE de tabla y columnas unique=True) no tenga repetidos.

    Args:
        df: Filas leidas del parquet.
        table_name: Tabla destino (para leer sus restricciones del ORM).
        source: Ruta/nombre del archivo, para el mensaje de error.

    Returns:
        El mismo DataFrame, si pasa todas las comprobaciones.

    Raises:
        ValueError: Con archivo, columna(s) y valores ofensores concretos.
    """
    table = _table(table_name)
    model_cols = {c.name for c in table.columns}

    faltantes = sorted(
        c.name
        for c in table.columns
        if not c.nullable and c.name not in df.columns and c.default is None
    )
    if faltantes:
        raise ValueError(
            f"❌ Seed invalido {source}: faltan columnas obligatorias de "
            f"{table_name}: {faltantes}"
        )

    if df.height == 0:
        raise ValueError(f"❌ Seed invalido {source}: no tiene filas")

    for col in table.columns:
        if col.nullable or col.name not in df.columns:
            continue
        nulos = df.filter(pl.col(col.name).is_null()).height
        if nulos:
            raise ValueError(
                f"❌ Seed invalido {source}: {nulos} fila(s) con "
                f"{table_name}.{col.name} en NULL (la tabla lo exige NOT NULL)"
            )

    for key in _unique_key_sets(table):
        cols = [c for c in key if c in df.columns]
        if len(cols) != len(key):
            continue
        dup = (
            df.group_by(cols)
            .len()
            .filter(pl.col("len") > 1)
            .sort(cols)
        )
        if dup.height:
            ejemplos = dup.head(5).select(cols).rows()
            raise ValueError(
                f"❌ Seed invalido {source}: {dup.height} valor(es) repetido(s) "
                f"en {table_name}({', '.join(cols)}), que la tabla exige UNIQUE. "
                f"Ejemplos: {ejemplos}"
            )

    extra = sorted(set(df.columns) - model_cols)
    if extra:
        raise ValueError(
            f"❌ Seed invalido {source}: columnas que no existen en "
            f"{table_name}: {extra}"
        )

    return df
