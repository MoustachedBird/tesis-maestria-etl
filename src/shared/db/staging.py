"""Carga de un corte en tabla aparte, enganchada a Silver solo si cuadra.

El diagnostico que origina esto (arquitectura.md §1.2) fue un OOM a media
carga que dejo filas parciales y dejo el warehouse irrecuperable: la unidad
de recuperacion era el warehouse entero. Aqui la carga ocurre en una tabla
suelta que Silver no ve, y la tabla se engancha como particion --operacion
de catalogo, instantanea-- solo despues de validarla. Si algo falla a media
carga se tira la tabla de staging y Silver nunca se entero.

**Divergencia deliberada del plan: la tabla de staging nace LOGGED.**
arquitectura.md Fase 3.4 dice "tabla UNLOGGED". El problema es que una
particion UNLOGGED hace a Silver no duradera (se vacia ante un crash del
servidor), y evitarlo obliga a `SET LOGGED` antes de enganchar, que reescribe
la tabla entera y vuelve a escribir todo el WAL que el UNLOGGED se habia
ahorrado: los datos se escriben dos veces en vez de una. El ahorro real de
WAL en cargas masivas no es `UNLOGGED`, es `wal_level=minimal` en el
servidor, que es configuracion y no codigo. Queda el parametro `unlogged`
para medirlo cuando haya servidor; el default es el que no compromete la
durabilidad de Silver.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Connection

from src.shared.db.base import DEFAULT_SCHEMA
from src.shared.db.partitions import PARTITIONED_TABLE, partition_name
from src.shared.logging import get_logger

logger = get_logger(__name__)

#: Sufijo de la tabla donde se carga antes de enganchar. Se renombra al
#: nombre definitivo de la particion recien despues del ATTACH.
STAGING_SUFFIX = "_staging"


class StagingValidationError(RuntimeError):
    """La tabla de staging no cumple lo que se exige para engancharla.

    Se levanta antes de tocar la tabla padre, asi que Silver queda intacta.
    """


@dataclass
class StagingTarget:
    """Destino de carga mientras el corte todavia no es parte de Silver.

    Attributes:
        schema: schema donde vive la tabla de staging.
        table: nombre de la tabla de staging (sin calificar).
        period: corte que se esta cargando.
        parent: tabla particionada a la que se enganchara.
    """

    schema: str
    table: str
    period: date
    parent: str = PARTITIONED_TABLE

    @property
    def qualified(self) -> str:
        """Nombre calificado y entrecomillado, para SQL crudo."""
        return f'"{self.schema}"."{self.table}"'


def _create_staging_table(
    conn: Connection, target: StagingTarget, unlogged: bool, parent: str
) -> None:
    """Crea la tabla de staging con la estructura de la tabla padre.

    `LIKE` sin `INCLUDING INDEXES`, a proposito: cargar 4M filas contra
    indices vivos es el patron que esta migracion existe para eliminar. El
    ATTACH los construye despues, en bloque, porque PostgreSQL le crea a la
    particion un indice por cada indice particionado del padre (la PK
    incluida).

    Las FKs tampoco se copian --`LIKE` nunca las copia-- y no hace falta:
    las de la tabla particionada se aplican solas a lo que se engancha.

    Args:
        conn: conexion con permisos de DDL.
        target: destino de carga.
        unlogged: si la tabla nace UNLOGGED (ver el docstring del modulo).
        parent: nombre de la tabla particionada padre, sin calificar.
    """
    persistence = "UNLOGGED " if unlogged else ""
    conn.execute(text(f"DROP TABLE IF EXISTS {target.qualified}"))
    conn.execute(
        text(
            f"CREATE {persistence}TABLE {target.qualified} "
            f'(LIKE "{target.schema}"."{parent}" INCLUDING DEFAULTS INCLUDING GENERATED)'
        )
    )


def _validate(conn: Connection, target: StagingTarget, min_rows: int) -> int:
    """Comprueba que lo cargado puede ser la particion del corte.

    Dos cosas, ambas baratas comparadas con descubrirlas despues del ATTACH:
    que haya filas y que todas tengan el periodo esperado. La segunda la
    rechazaria el propio ATTACH, pero con un error del servidor que no dice
    cuantas filas estaban mal ni de que periodo eran.

    Args:
        conn: conexion de lectura sobre la tabla de staging.
        target: destino de carga.
        min_rows: minimo de filas aceptable.

    Returns:
        int: filas en la tabla de staging.

    Raises:
        StagingValidationError: si esta vacia, corta, o tiene otro periodo.
    """
    rows = conn.execute(text(f"SELECT COUNT(*) FROM {target.qualified}")).scalar_one()
    if rows < min_rows:
        raise StagingValidationError(
            f"{target.qualified} tiene {rows} filas, menos que el minimo {min_rows}"
        )

    intrusas = conn.execute(
        text(
            f"SELECT COUNT(*) FROM {target.qualified} "
            f"WHERE period IS DISTINCT FROM DATE '{target.period.isoformat()}'"
        )
    ).scalar_one()
    if intrusas:
        raise StagingValidationError(
            f"{target.qualified}: {intrusas} de {rows} filas no son del corte "
            f"{target.period.isoformat()}"
        )
    return rows


def _attach(conn: Connection, target: StagingTarget, parent: str) -> str:
    """Engancha la tabla de staging como particion y le da su nombre final.

    Antes del ATTACH agrega el CHECK que replica el limite de la particion.
    Eso no ahorra trabajo total --alguien tiene que leer la tabla-- pero
    mueve ese escaneo a un momento en que solo se tiene bloqueada la tabla
    de staging. Sin el CHECK, el escaneo ocurre dentro del ATTACH, que
    sostiene un ACCESS EXCLUSIVE sobre la tabla padre: Silver entera
    bloqueada durante el escaneo en vez de durante una operacion de
    catalogo.

    Args:
        conn: conexion con permisos de DDL.
        target: destino de carga, ya validado.
        parent: nombre de la tabla particionada padre, sin calificar.

    Returns:
        str: nombre definitivo de la particion.
    """
    literal = f"DATE '{target.period.isoformat()}'"
    conn.execute(
        text(
            f"ALTER TABLE {target.qualified} "
            f"ADD CONSTRAINT {target.table}_period_check CHECK (period = {literal})"
        )
    )
    conn.execute(
        text(
            f'ALTER TABLE "{target.schema}"."{parent}" '
            f"ATTACH PARTITION {target.qualified} FOR VALUES IN ({literal})"
        )
    )

    final = partition_name(target.period, parent)
    conn.execute(text(f'ALTER TABLE {target.qualified} RENAME TO "{final}"'))
    # El CHECK ya es redundante con el limite de la particion y le cuesta una
    # evaluacion a cada INSERT futuro sobre ese corte.
    conn.execute(
        text(f'ALTER TABLE "{target.schema}"."{final}" DROP CONSTRAINT {target.table}_period_check')
    )
    return final


@contextmanager
def staging_partition(
    conn: Connection,
    period: date,
    schema: str = DEFAULT_SCHEMA,
    parent: str = PARTITIONED_TABLE,
    unlogged: bool = False,
    min_rows: int = 1,
) -> Iterator[StagingTarget]:
    """Carga un corte fuera de Silver y lo engancha solo si queda bien.

    Uso tipico desde el ingest::

        with engine.begin() as conn:
            with staging_partition(conn, period) as target:
                copy_dataframe(conn, df, target.table, target.schema)

    Al salir sin excepcion: valida y engancha. Al salir con excepcion:
    tira la tabla de staging y re-lanza. En ambos casos Silver solo cambia
    de estado una vez, y de forma atomica.

    Args:
        conn: conexion SQLAlchemy; el llamador controla la transaccion.
        period: corte que se carga.
        schema: schema de la tabla particionada.
        parent: nombre de la tabla particionada padre.
        unlogged: tabla de staging UNLOGGED (ver el docstring del modulo).
        min_rows: minimo de filas para aceptar el corte.

    Yields:
        StagingTarget: a donde escribir.

    Raises:
        StagingValidationError: si lo cargado no cuadra con el corte.
    """
    # El nombre lleva el padre: sin eso, cargar el mismo corte en
    # `clee_radio` y en `clee_h3` apuntaria a la misma tabla de staging.
    target = StagingTarget(
        schema=schema,
        table=f"{partition_name(period, parent)}{STAGING_SUFFIX}",
        period=period,
        parent=parent,
    )
    _create_staging_table(conn, target, unlogged=unlogged, parent=parent)
    logger.info(
        "staging abierto", table=target.qualified, period=period.isoformat(), unlogged=unlogged
    )

    try:
        yield target
    except Exception:
        # El DROP va en su propio intento: si la transaccion ya esta
        # abortada no se puede limpiar aqui, y perder el error original por
        # un fallo de limpieza seria peor que dejar la tabla huerfana (que
        # el proximo DROP IF EXISTS se lleva de todas formas).
        try:
            conn.execute(text(f"DROP TABLE IF EXISTS {target.qualified}"))
        except Exception:
            logger.warning(
                "no se pudo limpiar la tabla de staging; la retira el proximo intento",
                table=target.qualified,
            )
        logger.error("carga abortada, Silver sin cambios", period=period.isoformat())
        raise

    rows = _validate(conn, target, min_rows=min_rows)
    final = _attach(conn, target, parent=parent)
    logger.info(
        "particion enganchada",
        partition=f"{schema}.{final}",
        rows=rows,
        period=period.isoformat(),
    )
