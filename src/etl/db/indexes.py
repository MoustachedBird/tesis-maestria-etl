"""
Gestion automatica de los indices de la tabla de hechos.

Los indices sirven para CONSULTAR, no para CARGAR: mantenerlos fila por fila
hace las escrituras masivas ordenes de magnitud mas lentas (medido en este
repo con `ix_estab_h3_9`). Pero borrarlos a mano antes de cada carga y
recrearlos despues deja la base a merced de que alguien lo recuerde.

Por eso aqui no hay comandos: las etapas que escriben en masa (ingest,
resolucion CLEE) envuelven su trabajo en `suspended_indexes()`, que los
suelta al entrar y los recrea en un `finally`. Si un proceso muere a media
carga los indices quedan ausentes --la base sigue siendo CORRECTA, solo
consulta mas lento-- y la siguiente corrida los restituye sola;
`missing_indexes()` permite reportarlo.

La definicion vive en el modelo ORM (`Base.metadata`), no en SQL suelto: es
la misma fuente que usa la migracion, asi que no pueden divergir.

**Sobre la tabla particionada.** `establecimientos` esta particionada por
`period`, y eso cambia dos cosas:

- Un indice creado sobre el padre es un *indice particionado*: se propaga
  solo a cada particion, y soltarlo suelta los hijos con el. Eso mantiene
  `drop_indexes`/`ensure_indexes` tan simples como antes.
- `CREATE INDEX CONCURRENTLY` **no funciona sobre el padre**. Para construir
  sin bloquear hay que crear por particion y engancharlos al indice del
  padre, que es lo que hace `ensure_indexes(concurrently=True)`. El default
  es el camino bloqueante, que es el correcto dentro del ETL: ahi nadie mas
  esta leyendo y es bastante mas rapido.
"""

import logging
from contextlib import contextmanager

from sqlalchemy import text

logger = logging.getLogger(__name__)

MANAGED_TABLES = ("establecimientos",)
"""Tablas cuyos indices se suspenden durante las escrituras masivas.

Solo la tabla de hechos: los catalogos son chicos y sus indices no estorban.
"""


#: Largo maximo de un identificador en PostgreSQL. Pasarse no es un error:
#: el servidor TRUNCA en silencio, que es peor --dos particiones distintas
#: pueden terminar pidiendo el mismo nombre de indice y la segunda choca.
MAX_IDENTIFIER_LEN = 63


def _child_index_name(parent_index: str, partition: str) -> str:
    """Nombre del indice de una particion, acotado a lo que Postgres acepta.

    `ix_silver_establecimientos_clee_propagado_radio` (47) +
    `establecimientos_2024_11` (24) se pasa de 63, asi que el sufijo es solo
    la parte `YYYY_MM` de la particion y, si aun asi no cabe, lo que se
    recorta es el nombre del indice --nunca el sufijo, que es lo unico que
    distingue a una particion de otra.

    Args:
        parent_index: nombre del indice sobre la tabla padre.
        partition: nombre de la particion (p. ej. `establecimientos_2024_11`).

    Returns:
        str: nombre unico por particion, de a lo mas 63 caracteres.
    """
    from src.shared.db.partitions import PARTITIONED_TABLE

    suffix = partition.removeprefix(f"{PARTITIONED_TABLE}_")
    room = MAX_IDENTIFIER_LEN - len(suffix) - 1
    return f"{parent_index[:room]}_{suffix}"


def _index_columns(index) -> list[str]:
    """Nombres de columna de un `Index` del modelo, en orden.

    Args:
        index: objeto `Index` de SQLAlchemy.

    Returns:
        list[str]: nombres de columna.
    """
    return [c.name for c in index.columns]


def expected_indexes() -> dict[str, str]:
    """
    Indices declarados en el modelo para las tablas gestionadas.

    El DDL se arma a partir de las columnas del `Index` en vez de compilarlo
    con el dialecto: hace falta poder reemitir la misma definicion contra una
    particion concreta (ver `ensure_indexes(concurrently=True)`), y para eso
    una cadena ya compilada contra el padre no sirve.

    Returns:
        Dict nombre -> sentencia CREATE INDEX sobre la tabla padre.
    """
    from src.shared.db.base import DEFAULT_SCHEMA, get_table
    from src.shared.db.models import Establecimiento  # noqa: F401  (puebla el metadata)

    out: dict[str, str] = {}
    for table_name in MANAGED_TABLES:
        table = get_table(table_name)
        schema = table.schema or DEFAULT_SCHEMA
        for index in table.indexes:
            cols = ", ".join(f'"{c}"' for c in _index_columns(index))
            out[index.name] = (
                f'CREATE INDEX IF NOT EXISTS "{index.name}" '
                f'ON "{schema}"."{table_name}" ({cols})'
            )
    return out


def existing_indexes(conn) -> set[str]:
    """Nombres de los indices de usuario presentes hoy en las tablas gestionadas.

    Lee `pg_indexes`, que para una tabla particionada reporta el indice del
    padre bajo el nombre del padre --que es justo el nivel al que se gestiona
    aqui; los indices hijos se manejan solos.

    Args:
        conn: conexion SQLAlchemy sobre PostgreSQL.

    Returns:
        set[str]: nombres de indice presentes.
    """
    from src.shared.db.base import DEFAULT_SCHEMA, get_table

    schema = get_table(MANAGED_TABLES[0]).schema or DEFAULT_SCHEMA
    rows = conn.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = :schema AND tablename = ANY(:tables)"
        ),
        {"schema": schema, "tables": list(MANAGED_TABLES)},
    ).scalars()
    return set(rows)


def missing_indexes(conn) -> list[str]:
    """Indices declarados que faltan en la base (para reportes de estado).

    Args:
        conn: conexion SQLAlchemy sobre PostgreSQL.

    Returns:
        list[str]: nombres faltantes, ordenados.
    """
    return sorted(set(expected_indexes()) - existing_indexes(conn))


def drop_indexes(conn) -> list[str]:
    """
    Suelta los indices gestionados presentes; devuelve los nombres soltados.

    No toca PK ni FKs: esos son constraints, no indices de usuario, y
    soltarlos cambiaria el esquema en vez de solo su rendimiento. Soltar el
    indice del padre se lleva los de todas las particiones.

    Args:
        conn: conexion SQLAlchemy sobre PostgreSQL.

    Returns:
        list[str]: nombres soltados, ordenados.
    """
    from src.shared.db.base import DEFAULT_SCHEMA, get_table

    schema = get_table(MANAGED_TABLES[0]).schema or DEFAULT_SCHEMA
    presentes = existing_indexes(conn) & set(expected_indexes())
    for name in sorted(presentes):
        conn.execute(text(f'DROP INDEX IF EXISTS "{schema}"."{name}"'))
    if presentes:
        logger.info(f"   Indices suspendidos durante la escritura: {len(presentes)}")
    return sorted(presentes)


def _create_concurrently(conn, name: str) -> None:
    """Construye un indice particionado sin bloquear la tabla padre.

    El baile que exige PostgreSQL: el indice del padre se crea con `ON ONLY`
    (queda invalido y vacio, sin tocar las particiones), cada particion
    construye el suyo con CONCURRENTLY, y al engancharlos todos el del padre
    pasa a valido solo.

    Args:
        conn: conexion SQLAlchemy en autocommit (CONCURRENTLY no admite
            transaccion).
        name: nombre del indice del padre.
    """
    from src.shared.db.base import DEFAULT_SCHEMA, get_table
    from src.shared.db.partitions import existing_partitions

    table = get_table(MANAGED_TABLES[0])
    schema = table.schema or DEFAULT_SCHEMA
    index = next(i for i in table.indexes if i.name == name)
    cols = ", ".join(f'"{c}"' for c in _index_columns(index))

    # ON ONLY: el padre queda con un indice vacio e invalido hasta que todas
    # las particiones enganchen el suyo.
    conn.execute(
        text(
            f'CREATE INDEX IF NOT EXISTS "{name}" '
            f'ON ONLY "{schema}"."{table.name}" ({cols})'
        )
    )

    for partition in existing_partitions(conn, schema):
        child = _child_index_name(name, partition)
        conn.execute(
            text(
                f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{child}" '
                f'ON "{schema}"."{partition}" ({cols})'
            )
        )
        conn.execute(
            text(f'ALTER INDEX "{schema}"."{name}" ATTACH PARTITION "{schema}"."{child}"')
        )


def ensure_indexes(conn, concurrently: bool = False) -> list[str]:
    """
    Crea los indices declarados que falten (idempotente).

    Args:
        conn: conexion SQLAlchemy sobre PostgreSQL. Con `concurrently=True`
            tiene que estar en autocommit: `CREATE INDEX CONCURRENTLY` no
            corre dentro de una transaccion.
        concurrently: construir sin bloquear la tabla, particion por
            particion. Mas lento; solo hace falta si alguien mas esta
            consultando Silver mientras tanto.

    Returns:
        list[str]: nombres de los indices creados en esta llamada.
    """
    faltantes = missing_indexes(conn)
    esperados = expected_indexes()
    for name in faltantes:
        logger.info(f"   Creando indice {name} (sobre millones de filas toma minutos)...")
        if concurrently:
            _create_concurrently(conn, name)
        else:
            conn.execute(text(esperados[name]))
    if faltantes:
        logger.info(f"   Indices restituidos: {len(faltantes)}")
    return faltantes


@contextmanager
def suspended_indexes(conn, enabled: bool = True):
    """
    Suspende los indices mientras dura una escritura masiva y los restituye.

    Uso previsto: envolver el ingest y la resolucion CLEE. La restitucion va
    en `finally`, asi que tambien ocurre si la etapa falla con excepcion; si
    el proceso muere de golpe, la siguiente corrida los recrea.

    Args:
        conn: Conexion SQLAlchemy sobre PostgreSQL.
        enabled: False para dejar los indices intactos (util en pruebas o
            cargas pequenas donde el drop/create cuesta mas que mantenerlos).
    """
    if not enabled:
        yield
        return

    drop_indexes(conn)
    try:
        yield
    finally:
        ensure_indexes(conn)
