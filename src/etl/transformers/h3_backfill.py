"""
H3-Cells Stage: backfill de celdas H3 en Silver DuckDB

Calcula `h3_cell_9` (blocking, res 9 ~ arista 174m) y `h3_cell_5`
(particionado, res 5 ~ arista 8.5km) desde latitud/longitud, directamente
en la tabla establecimientos via SQL UPDATE con la extension comunitaria
`h3` de DuckDB. Todo ocurre out-of-core dentro de DuckDB: no se materializa
ningun DataFrame en RAM, y el procesamiento es por periodo para acotar el
tamanho de cada transaccion.

Incremental por disenho: solo procesa filas con `h3_cell_9 IS NULL` y
coordenadas validas, asi que sirve tanto de backfill inicial (~100M filas)
como de computo para periodos nuevos en cada corrida del transform.

Pipeline position: ingest -> normalize-dates -> h3-cells -> clee-resolution
"""

import time
from datetime import date
from pathlib import Path

import duckdb

from src.etl.utils.clee_resolution_config import H3_BLOCKING_RES, H3_PARTITION_RES
from src.etl.utils.warehouse import connect_configured, load_h3_extension
from src.meta.tracker import MetadataTracker
from src.shared.enums import JobStatus, TaskStatus
from src.shared.logging import get_stage_logger

_LOG_FILE = Path("./data/etl_h3_backfill.log")

logger = get_stage_logger(__name__, _LOG_FILE)

# Filas elegibles: coordenadas presentes y dentro del dominio de h3
# (h3_latlng_to_cell lanza error fuera de [-90,90]/[-180,180]).
_COORDS_VALID_SQL = """
    latitud IS NOT NULL AND longitud IS NOT NULL
    AND latitud BETWEEN -90 AND 90
    AND longitud BETWEEN -180 AND 180
"""

# h3_cell_5 se deriva como PADRE de h3_cell_9 (no directo del punto):
# la jerarquia H3 no es perfectamente anidada y ~6.5% de los puntos cerca
# de bordes caen en un res-5 distinto al padre de su res-9. Para el
# particionado de Fase 4 necesitamos que todas las filas de una misma
# celda de blocking (res 9) compartan particion (res 5) -- eso solo lo
# garantiza cell_to_parent.
_UPDATE_SQL_TEMPLATE = f"""
UPDATE establecimientos
SET h3_cell_9 = CAST(h3_latlng_to_cell(latitud, longitud, {H3_BLOCKING_RES}) AS BIGINT),
    h3_cell_5 = CAST(h3_cell_to_parent(
        h3_latlng_to_cell(latitud, longitud, {H3_BLOCKING_RES}), {H3_PARTITION_RES}
    ) AS BIGINT)
WHERE period = ?
  AND {{state_clause}}
  AND {_COORDS_VALID_SQL}
  {{force_clause}}
"""


class H3CellBackfiller:
    """
    Calcula celdas H3 (res 9 y res 5) para establecimientos en Silver.

    Opera directamente sobre la tabla via SQL UPDATE (extension h3 de
    DuckDB), un periodo a la vez, con tracking en metadata (process_tasks).

    Modes:
        force=False: solo filas con h3_cell_9 IS NULL (incremental).
        force=True:  recalcula todas las filas con coordenadas validas
                     (usar solo si cambia la resolucion H3).
    """

    def __init__(
        self,
        tracker: MetadataTracker,
        silver_db_path: Path,
    ):
        """
        Initialize H3 backfiller.

        Args:
            tracker: MetadataTracker instance for job/task tracking
            silver_db_path: Path to Silver DuckDB warehouse
        """
        self.tracker = tracker
        self.silver_db_path = Path(silver_db_path)

    def _get_connection(self) -> duckdb.DuckDBPyConnection:
        """Conexion a Silver con la extension h3 cargada.

        Usa connect_configured(): DUCKDB_MEMORY_LIMIT / DUCKDB_THREADS de
        settings aplicados (el UPDATE sobre periodos de millones de filas
        no debe rebasar el presupuesto de RAM de la maquina).
        """
        return load_h3_extension(connect_configured(self.silver_db_path))

    def _get_pending_periods(self, force: bool) -> list[tuple[date, int]]:
        """
        Periodos con filas elegibles pendientes de celda H3.

        Args:
            force: Si True, cuenta todas las filas con coordenadas validas
                   (recomputo total); si False, solo las que tienen
                   h3_cell_9 IS NULL.

        Returns:
            Lista de (period, filas_pendientes), orden cronologico.
        """
        null_clause = "" if force else "AND h3_cell_9 IS NULL"
        with self._get_connection() as conn:
            rows = conn.execute(f"""
                SELECT period, COUNT(*) AS pending
                FROM establecimientos
                WHERE period IS NOT NULL
                  AND {_COORDS_VALID_SQL}
                  {null_clause}
                GROUP BY period
                HAVING COUNT(*) > 0
                ORDER BY period
            """).fetchall()
        return [(r[0], r[1]) for r in rows]

    def _get_pending_states(self, period: date, force: bool) -> list[tuple[str | None, int]]:
        """
        Estados (cve_ent) con filas pendientes dentro de un periodo.

        El batching por estado da dos cosas: progreso visible en consola
        (32 ticks por periodo en vez de un UPDATE opaco de millones de
        filas) y transacciones mas pequenhas (menos RAM pico por UPDATE).

        Returns:
            Lista de (cve_ent, filas_pendientes); cve_ent puede ser None.
        """
        null_clause = "" if force else "AND h3_cell_9 IS NULL"
        with self._get_connection() as conn:
            rows = conn.execute(f"""
                SELECT cve_ent, COUNT(*) AS pending
                FROM establecimientos
                WHERE period = ?
                  AND {_COORDS_VALID_SQL}
                  {null_clause}
                GROUP BY cve_ent
                HAVING COUNT(*) > 0
                ORDER BY cve_ent
            """, [period]).fetchall()
        return [(r[0], r[1]) for r in rows]

    def _backfill_period(
        self,
        period: date,
        force: bool,
        progress: dict,
    ) -> int:
        """
        Ejecuta el UPDATE de celdas H3 para un periodo, por lotes de estado.

        Args:
            period: Periodo a procesar.
            force: Si True, recalcula todas las filas con coords validas.
            progress: Estado mutable del avance global:
                      {"done": filas ya procesadas, "total": filas totales,
                       "t0": timestamp de inicio del run}.

        Returns:
            Numero de filas actualizadas en el periodo.
        """
        force_clause = "" if force else "AND h3_cell_9 IS NULL"
        states = self._get_pending_states(period, force)
        period_updated = 0

        with self._get_connection() as conn:
            for s_idx, (state, n_pending) in enumerate(states, start=1):
                if state is None:
                    state_clause = "cve_ent IS NULL"
                    params: list = [period]
                else:
                    state_clause = "cve_ent = ?"
                    params = [period, state]

                sql = _UPDATE_SQL_TEMPLATE.format(
                    state_clause=state_clause, force_clause=force_clause
                )

                t0 = time.perf_counter()
                result = conn.execute(sql, params)
                row = result.fetchone()
                updated = row[0] if row else 0
                elapsed = time.perf_counter() - t0

                period_updated += updated
                progress["done"] += updated

                rate = updated / elapsed if elapsed > 0 else 0
                pct = 100.0 * progress["done"] / progress["total"] if progress["total"] else 100.0
                run_elapsed = time.perf_counter() - progress["t0"]
                overall_rate = progress["done"] / run_elapsed if run_elapsed > 0 else 0
                remaining = progress["total"] - progress["done"]
                eta_min = (remaining / overall_rate / 60) if overall_rate > 0 else 0

                logger.info(
                    f"   {period} | edo {state or 'NULL'} ({s_idx}/{len(states)}): "
                    f"{updated:,} filas en {elapsed:.1f}s ({rate:,.0f} filas/s) | "
                    f"global {progress['done']:,}/{progress['total']:,} ({pct:.1f}%) "
                    f"ETA ~{eta_min:.0f} min"
                )

        return period_updated

    def run(self, force: bool = False) -> bool:
        """
        Run H3 cell backfill for all pending periods.

        Args:
            force: If True, recompute cells for ALL rows with valid coords.
                   If False, only rows where h3_cell_9 IS NULL (incremental).

        Returns:
            True if successful, False otherwise
        """
        pending = self._get_pending_periods(force)

        if not pending:
            logger.info("H3-cells: todas las filas elegibles ya tienen celda. Nada por hacer.")
            return True

        total_pending = sum(n for _, n in pending)
        logger.info(
            f"H3-cells: {len(pending)} periodos, {total_pending:,} filas pendientes "
            f"(res {H3_BLOCKING_RES} + res {H3_PARTITION_RES}, "
            f"modo {'FORCE' if force else 'INCREMENTAL'})"
        )
        logger.info(f"   Log detallado: {_LOG_FILE}")

        progress = {"done": 0, "total": total_pending, "t0": time.perf_counter()}

        run_id = self.tracker.create_run(
            job_name="denue_h3_backfill",
            job_type="TRANSFORM",
            trigger_type="MANUAL",
            config={"force": force, "res_blocking": H3_BLOCKING_RES, "res_partition": H3_PARTITION_RES},
        )

        total_updated = 0
        failed = False

        for idx, (period, n_pending) in enumerate(pending, start=1):
            logger.info(
                f"[{idx}/{len(pending)}] Periodo {period}: {n_pending:,} filas pendientes..."
            )
            task_id = self.tracker.create_process_task(
                run_id=run_id,
                task_name=f"h3_cells_{period.isoformat()}",
                source_path=str(self.silver_db_path),
                period=period.isoformat(),
                target_table="establecimientos",
            )
            self.tracker.start_process_task(task_id)

            try:
                updated = self._backfill_period(period, force, progress)
                total_updated += updated
                self.tracker.complete_process_task(
                    task_id=task_id,
                    status=TaskStatus.SUCCESS,
                    rows_loaded=updated,
                )
                logger.info(
                    f"[{idx}/{len(pending)}] Periodo {period} completado: "
                    f"{updated:,} filas actualizadas ✅"
                )
            except Exception as e:
                failed = True
                self.tracker.complete_process_task(
                    task_id=task_id,
                    status=TaskStatus.FAILED,
                    error_type="H3_BACKFILL",
                    error_message=str(e),
                )
                logger.error(f"   [{idx}/{len(pending)}] {period}: fallo H3 backfill: {e}")

        status = JobStatus.FAILED if failed else JobStatus.SUCCESS
        self.tracker.complete_run(run_id, status)

        logger.info(f"H3-cells terminado: {total_updated:,} filas actualizadas en total.")
        return not failed
