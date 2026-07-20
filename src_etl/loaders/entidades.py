"""
Dimension de entidad economica (N3): una fila por negocio resuelto.

`entidades_economicas` colapsa las observaciones de `establecimientos`
(una fila por negocio POR PERIODO) a una fila por identidad resuelta
(`clee_propagado_radio`), con los atributos del periodo mas reciente y el
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

import duckdb

from src_etl.core.config import settings
from src_etl.core.logging import get_stage_logger
from src_etl.utils.warehouse import connect_configured
from src_metadata.models import JobStatus, TaskStatus
from src_metadata.tracker import MetadataTracker

logger = get_stage_logger(__name__, Path("./data/etl_entidades.log"))

TABLE_NAME = "entidades_economicas"
"""Nombre de la tabla dimension en Silver (Gold la hereda en make load)."""

ENTIDADES_DDL = """
CREATE TABLE IF NOT EXISTS entidades_economicas (
    clee_propagado_radio VARCHAR PRIMARY KEY,
    es_sintetica BOOLEAN NOT NULL,
    nom_estab VARCHAR,
    raz_social VARCHAR,
    codigo_act VARCHAR,
    cve_ent VARCHAR,
    cve_mun VARCHAR,
    cod_postal VARCHAR,
    latitud DOUBLE,
    longitud DOUBLE,
    h3_cell_9 BIGINT,
    primer_periodo DATE NOT NULL,
    ultimo_periodo DATE NOT NULL,
    n_periodos INTEGER NOT NULL,
    fecha_alta_date DATE
)
"""
"""DDL de la dimension (N3, todo.md): PK = identidad resuelta; atributos
del periodo mas reciente; `es_sintetica` marca los NEW_* (negocios sin
match — nuevos o desaparecidos); `fecha_alta_date` es la MINIMA conocida
entre las observaciones (alta mas antigua reportada a INEGI)."""

BUILD_SQL = """
INSERT INTO entidades_economicas
WITH obs AS (
    SELECT
        clee_propagado_radio, id, period, nom_estab, raz_social,
        codigo_act, cve_ent, cve_mun, cod_postal, latitud, longitud,
        h3_cell_9, fecha_alta_date
    FROM establecimientos
    WHERE clee_propagado_radio IS NOT NULL AND clee_propagado_radio != ''
),
vida AS (
    SELECT
        clee_propagado_radio,
        MIN(period) AS primer_periodo,
        MAX(period) AS ultimo_periodo,
        COUNT(DISTINCT period) AS n_periodos,
        MIN(fecha_alta_date) AS fecha_alta_date
    FROM obs
    GROUP BY clee_propagado_radio
),
representante AS (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY clee_propagado_radio
        ORDER BY period DESC, id
    ) AS rn
    FROM obs
)
SELECT
    r.clee_propagado_radio,
    starts_with(r.clee_propagado_radio, 'NEW_') AS es_sintetica,
    r.nom_estab, r.raz_social, r.codigo_act, r.cve_ent, r.cve_mun,
    r.cod_postal, r.latitud, r.longitud, r.h3_cell_9,
    v.primer_periodo, v.ultimo_periodo, v.n_periodos, v.fecha_alta_date
FROM representante r
JOIN vida v USING (clee_propagado_radio)
WHERE r.rn = 1
"""
"""Agregacion de observaciones -> dimension. La fila representativa es la
del periodo mas reciente (rn=1, desempate por id); el rango de vida y la
fecha de alta vienen del agregado por identidad."""


def build_entidades_economicas(conn: duckdb.DuckDBPyConnection) -> int:
    """
    (Re)construye la dimension completa en la conexion dada.

    DROP + CREATE + INSERT en una transaccion: la tabla queda consistente
    o intacta. Ver docstring del modulo para por que el rebuild es total.

    Returns:
        Numero de entidades (filas) construidas.
    """
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(f"DROP TABLE IF EXISTS {TABLE_NAME}")
        conn.execute(ENTIDADES_DDL)
        conn.execute(BUILD_SQL)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return conn.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}").fetchone()[0]


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
        silver_db_path: Path | None = None,
    ):
        """
        Args:
            data_root: Raiz de datos (contiene silver/, metadata.duckdb).
            tracker: MetadataTracker (default: data/metadata.duckdb).
            silver_db_path: Silver DuckDB (default: settings).
        """
        self.data_root = Path(data_root)
        self.tracker = tracker or MetadataTracker(self.data_root / "metadata.duckdb")
        self.warehouse_path = Path(silver_db_path or settings.SILVER_DUCKDB_PATH)

    def run(self) -> bool:
        """
        Construye la dimension con tracking; True si no hubo errores.

        Precondicion: `clee_propagado_radio` poblada (resolucion CLEE
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
            source_path=str(self.warehouse_path),
            target_table=TABLE_NAME,
        )
        self.tracker.start_process_task(task_id)
        logger.info(f"📦 Construyendo dimension {TABLE_NAME}...")

        conn = connect_configured(self.warehouse_path)
        try:
            rows = build_entidades_economicas(conn)
            sinteticas = conn.execute(
                f"SELECT COUNT(*) FROM {TABLE_NAME} WHERE es_sintetica"
            ).fetchone()[0]

            self.tracker.complete_process_task(task_id, TaskStatus.SUCCESS, rows_loaded=rows)
            self.tracker.record_lineage(
                run_id=run_id,
                task_id=task_id,
                source_location=f"{self.warehouse_path}::establecimientos",
                target_location=f"{self.warehouse_path}::{TABLE_NAME}",
                source_type="TABLE",
                target_type="TABLE",
            )
            self.tracker.complete_run(run_id, JobStatus.SUCCESS)

            if rows == 0:
                logger.warning(
                    "⚠️ Dimension vacia: clee_propagado_radio sin poblar. "
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
        finally:
            conn.close()
