"""La muestra: una tirada estratificada y reproducible sobre gold.

Es el SQL de `verificacion_cierre.md` §4.3 con los parámetros en `.env`, y
escribe `verificacion.muestras` + `verificacion.muestra_negocios` en UNA
transacción: o queda la tirada completa o no queda nada.

Dos decisiones que no son de implementación:

- **La hipótesis se congela aquí.** `cerrado_gold`, `meses_sin_aparecer_gold`
  y `huecos_gold` se copian de gold en el momento de muestrear y no se
  vuelven a tocar: son lo que se le va a presentar al agente y contra lo que
  se compara su veredicto. Si gold se reconstruye con otro umbral, esta
  muestra sigue midiendo la regla con la que nació; la nueva se mide con
  otra muestra.
- **Reproducible por semilla, y por eso sin paralelismo.** `setseed()` fija
  la secuencia de `random()` de la sesión; con workers en paralelo cada uno
  tiene la suya y la tirada deja de ser la misma. El `ORDER BY random()`
  sobre ~9.58M filas es un sort de un minuto: es una operación por muestra,
  no una consulta.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from src.meta.tracker import MetadataTracker
from src.shared.enums import JobStatus, TaskStatus, TriggerType
from src.shared.logging import get_stage_logger

logger = get_stage_logger(__name__, Path("./data/agente.log"))

JOB_TYPE = "VERIFICACION"
"""Etapa propia en `meta.job_runs.job_type`: no es TRANSFORM ni GOLD."""

JOB_NAME_MUESTREAR = "agente_supervivencia_muestrear"
"""Nombre de la corrida de muestreo en `meta.job_runs`."""

ESTRATOS: tuple[tuple[str, str], ...] = (
    ("cerrado|reocupado", "cerrado AND local_reocupado"),
    ("cerrado|no_reocupado", "cerrado"),
    ("activo|intermitente", "huecos > 0"),
    ("activo|continuo", "TRUE"),
)
"""Los cuatro estratos, EN ORDEN: el primero que cumple gana (es un `CASE`).

Son los de `verificacion_cierre.md` §4.2. El orden importa: un cerrado con
local reocupado cumple también la condición del segundo, y un activo
intermitente la del cuarto. Se declaran una vez y los usan el muestreo (para
asignar) y las métricas (para reponderar), así que no pueden divergir.
"""

FILTRO_POBLACION = "NOT cluster_sospechoso AND nombre IS NOT NULL AND cve_geo_final IS NOT NULL"
"""A qué población se extrapola: lo que se puede buscar.

Sin nombre no hay qué buscar; sin municipio tampoco; y un cluster
sospechoso no es UN negocio. Queda grabado en `muestras.filtro` porque define
el universo del que la muestra habla.
"""


def sql_estrato() -> str:
    """El `CASE` que asigna estrato a una fila de gold (sin alias: va sobre la tabla directa)."""
    ramas = "\n".join(f"      WHEN {condicion} THEN '{nombre}'" for nombre, condicion in ESTRATOS)
    return f"CASE\n{ramas}\n    END"


def tamanos_poblacion(conn: Connection) -> dict[str, int]:
    """Cuántos negocios de la población caen en cada estrato.

    Es lo que decide si las cuotas alcanzan y el peso con que se repondera
    cada estrato en las métricas (la muestra los iguala; la población no).
    """
    filas = conn.execute(
        text(f"""
        SELECT {sql_estrato()} AS estrato, count(*) AS n
        FROM gold.negocios_clee_oficial
        WHERE {FILTRO_POBLACION}
        GROUP BY 1
        """)
    ).all()
    conteos = {nombre: 0 for nombre, _ in ESTRATOS}
    conteos.update({estrato: int(n) for estrato, n in filas})
    return conteos


def crear_muestra(
    engine: Engine,
    tracker: MetadataTracker,
    nombre: str,
    semilla: float,
    cuota: int,
) -> dict[str, int]:
    """Saca una muestra nueva de gold y la registra en `meta`.

    Args:
        engine: engine con permisos de escritura en `verificacion`.
        tracker: registro de `meta` donde queda la corrida.
        nombre: cómo se va a citar la muestra; único.
        semilla: argumento de `setseed()`, en [-1, 1].
        cuota: negocios por estrato.

    Returns:
        dict[str, int]: negocios que salieron por estrato.

    Raises:
        ValueError: si el nombre ya existe, gold está vacío o los parámetros
            no tienen sentido. Se lanza antes de escribir nada.
    """
    if not -1.0 <= semilla <= 1.0:
        raise ValueError(f"la semilla de setseed() va en [-1, 1]; llegó {semilla}")
    if cuota <= 0:
        raise ValueError(f"la cuota por estrato tiene que ser positiva; llegó {cuota}")

    run_id = tracker.create_run(
        job_name=JOB_NAME_MUESTREAR,
        job_type=JOB_TYPE,
        trigger_type=TriggerType.MANUAL.value,
        config={
            "muestra": nombre,
            "semilla": semilla,
            "cuota_por_estrato": cuota,
            "estratos": [e for e, _ in ESTRATOS],
            "filtro": FILTRO_POBLACION,
        },
    )
    task_id = tracker.create_process_task(
        run_id=run_id,
        task_name=nombre,
        source_path="gold.negocios_clee_oficial",
        target_table="verificacion.muestra_negocios",
    )
    tracker.start_process_task(task_id)

    try:
        conteos = _tirar(engine, nombre, semilla, cuota)
    except Exception as e:
        tracker.complete_process_task(task_id, TaskStatus.FAILED, error_message=str(e))
        tracker.complete_run(run_id, JobStatus.FAILED, error_summary=str(e))
        raise

    total = sum(conteos.values())
    tracker.complete_process_task(task_id, TaskStatus.SUCCESS, rows_loaded=total)
    tracker.complete_run(run_id, JobStatus.SUCCESS)
    return conteos


def _tirar(engine: Engine, nombre: str, semilla: float, cuota: int) -> dict[str, int]:
    """La transacción de la tirada. No llamar directo: usar `crear_muestra`."""
    muestra_id = str(uuid.uuid4())
    with engine.begin() as conn:
        if conn.execute(
            text("SELECT 1 FROM verificacion.muestras WHERE nombre = :n"), {"n": nombre}
        ).scalar():
            raise ValueError(
                f"ya existe una muestra llamada {nombre!r}: otra tirada es otra fila con otro nombre"
            )
        reglas = conn.execute(
            text("SELECT version_reglas, ventana_cortes FROM gold.negocios_clee_oficial LIMIT 1")
        ).first()
        if reglas is None:
            raise ValueError(
                "gold.negocios_clee_oficial esta vacia: corre `transform --only gold` antes"
            )

        poblacion = tamanos_poblacion(conn)
        logger.info(f"   poblacion por estrato: {poblacion}")

        # Sin paralelismo, para que setseed() gobierne TODA la secuencia de
        # random(): con workers cada uno tendria la suya y la tirada no se
        # podria repetir.
        conn.execute(text("SET LOCAL max_parallel_workers_per_gather = 0"))
        conn.execute(text("SELECT setseed(:s)"), {"s": semilla})

        conn.execute(
            text("""
            INSERT INTO verificacion.muestras
                (muestra_id, nombre, semilla, tamano_pedido, n_negocios, estratos, filtro,
                 version_reglas, ventana_cortes, cierre_meses_ausente)
            VALUES (:id, :nombre, :semilla, :pedido, 0, CAST(:estratos AS jsonb), :filtro,
                    :version, :ventana, :meses)
            """),
            {
                "id": muestra_id,
                "nombre": nombre,
                "semilla": semilla,
                "pedido": cuota * len(ESTRATOS),
                "estratos": json.dumps({e: cuota for e, _ in ESTRATOS}),
                "filtro": FILTRO_POBLACION,
                "version": reglas.version_reglas,
                "ventana": list(reglas.ventana_cortes),
                "meses": _umbral_meses(),
            },
        )

        logger.info(f"   ▶ muestreando {cuota} negocios por estrato (semilla {semilla})...")
        conn.execute(
            text(f"""
            WITH poblacion AS (
                SELECT clee, cerrado, meses_sin_aparecer, huecos,
                       {sql_estrato()} AS estrato
                FROM gold.negocios_clee_oficial
                WHERE {FILTRO_POBLACION}
            ),
            numerada AS (
                SELECT p.*, row_number() OVER (PARTITION BY estrato ORDER BY random()) AS rn
                FROM poblacion p
            )
            INSERT INTO verificacion.muestra_negocios
                (muestra_id, clee, orden, estrato, cerrado_gold, meses_sin_aparecer_gold, huecos_gold)
            SELECT :id, n.clee,
                   row_number() OVER (ORDER BY n.estrato, n.rn),
                   n.estrato, n.cerrado, n.meses_sin_aparecer, n.huecos
            FROM numerada n
            WHERE n.rn <= :cuota
            """),
            {"id": muestra_id, "cuota": cuota},
        )
        conteos = dict(
            conn.execute(
                text("""
                SELECT estrato, count(*) FROM verificacion.muestra_negocios
                WHERE muestra_id = :id GROUP BY estrato ORDER BY estrato
                """),
                {"id": muestra_id},
            ).all()
        )
        conn.execute(
            text("UPDATE verificacion.muestras SET n_negocios = :n WHERE muestra_id = :id"),
            {"n": sum(conteos.values()), "id": muestra_id},
        )

    for estrato, _ in ESTRATOS:
        n = conteos.get(estrato, 0)
        aviso = "" if n >= cuota else f"  ⚠️ la poblacion solo tiene {poblacion.get(estrato, 0)}"
        logger.info(f"   {estrato:<22} {n:>5}{aviso}")
    logger.info(f"   ✅ muestra {nombre!r}: {sum(conteos.values())} negocios ({muestra_id})")
    return {estrato: int(conteos.get(estrato, 0)) for estrato, _ in ESTRATOS}


def _umbral_meses() -> int:
    """El `CIERRE_MESES_AUSENTE` vigente, que es con el que gold calculó `cerrado`."""
    from src.shared.config import get_settings

    return get_settings().CIERRE_MESES_AUSENTE
