"""Particiones de `silver.establecimientos`, una por corte del DENUE.

La tabla esta particionada por LIST sobre `period` (ver el modelo en
`models/silver.py`). Eso convierte al periodo en la unidad de recuperacion
que pedia arquitectura.md §2.4: un corte que se carga mal se desengancha y
se vuelve a cargar sin tocar los otros 19, en vez de reconstruir las 82.9M
filas del warehouse completo.

Las particiones no se declaran en el modelo ORM --son objetos que nacen con
cada corte nuevo, no esquema fijo-- asi que se crean desde aqui, con DDL
parametrizado por fecha y nunca por interpolacion de texto libre.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Connection

from src.shared.db.base import DEFAULT_SCHEMA

#: Tabla particionada. Unica por ahora; si se suman mas, esto pasa a ser un
#: parametro en vez de una constante del modulo.
PARTITIONED_TABLE = "establecimientos"

#: Prefijo de las particiones hijas. El nombre completo es
#: `establecimientos_YYYY_MM` (p. ej. `establecimientos_2024_11`).
PARTITION_PREFIX = "establecimientos"


def partition_name(period: date) -> str:
    """Nombre de la particion de un corte.

    Args:
        period: fecha del corte (primer dia del mes, por convencion DENUE).

    Returns:
        str: identificador sin comillas, p. ej. `establecimientos_2024_11`.
    """
    return f"{PARTITION_PREFIX}_{period.year:04d}_{period.month:02d}"


def qualified_partition(period: date, schema: str = DEFAULT_SCHEMA) -> str:
    """Nombre calificado y entrecomillado de la particion.

    Args:
        period: fecha del corte.
        schema: schema donde vive la tabla particionada.

    Returns:
        str: p. ej. `"silver"."establecimientos_2024_11"`.
    """
    return f'"{schema}"."{partition_name(period)}"'


def create_partition(
    conn: Connection,
    period: date,
    schema: str = DEFAULT_SCHEMA,
    if_not_exists: bool = True,
) -> str:
    """Crea la particion de un corte, enganchada a la tabla padre.

    Idempotente por default: volver a llamarla para un corte existente no
    hace nada. Es el camino normal del ingest, que no deberia tener que
    saber si el corte ya se vio antes.

    Args:
        conn: conexion con permisos de DDL sobre el schema.
        period: fecha del corte.
        schema: schema de la tabla particionada.
        if_not_exists: si False, falla cuando la particion ya existe.

    Returns:
        str: nombre de la particion creada o ya existente.
    """
    exists_clause = "IF NOT EXISTS " if if_not_exists else ""
    # `period` entra como literal con cast explicito y no como bind param:
    # PostgreSQL no admite parametros en el DDL de particiones. El valor es
    # un `datetime.date`, no texto del usuario, asi que su repr ISO es
    # segura por construccion.
    conn.execute(
        text(
            f"CREATE TABLE {exists_clause}{qualified_partition(period, schema)} "
            f'PARTITION OF "{schema}"."{PARTITIONED_TABLE}" '
            f"FOR VALUES IN (DATE '{period.isoformat()}')"
        )
    )
    return partition_name(period)


def detach_partition(conn: Connection, period: date, schema: str = DEFAULT_SCHEMA) -> None:
    """Desengancha la particion de un corte sin borrar sus datos.

    El paso previo a recargar un corte: la particion queda como tabla suelta
    (inspeccionable, respaldable) y la tabla padre deja de verla en el acto.

    Args:
        conn: conexion con permisos de DDL.
        period: fecha del corte.
        schema: schema de la tabla particionada.
    """
    conn.execute(
        text(
            f'ALTER TABLE "{schema}"."{PARTITIONED_TABLE}" '
            f"DETACH PARTITION {qualified_partition(period, schema)}"
        )
    )


def drop_partition(
    conn: Connection, period: date, schema: str = DEFAULT_SCHEMA, if_exists: bool = True
) -> None:
    """Elimina la particion de un corte y sus filas.

    Args:
        conn: conexion con permisos de DDL.
        period: fecha del corte.
        schema: schema de la tabla particionada.
        if_exists: si False, falla cuando la particion no existe.
    """
    exists_clause = "IF EXISTS " if if_exists else ""
    conn.execute(text(f"DROP TABLE {exists_clause}{qualified_partition(period, schema)}"))


def existing_partitions(conn: Connection, schema: str = DEFAULT_SCHEMA) -> list[str]:
    """Particiones enganchadas hoy a la tabla padre.

    Args:
        conn: conexion de lectura.
        schema: schema de la tabla particionada.

    Returns:
        list[str]: nombres sin calificar, ordenados (el nombre lleva la
        fecha, asi que el orden alfabetico es cronologico).
    """
    rows = conn.execute(
        text(
            """
            SELECT child.relname
            FROM pg_inherits
            JOIN pg_class parent ON parent.oid = pg_inherits.inhparent
            JOIN pg_class child ON child.oid = pg_inherits.inhrelid
            JOIN pg_namespace ns ON ns.oid = parent.relnamespace
            WHERE ns.nspname = :schema AND parent.relname = :parent
            ORDER BY child.relname
            """
        ),
        {"schema": schema, "parent": PARTITIONED_TABLE},
    ).scalars()
    return list(rows)
