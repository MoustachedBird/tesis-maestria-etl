"""Rellena `silver.establecimientos.h3_cell_7` en los cortes ya cargados.

`b0008` agregó la columna, y el ingest la calcula desde entonces
(`transformers/enrich.py`). Pero los 20 cortes que ya estaban en Silver se
cargaron sin ella, así que la traen en NULL — y el método de resolución
`celda_h3_7` no puede correr sobre un criterio vacío.

Esto existe porque la alternativa era derivarla al vuelo, y eso cuesta una
llamada a h3 por fila en cada corrida y en cada método. Materializarla cuesta
8 bytes por fila y se paga una vez, que es el mismo trato que ya se hizo con
`h3_cell_9` y `h3_cell_5`.

**Se deriva como PADRE de `h3_cell_9`, nunca del punto.** Es la misma regla
que `enrich.add_h3_cells` y no es un detalle: calcularla desde las
coordenadas daría casi siempre lo mismo, pero en los bordes el padre de la
celda res 9 y la celda res 7 del mismo punto pueden diferir. Lo que la
resolución necesita es que la jerarquía sea **consistente** --que toda res 9
esté dentro de su padre--, no que cada celda sea la más cercana al punto. Si
el relleno usara el punto, las filas viejas y las nuevas quedarían con
criterios incompatibles en los bordes, y el blocking perdería matches sin que
nada lo delatara.

**No es un `UPDATE` sobre los 82.9M.** Va corte por corte, cada uno su propia
transacción, y el trabajo pesado lo hace el servidor con un join contra un
mapa pequeño:

    1. las celdas res 9 DISTINTAS del corte que aún no tienen padre
    2. sus padres, calculados en Python (~9M celdas/s, así que es gratis)
    3. el mapa a una tabla temporal con `COPY`
    4. un `UPDATE ... FROM` del corte contra el mapa

El paso 1 es la clave del costo: de los ~4M de observaciones de un corte
salen muchas menos celdas distintas, porque los establecimientos se apiñan.
Mapear celdas y no filas es lo que vuelve el trabajo de Python irrelevante.

**Reanudable sin registro aparte:** un corte está hecho cuando no le quedan
filas con `h3_cell_9` no nulo y `h3_cell_7` nulo. Un corte que falló a la
mitad hizo rollback, así que no hay estados intermedios que interpretar.
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import h3.api.basic_int as h3_int
import polars as pl
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from src.etl.utils.troceado import SET_WORK_MEM, cortes_ordenados, latido
from src.meta.tracker import MetadataTracker
from src.shared.db.base import DEFAULT_SCHEMA
from src.shared.db.copy import copy_dataframe
from src.shared.enums import JobStatus, TaskStatus, TriggerType
from src.shared.logging import get_stage_logger

logger = get_stage_logger(__name__, Path("./data/etl_h3_7.log"))

JOB_NAME = "denue_relleno_h3_cell_7"
"""Nombre de la corrida en `meta.job_runs`."""

JOB_TYPE = "BACKFILL"
"""Etapa propia: no es un ingest ni una transformación, es reparar una columna.

Mezclarla con `TRANSFORM` haría que `/ops/health/pipeline` --que reporta la
última corrida POR TIPO-- tapara un relleno fallido con un ingest exitoso.
"""

RES_DESTINO = 7
"""Resolución H3 del padre. Es `CELL_MATCH_RES` de la resolución CLEE.

A res 7 la celda mide ~1.5 km de lado a lado, que es el criterio espacial del
método `celda_h3_7`: lo que cae en la misma celda es candidato, sin círculo
de tolerancia.
"""

_SQL_PENDIENTES = """
SELECT count(*)
FROM {schema}.establecimientos
WHERE period = DATE '{corte}'
  AND h3_cell_9 IS NOT NULL
  AND h3_cell_7 IS NULL
"""
"""Cuántas filas del corte esperan su padre.

Es el avance y el criterio de reanudación a la vez: en 0, el corte está
hecho. Las filas sin `h3_cell_9` no cuentan --son establecimientos que INEGI
publicó sin geolocalizar-- y quedarse en NULL es su valor correcto.
"""

_SQL_CELDAS = """
SELECT DISTINCT h3_cell_9
FROM {schema}.establecimientos
WHERE period = DATE '{corte}'
  AND h3_cell_9 IS NOT NULL
  AND h3_cell_7 IS NULL
"""
"""Las celdas res 9 distintas por mapear.

`DISTINCT` y no las filas: el padre es función de la celda, así que mapear
filas repetiría el mismo cálculo miles de veces y traería 4M de enteros a
Python en vez de unos cientos de miles.
"""

_SQL_MAPA = """
CREATE TEMP TABLE mapa_h3 (
    h3_cell_9 bigint PRIMARY KEY,
    h3_cell_7 bigint NOT NULL
) ON COMMIT DROP
"""
"""El mapa de la vuelta.

`TEMP ... ON COMMIT DROP` sí es lo correcto aquí, al contrario que en el
troceo: el mapa se consume dentro de la transacción del corte y no tiene que
sobrevivirle. La PK no es decorativa -- es el índice con que el `UPDATE`
busca cada celda.
"""

_SQL_APLICAR = """
UPDATE {schema}.establecimientos e
SET h3_cell_7 = m.h3_cell_7
FROM mapa_h3 m
WHERE e.period = DATE '{corte}'
  AND e.h3_cell_9 = m.h3_cell_9
  AND e.h3_cell_7 IS NULL
"""
"""El join contra el mapa, acotado a UNA partición.

El `e.period` no es redundante con la poda de particiones: es lo que la hace
posible. Sin él, PostgreSQL tendría que considerar las 20 particiones para un
mapa que solo describe una.

El `h3_cell_7 IS NULL` lo vuelve idempotente: correrlo dos veces sobre el
mismo corte no reescribe filas ya hechas, que en una tabla de 82.9M es la
diferencia entre nada y un rewrite completo por MVCC.
"""


def _padres(celdas: list[int]) -> pl.DataFrame:
    """Mapea cada celda res 9 a su padre res 7.

    Usa la API de h3 basada en enteros, así que no hay dos conversiones a
    cadena por celda --que es lo que haría el camino `int_to_str` ->
    `cell_to_parent` -> `str_to_int`-- y el resultado es idéntico al que
    `enrich.add_h3_cells` escribe en el ingest.

    Args:
        celdas: índices H3 de resolución 9 como entero de 64 bits.

    Returns:
        pl.DataFrame: columnas `h3_cell_9` y `h3_cell_7`, ambas Int64.
    """
    return pl.DataFrame(
        {
            "h3_cell_9": celdas,
            "h3_cell_7": [h3_int.cell_to_parent(c, RES_DESTINO) for c in celdas],
        },
        schema={"h3_cell_9": pl.Int64, "h3_cell_7": pl.Int64},
    )


def _rellenar_corte(engine: Engine, corte: date, schema: str, etiqueta: str) -> int:
    """Rellena un corte en una sola transacción.

    Args:
        engine: engine con permisos de escritura.
        corte: el corte a rellenar.
        schema: schema de los hechos.
        etiqueta: prefijo del log, p. ej. `corte 7/20`.

    Returns:
        int: filas actualizadas; 0 si no había nada pendiente.
    """
    with engine.connect() as conn:
        pendientes = conn.execute(
            text(_SQL_PENDIENTES.format(schema=schema, corte=corte.isoformat()))
        ).scalar_one()

    if pendientes == 0:
        logger.info(f"   ✨ {etiqueta}: {corte} ya tiene h3_cell_7, se salta")
        return 0

    logger.info(f"   ▶ {etiqueta}: {corte} con {pendientes:,} filas pendientes")
    inicio = time.monotonic()

    with engine.begin() as conn:
        conn.execute(text(SET_WORK_MEM))

        with latido(conn, f"{etiqueta} celdas distintas", logger):
            celdas = (
                conn.execute(text(_SQL_CELDAS.format(schema=schema, corte=corte.isoformat())))
                .scalars()
                .all()
            )
        logger.info(
            f"      {etiqueta}: {len(celdas):,} celdas res 9 distintas "
            f"({pendientes / max(len(celdas), 1):.1f} filas por celda)"
        )

        mapa = _padres(list(celdas))
        distintos = mapa["h3_cell_7"].n_unique()
        logger.info(
            f"      {etiqueta}: mapeadas a {distintos:,} celdas res {RES_DESTINO} "
            f"(factor de agrupacion {len(celdas) / max(distintos, 1):.1f}x)"
        )

        conn.execute(text(_SQL_MAPA))
        copy_dataframe(conn, mapa, "mapa_h3", schema=None)
        # Sin ANALYZE el planificador cree que la temporal tiene mil filas y
        # elige un nested loop contra una particion de 4M.
        conn.execute(text("ANALYZE mapa_h3"))

        with latido(conn, f"{etiqueta} UPDATE", logger):
            escritas = conn.execute(
                text(_SQL_APLICAR.format(schema=schema, corte=corte.isoformat()))
            ).rowcount

    minutos = (time.monotonic() - inicio) / 60
    logger.info(f"   ✔ {etiqueta}: {corte} -> {escritas:,} filas en {minutos:.1f} min")
    return escritas


def _sin_padre(conn: Connection, schema: str) -> int:
    """Filas con `h3_cell_9` y sin `h3_cell_7` en TODO Silver.

    Es el invariante de salida: en 0, la columna está completa y
    `clee_celda_h3_7` puede correr sobre ella.
    """
    return conn.execute(
        text(f"""
        SELECT count(*) FROM {schema}.establecimientos
        WHERE h3_cell_9 IS NOT NULL AND h3_cell_7 IS NULL
        """)
    ).scalar_one()


def rellenar_h3_cell_7(
    engine: Engine,
    tracker: MetadataTracker,
    schema: str = DEFAULT_SCHEMA,
) -> int:
    """Rellena `h3_cell_7` en todos los cortes que lo tengan en NULL.

    **No hay `force`, y es a propósito.** El relleno es idempotente y solo
    toca filas con `h3_cell_7 IS NULL`: no hay nada que "rehacer", porque el
    padre de una celda no cambia. Un `--force` que reescribiera los 82.9M
    produciría exactamente los mismos valores a cambio de un rewrite completo
    por MVCC — todo el costo y ningún efecto.

    Queda en `meta`: una corrida en `meta.job_runs` y una tarea en
    `meta.process_tasks` **por corte**, con filas pendientes, filas escritas y
    duración. Así "¿qué cortes faltan?" y "¿cuánto tardó 2019-11?" se
    contestan con SQL en vez de leyendo `data/etl_h3_7.log`.

    Al terminar valida que no queden filas con `h3_cell_9` y sin
    `h3_cell_7`; si quedan, la corrida falla. Es la condición que
    `clee_celda_h3_7` necesita, y descubrirla al arrancar la resolución
    --horas después-- costaría mucho más.

    Args:
        engine: engine con permisos de escritura; esta función controla sus
            transacciones, una por corte.
        tracker: registro de `meta` donde queda la corrida.
        schema: schema de los hechos.

    Returns:
        int: filas actualizadas en total.

    Raises:
        RuntimeError: si al terminar quedan filas sin padre.
    """
    run_id = tracker.create_run(
        job_name=JOB_NAME,
        job_type=JOB_TYPE,
        trigger_type=TriggerType.MANUAL.value,
        config={"columna": "h3_cell_7", "resolucion": RES_DESTINO, "derivada_de": "h3_cell_9"},
    )

    try:
        with engine.connect() as conn:
            cortes = cortes_ordenados(conn, schema)
            faltan_antes = _sin_padre(conn, schema)

        if not cortes:
            logger.warning(f"   no hay cortes en {schema}.establecimientos, nada que rellenar")
            tracker.complete_run(run_id, JobStatus.SUCCESS)
            return 0

        logger.info(
            f"   h3_cell_7 (padre res {RES_DESTINO} de h3_cell_9): "
            f"{len(cortes)} cortes, {faltan_antes:,} filas sin padre"
        )
        if faltan_antes == 0:
            logger.info("   ✨ la columna ya esta completa; nada que hacer")
            tracker.complete_run(run_id, JobStatus.SUCCESS)
            return 0

        total = 0
        inicio = time.monotonic()
        for n, corte in enumerate(cortes, start=1):
            etiqueta = f"corte {n}/{len(cortes)}"
            task_id = tracker.create_process_task(
                run_id=run_id,
                task_name=f"h3_cell_7_{corte:%Y_%m}",
                source_path=f"{schema}.establecimientos.h3_cell_9",
                period=corte.isoformat(),
                target_table="establecimientos",
            )
            tracker.start_process_task(task_id)
            try:
                escritas = _rellenar_corte(engine, corte, schema, etiqueta)
            except Exception as e:
                tracker.complete_process_task(task_id, TaskStatus.FAILED, error_message=str(e))
                raise
            total += escritas
            tracker.complete_process_task(
                task_id,
                TaskStatus.SUCCESS if escritas else TaskStatus.SKIPPED,
                rows_loaded=escritas,
            )

        with engine.connect() as conn:
            faltan = _sin_padre(conn, schema)
        if faltan:
            raise RuntimeError(
                f"quedan {faltan:,} filas con h3_cell_9 y sin h3_cell_7 tras rellenar "
                f"los {len(cortes)} cortes; `clee-celda-h3-7` no puede correr asi"
            )

        logger.info(
            f"   ✅ h3_cell_7 completa: {total:,} filas en "
            f"{(time.monotonic() - inicio) / 60:.1f} min"
        )
    except Exception as e:
        tracker.complete_run(run_id, JobStatus.FAILED, error_summary=str(e))
        raise

    tracker.complete_run(run_id, JobStatus.SUCCESS)
    return total
