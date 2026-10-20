"""Resolucion de catalogos: de las etiquetas del CSV a las llaves de la tabla.

El CSV de INEGI trae etiquetas ("CALLE", "FIJO", "11 A 30 PERSONAS") y la
tabla de hechos guarda enteros que apuntan a los catalogos. Traducir una por
otra es trabajo de aqui, y con el va el fail-fast que lo protege.

**Por que validar si ya hay FKs.** Un valor fuera de catalogo no produce un
error al traducirlo: produce un NULL, y el NULL es legitimo --hay
establecimientos sin tipo de vialidad. Sin la validacion, un valor nuevo que
INEGI empiece a publicar se perderia en silencio en millones de filas. Las
validaciones de geografia y SCIAN si acabarian en violacion de FK, pero el
mensaje del servidor no dice que nivel falto ni que valores eran; estas
paran antes y lo dicen.

Los valores permitidos se leen de las tablas, no de una lista en Python: el
dominio vive en los seeds, y agregar un valor nuevo debe ser tocar un
parquet y nada mas.
"""

from __future__ import annotations

import polars as pl
from sqlalchemy import select
from sqlalchemy.engine import Connection

from src.etl.utils.denue_schema import DOMAIN_ID_COLUMNS
from src.shared.db.base import get_table

#: Niveles de geografia, de menor a mayor detalle, con las columnas que
#: identifican cada uno. Se validan por separado para que el error diga si
#: falto la entidad, el municipio o la localidad.
_NIVELES_GEOGRAFIA: list[tuple[list[str], str, str]] = [
    (["cve_ent"], "denue_entidades", "entidad"),
    (["cve_ent", "cve_mun"], "denue_municipios", "municipio"),
    (["cve_ent", "cve_mun", "cve_loc"], "denue_localidades", "localidad"),
]

#: Cuantos valores ofensores se muestran en el mensaje de error. Suficientes
#: para ver el patron, no tantos que el log sea ilegible.
_MAX_EJEMPLOS = 10


class CatalogoDesactualizado(ValueError):
    """El CSV trae un valor que ningun catalogo conoce.

    Casi siempre significa que INEGI publico algo nuevo y hay que agregarlo
    al seed correspondiente. El mensaje dice que columna, que valores y que
    catalogo, para que la accion sea directa.
    """


def _valores_de(conn: Connection, tabla: str, columna: str) -> set:
    """Valores presentes hoy en una columna de catalogo.

    Args:
        conn: conexion de lectura.
        tabla: catalogo a leer.
        columna: columna cuyos valores se devuelven.

    Returns:
        set: valores no nulos.
    """
    catalogo = get_table(tabla)
    return set(conn.execute(select(catalogo.c[columna])).scalars().all()) - {None}


def validate_domain_labels(conn: Connection, df: pl.DataFrame) -> None:
    """Falla si alguna etiqueta de dominio no esta en su catalogo.

    Args:
        conn: conexion de lectura.
        df: filas del CSV, con las etiquetas sin traducir.

    Raises:
        CatalogoDesactualizado: con la columna, los valores y el catalogo.
    """
    for columna, (_id, tabla, etiqueta) in DOMAIN_ID_COLUMNS.items():
        if columna not in df.columns:
            continue
        permitidos = _valores_de(conn, tabla, etiqueta)
        intrusos = (
            df.select(columna)
            .unique()
            .drop_nulls()
            .filter(~pl.col(columna).is_in(list(permitidos)))
            .to_series()
            .to_list()
        )
        if intrusos:
            raise CatalogoDesactualizado(
                f"'{columna}': los valores {intrusos[:_MAX_EJEMPLOS]} no estan en "
                f"{tabla}. Si son legitimos, agregarlos al seed y re-correr."
            )


def validate_codigo_act(conn: Connection, df: pl.DataFrame) -> None:
    """Falla si algun `codigo_act` no esta en el catalogo SCIAN.

    SCIAN evoluciona entre cortes, asi que un codigo nuevo es esperable --y
    debe detener la carga, no colarse.

    Args:
        conn: conexion de lectura.
        df: filas del CSV.

    Raises:
        CatalogoDesactualizado: con los codigos desconocidos.
    """
    if "codigo_act" not in df.columns:
        return
    conocidos = _valores_de(conn, "denue_actividades_economicas", "codigo_act")
    intrusos = (
        df.select("codigo_act")
        .unique()
        .drop_nulls()
        .filter(~pl.col("codigo_act").is_in(list(conocidos)))
        .to_series()
        .to_list()
    )
    if intrusos:
        raise CatalogoDesactualizado(
            f"codigo_act fuera de SCIAN: {intrusos[:_MAX_EJEMPLOS]}. "
            "Actualizar el seed de denue_actividades_economicas y re-correr."
        )


def validate_geografia(conn: Connection, df: pl.DataFrame) -> None:
    """Falla si alguna clave geografica no esta en su catalogo.

    Cada nivel se valida solo si el CSV trae todas sus columnas: los datos
    de encuesta pueden venir incompletos, y la ausencia de `cve_loc` no es
    un error.

    Args:
        conn: conexion de lectura.
        df: filas del CSV.

    Raises:
        CatalogoDesactualizado: diciendo que nivel falto.
    """
    for columnas, tabla, nombre in _NIVELES_GEOGRAFIA:
        if not set(columnas) <= set(df.columns):
            continue

        catalogo = get_table(tabla)
        conocidas = {
            tuple(fila)
            for fila in conn.execute(select(*[catalogo.c[c] for c in columnas])).all()
        }
        presentes = {
            tuple(fila)
            for fila in df.select(columnas).unique().drop_nulls().iter_rows()
        }
        intrusas = sorted(presentes - conocidas)
        if intrusas:
            raise CatalogoDesactualizado(
                f"{nombre} fuera de {tabla}: {intrusas[:_MAX_EJEMPLOS]}. "
                "Agregar al seed de data/seeds y re-correr."
            )


def map_labels_to_ids(conn: Connection, df: pl.DataFrame) -> pl.DataFrame:
    """Traduce las etiquetas de dominio a las llaves que guarda la tabla.

    La columna de etiqueta se reemplaza por la de id (`tipo_vial` ->
    `tipo_vial_id`). Una etiqueta nula da id nulo, que es valido.

    Correr `validate_domain_labels` antes no es opcional: sin ella, una
    etiqueta desconocida tambien da id nulo y se vuelve indistinguible de
    un dato ausente.

    Args:
        conn: conexion de lectura.
        df: filas con las etiquetas.

    Returns:
        pl.DataFrame: con las columnas de id en lugar de las de etiqueta.
    """
    for columna, (columna_id, tabla, etiqueta) in DOMAIN_ID_COLUMNS.items():
        if columna not in df.columns:
            continue

        catalogo = get_table(tabla)
        pares = conn.execute(select(catalogo.c[etiqueta], catalogo.c["id"])).all()
        traduccion = pl.DataFrame(
            {etiqueta: [p[0] for p in pares], columna_id: [p[1] for p in pares]},
            schema={etiqueta: pl.Utf8, columna_id: pl.Int32},
        )

        df = (
            df.join(traduccion, left_on=columna, right_on=etiqueta, how="left")
            .drop(columna)
            .drop(etiqueta, strict=False)
        )
    return df


def resolve(conn: Connection, df: pl.DataFrame) -> pl.DataFrame:
    """Valida contra los catalogos y traduce las etiquetas a llaves.

    El orden importa: validar primero, traducir despues. Al reves, una
    etiqueta desconocida ya seria un NULL y no habria nada que reportar.

    Args:
        conn: conexion de lectura.
        df: filas del CSV, limpias.

    Returns:
        pl.DataFrame: listo para el COPY.

    Raises:
        CatalogoDesactualizado: si algun valor no esta en su catalogo.
    """
    validate_domain_labels(conn, df)
    validate_codigo_act(conn, df)
    validate_geografia(conn, df)
    return map_labels_to_ids(conn, df)
