"""
Dimension de entidad economica (N3): una fila por negocio resuelto.

`entidades_economicas` colapsa las observaciones de `establecimientos`
(una fila por negocio POR PERIODO) a una fila por identidad resuelta
(`silver.clee_radio.clee_resuelto`), con los atributos del periodo mas reciente y el
rango de vida observado (`primer_periodo`/`ultimo_periodo`). Es la tabla
contra la que se calculan supervivencia y mortalidad en la tesis, y el
punto natural de cruce con `sesnsp_incidencia_municipal` (por
`cve_ent`/`cve_mun` + periodo).

Decisiones de diseno:
- **Se construye en Silver** (todo.md N3 dice "tabla en Gold"; construirla
  en Silver logra lo mismo porque `make load` copia el archivo completo,
  y mantiene el load como copia pura sin logica).
- **Rebuild completo por corrida** (DROP + CREATE + INSERT): los ids
  sinteticos NEW_* NO son estables entre corridas de resolucion con
  `--force`, asi que la dimension debe reconstruirse tras cada
  re-resolucion — nunca se actualiza incrementalmente.
- **Sin FKs hacia catalogos a proposito**: es un artefacto DERIVADO de la
  tabla de hechos (que ya garantiza esos dominios via sus propias FKs);
  anadir FKs solo encareceria el INSERT masivo (~15-20M filas) sin
  aportar integridad nueva. PK si: garantiza unicidad por identidad.
- La fila representativa por entidad es la del periodo MAS RECIENTE
  (desempate determinista por `id` para corridas reproducibles).
"""

from pathlib import Path

from sqlalchemy import Engine, delete, func, select, text
from sqlalchemy.engine import Connection

from src.meta.tracker import MetadataTracker
from src.shared.db.base import get_table
from src.shared.enums import JobStatus, TaskStatus
from src.shared.logging import get_stage_logger

logger = get_stage_logger(__name__, Path("./data/etl_entidades.log"))

TABLE_NAME = "entidades_economicas"
"""Nombre de la tabla dimension en Silver (Gold la hereda en make load)."""

IDENTITY_TABLE = "clee_radio"
"""Metodo cuya identidad define la dimension.

El baseline de la tesis. H3 es pista de comparacion: se mide contra esta,
no sustituye a la unidad de analisis (ver `docs/separacion_ingest_clee.md`).
"""

BUILD_SQL = f"""
INSERT INTO silver.entidades_economicas
WITH obs AS (
    SELECT
        i.clee_resuelto, e.id, e.period, e.nom_estab, e.raz_social,
        e.codigo_act, e.cve_ent, e.cve_mun, e.cod_postal, e.latitud, e.longitud,
        e.h3_cell_9, e.fecha_alta_date
    FROM silver.establecimientos e
    JOIN silver.{IDENTITY_TABLE} i ON i.id = e.id AND i.period = e.period
    WHERE i.clee_resuelto IS NOT NULL AND i.clee_resuelto != ''
),
vida AS (
    SELECT
        clee_resuelto,
        MIN(period) AS primer_periodo,
        MAX(period) AS ultimo_periodo,
        COUNT(DISTINCT period) AS n_periodos,
        MIN(fecha_alta_date) AS fecha_alta_date
    FROM obs
    GROUP BY clee_resuelto
),
representante AS (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY clee_resuelto
        ORDER BY period DESC, id
    ) AS rn
    FROM obs
)
SELECT
    r.clee_resuelto,
    starts_with(r.clee_resuelto, 'NEW_') AS es_sintetica,
    r.nom_estab, r.raz_social, r.codigo_act, r.cve_ent, r.cve_mun,
    r.cod_postal, r.latitud, r.longitud, r.h3_cell_9,
    v.primer_periodo, v.ultimo_periodo, v.n_periodos, v.fecha_alta_date
FROM representante r
JOIN vida v USING (clee_resuelto)
WHERE r.rn = 1
"""
"""Agregacion de observaciones -> dimension. La fila representativa es la
del periodo mas reciente (rn=1, desempate por id); el rango de vida y la
fecha de alta vienen del agregado por identidad.

El JOIN con la tabla del metodo es INNER y no LEFT: una observacion sin
identidad resuelta no puede ser parte de la dimension de negocios. El JOIN
incluye `period` para que sea partition-wise."""


def build_entidades_economicas(conn: Connection) -> int:
    """
    (Re)construye la dimension completa sobre la conexion dada.

    DELETE + INSERT, no DROP + CREATE: la tabla y sus indices los define la
    migracion, asi que dropearla la recrearia sin ellos. El llamador abre la
    transaccion, de modo que la tabla queda consistente o intacta.

    El rebuild es total y no incremental porque los ids `NEW_*` no son
    estables entre re-resoluciones: una identidad sintetica de hoy puede ser
    otra manana, asi que actualizar en sitio dejaria filas huerfanas.

    Args:
        conn: conexion con transaccion abierta.

    Returns:
        int: identidades construidas.
    """
    tabla = get_table(TABLE_NAME)
    conn.execute(delete(tabla))
    conn.execute(text(BUILD_SQL))
    return conn.execute(select(func.count()).select_from(tabla)).scalar_one()


class EntidadesDimensionBuilder:
    """
    Runner con tracking de metadata para construir la dimension.

    Job `entidades_build` en metadata (una process_task por corrida),
    mismo contrato de observabilidad que los demas stages.

    Example:
        >>> builder = EntidadesDimensionBuilder(data_root=Path("data"))
        >>> builder.run()
    """

    JOB_NAME = "entidades_build"
    """Nombre del job en metadata (job_runs.job_name)."""

    def __init__(
        self,
        data_root: Path,
        tracker: MetadataTracker | None = None,
        engine: Engine | None = None,
    ):
        """
        Args:
            data_root: Raiz de datos.
            tracker: MetadataTracker; por default, uno nuevo.
            engine: conexion a la base; por default la del proyecto.
        """
        self.data_root = Path(data_root)
        self.tracker = tracker or MetadataTracker()
        self._engine = engine

    @property
    def engine(self) -> Engine:
        """Engine de la base, resuelto la primera vez que se usa."""
        if self._engine is None:
            from src.shared.db.session import get_engine

            self._engine = get_engine()
        return self._engine

    def run(self) -> bool:
        """
        Construye la dimension con tracking; True si no hubo errores.

        Precondicion: `silver.clee_radio` poblada (resolucion CLEE
        terminada). Si esta vacia, la dimension queda en 0 filas y se
        reporta como advertencia (no es error: permite correr el stage
        en bases nuevas).
        """
        run_id = self.tracker.create_run(
            job_name=self.JOB_NAME,
            job_type="TRANSFORM",
            trigger_type="MANUAL",
        )
        task_id = self.tracker.create_process_task(
            run_id=run_id,
            task_name=TABLE_NAME,
            source_path="silver.establecimientos",
            target_table=TABLE_NAME,
        )
        self.tracker.start_process_task(task_id)
        logger.info(f"📦 Construyendo dimension {TABLE_NAME}...")
        try:
            # Una transaccion para el DELETE y el INSERT: la dimension queda
            # construida o intacta, nunca a medias.
            with self.engine.begin() as conn:
                rows = build_entidades_economicas(conn)
                tabla = get_table(TABLE_NAME)
                sinteticas = conn.execute(
                    select(func.count()).select_from(tabla).where(tabla.c.es_sintetica)
                ).scalar_one()

            self.tracker.complete_process_task(task_id, TaskStatus.SUCCESS, rows_loaded=rows)
            self.tracker.record_lineage(
                run_id=run_id,
                task_id=task_id,
                source_location="silver.establecimientos",
                target_location=f"silver.{TABLE_NAME}",
                source_type="TABLE",
                target_type="TABLE",
            )
            self.tracker.complete_run(run_id, JobStatus.SUCCESS)

            if rows == 0:
                logger.warning(
                    "⚠️ Dimension vacia: silver.clee_radio sin poblar. "
                    "Corre primero la resolucion CLEE."
                )
            else:
                logger.info(
                    f"✅ {TABLE_NAME}: {rows:,} entidades "
                    f"({sinteticas:,} sinteticas NEW_*, {rows - sinteticas:,} con CLEE)"
                )
            return True

        except Exception as e:
            logger.error(f"❌ Error construyendo {TABLE_NAME}: {e}")
            self.tracker.complete_process_task(task_id, TaskStatus.FAILED, error_message=str(e))
            self.tracker.complete_run(run_id, JobStatus.FAILED, error_summary=str(e))
            return False
