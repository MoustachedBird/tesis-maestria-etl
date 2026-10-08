"""
Gestion automatica de los indices de la tabla de hechos.

Los indices sirven para CONSULTAR, no para CARGAR: cada indice ART de DuckDB
se mantiene fila por fila, lo que hace las escrituras masivas ordenes de
magnitud mas lentas (medido en este repo con `ix_estab_h3_9`). Pero borrarlos
a mano antes de cada carga y recrearlos despues deja la base a merced de que
alguien lo recuerde.

Por eso aqui no hay comandos: las etapas que escriben en masa (ingest,
resolucion CLEE) envuelven su trabajo en `suspended_indexes()`, que los
suelta al entrar y los recrea en un `finally`. Si un proceso muere a media
carga los indices quedan ausentes — la base sigue siendo CORRECTA, solo
consulta mas lento — y la siguiente corrida los restituye sola;
`missing_indexes()` permite reportarlo.

La definicion vive en el modelo ORM (`Base.metadata`), no en SQL suelto: es
la misma fuente que usa la migracion, asi que no pueden divergir.
"""

import logging
from contextlib import contextmanager

from sqlalchemy.schema import CreateIndex

logger = logging.getLogger(__name__)

MANAGED_TABLES = ("establecimientos",)
"""Tablas cuyos indices se suspenden durante las escrituras masivas.

Solo la tabla de hechos: los catalogos son chicos y sus indices no estorban.
"""


def _duckdb_dialect():
    """Dialecto para compilar el DDL de indices como lo entiende DuckDB."""
    from duckdb_engine import Dialect

    return Dialect()


def expected_indexes() -> dict[str, str]:
    """
    Indices declarados en el modelo para las tablas gestionadas.

    Returns:
        Dict nombre -> sentencia CREATE INDEX.
    """
    from src.shared.db.base import get_table
    from src.shared.db.models import Establecimiento  # noqa: F401  (puebla el metadata)

    dialect = _duckdb_dialect()
    out: dict[str, str] = {}
    for table_name in MANAGED_TABLES:
        table = get_table(table_name)
        for index in table.indexes:
            out[index.name] = str(CreateIndex(index).compile(dialect=dialect)).strip()
    return out


def existing_indexes(conn) -> set[str]:
    """Nombres de los indices de usuario presentes hoy en las tablas gestionadas."""
    marks = ", ".join(f"'{t}'" for t in MANAGED_TABLES)
    rows = conn.execute(
        f"SELECT index_name FROM duckdb_indexes() WHERE table_name IN ({marks})"
    ).fetchall()
    return {r[0] for r in rows}


def missing_indexes(conn) -> list[str]:
    """Indices declarados que faltan en la base (para reportes de estado)."""
    return sorted(set(expected_indexes()) - existing_indexes(conn))


def drop_indexes(conn) -> list[str]:
    """
    Suelta los indices gestionados presentes; devuelve los nombres soltados.

    No toca PK ni FKs: esos son constraints, no indices de usuario, y
    DuckDB los mantiene aparte.
    """
    presentes = existing_indexes(conn) & set(expected_indexes())
    for name in sorted(presentes):
        conn.execute(f'DROP INDEX IF EXISTS "{name}"')
    if presentes:
        logger.info(f"   Indices suspendidos durante la escritura: {len(presentes)}")
    return sorted(presentes)


def ensure_indexes(conn) -> list[str]:
    """
    Crea los indices declarados que falten (idempotente).

    Returns:
        Nombres de los indices creados en esta llamada.
    """
    faltantes = missing_indexes(conn)
    esperados = expected_indexes()
    for name in faltantes:
        logger.info(f"   Creando indice {name} (sobre millones de filas toma minutos)...")
        conn.execute(esperados[name])
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
        conn: Conexion DuckDB abierta a Silver.
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
