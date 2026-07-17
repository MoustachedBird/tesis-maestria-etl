"""
CLEE Resolution Stage: Entity Resolution (Silver DuckDB)

Resolves entity identities across temporal snapshots, matching
establishments over time and generating master identifiers (CLEEs).

Propagates CLEEs from the most recent period backward through time
(future -> past). Uses a sliding-window approach: each target period
is compared against one reference period at a time (instead of a
monolithic growing KB), keeping peak RAM to ~5-8 GB.

For periods that already have CLEE assigned by INEGI (2021+), resolution
is deterministic (clee_propagado_radio = clee). For older periods without CLEE
(2015-2020), a spatial tolerance radius (BallTree) pre-filter and a
conditional matching cascade resolve identities probabilistically.

Reads from and writes back to the Silver DuckDB warehouse directly,
populating the `clee_propagado_radio` column on the `establecimientos` table.
"""

import gc
import logging
from datetime import date
from pathlib import Path

import duckdb
import polars as pl

from src_etl.utils.clee_resolution_config import (
    SILVER_READ_QUERY,
    SPATIAL_RADIUS_M,
)
from src_etl.utils.entity_resolution import (
    assign_new_clees,
    build_single_period_kb,
    get_region_prefix,
    propagate_clees,
)
from src_etl.utils.matching import clean_contact_columns
from src_etl.utils.warehouse import connect_configured, get_distinct_periods
from src_metadata.models import JobStatus, TaskStatus
from src_metadata.tracker import MetadataTracker

logger = logging.getLogger(__name__)


class CLEEResolver:
    """
    Resolves entity identities across temporal snapshots.

    Uses a sliding-window approach: for each target period without CLEE,
    iterates over all already-resolved periods one at a time as reference,
    propagating clee_propagado_radio where spatial + fuzzy matching finds a hit.

    Peak memory: ~5-8 GB (1 reference period + 1 target period).

    Modes:
        force=True:  Reset all clee_propagado_radio, re-resolve from scratch.
        force=False: Only process records where clee_propagado_radio IS NULL
                     (incremental, preserves already-assigned values).
    """

    def __init__(
        self,
        silver_db_path: Path,
        tracker: MetadataTracker,
    ):
        """
        Initialize CLEE resolution stage.

        Args:
            silver_db_path: Path to Silver DuckDB warehouse
            tracker: MetadataTracker instance for job/task tracking
        """
        self.silver_db_path = Path(silver_db_path)
        self.tracker = tracker

        # Spatial tolerance from config (easy to calibrate)
        self.spatial_tol = SPATIAL_RADIUS_M

        # KB cache: cada periodo de referencia se carga de DuckDB (JOIN +
        # limpieza) O(T x R) veces en la ventana deslizante. La KB preparada
        # (10 columnas) se cachea en parquet tras la primera construccion.
        self.kb_cache_dir = self.silver_db_path.parent / "kb_cache"

    def _get_connection(self) -> duckdb.DuckDBPyConnection:
        """Get a DuckDB connection to Silver warehouse (settings limits applied)."""
        return connect_configured(self.silver_db_path)

    def _count_unresolved(self) -> int:
        """Count rows where clee_propagado_radio IS NULL."""
        with self._get_connection() as conn:
            return conn.execute("""
                SELECT COUNT(*) FROM establecimientos
                WHERE clee_propagado_radio IS NULL
            """).fetchone()[0]

    def _get_sorted_periods(self) -> list[date]:
        """
        Get all periods from Silver DuckDB sorted newest-first.

        Returns:
            List of date objects sorted chronologically descending.
            Example: [date(2024, 11, 1), date(2024, 5, 1), ...]
        """
        with self._get_connection() as conn:
            return get_distinct_periods(conn, descending=True)

    def _get_periods_with_clee_coverage(self) -> dict[date, bool]:
        """
        Classify each period as having full CLEE coverage or not.

        Returns:
            Dict mapping period -> True if 100% rows have CLEE, False otherwise.
        """
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT
                    period,
                    COUNT(*) AS total,
                    COUNT(CASE WHEN clee IS NOT NULL AND clee != '' THEN 1 END) AS with_clee
                FROM establecimientos
                WHERE period IS NOT NULL
                GROUP BY period
                ORDER BY period DESC
            """).fetchall()

        return {r[0]: (r[2] == r[1]) for r in rows}

    def _load_period(self, period: date) -> pl.DataFrame:
        """
        Load a single period's data from Silver DuckDB.

        Uses SILVER_READ_QUERY which JOINs with catalog tables to get
        `nombre_act` and casts ENUM columns to VARCHAR, filtered by period.

        Args:
            period: Date object representing the period (e.g. date(2024, 11, 1))

        Returns:
            Polars DataFrame with columns from COLS_NEEDED_FOR_MATCHING
        """
        query = SILVER_READ_QUERY + "\n    WHERE e.period = ?"

        with self._get_connection() as conn:
            result = conn.execute(query, [period])
            df = pl.from_arrow(result.fetch_arrow_table())

        if df.is_empty():
            return df

        # clean_contact_columns es el punto unico de sanitizacion
        # (rellena nulos de texto + deriva tel/email/www _clean)
        return clean_contact_columns(df)

    # ------------------------------------------------------------------
    # KB cache (parquet por periodo de referencia)
    # ------------------------------------------------------------------

    def _kb_cache_path(self, period: date) -> Path:
        """Path del parquet de KB cacheada para un periodo."""
        return self.kb_cache_dir / f"{period.isoformat()}.parquet"

    def _get_ref_kb(self, period: date) -> pl.DataFrame:
        """
        Obtiene la KB de un periodo de referencia, usando cache en disco.

        Primera vez: carga el periodo de DuckDB (JOIN + limpieza), construye
        la KB de 10 columnas y la persiste en parquet (escritura atomica via
        tmp + rename). Siguientes veces: lee el parquet directo, evitando
        re-materializar el periodo completo (~19 columnas) en RAM.
        """
        cache_path = self._kb_cache_path(period)
        if cache_path.exists():
            logger.info(f"   KB de {period} desde cache ({cache_path.name})")
            return pl.read_parquet(cache_path)

        df_ref = self._load_period(period)
        ref_kb = build_single_period_kb(df_ref)
        del df_ref
        gc.collect()

        self.kb_cache_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_suffix(".parquet.tmp")
        ref_kb.write_parquet(tmp_path)
        tmp_path.rename(cache_path)
        logger.info(f"   KB de {period} cacheada ({ref_kb.height:,} filas)")

        return ref_kb

    def _invalidate_kb_cache(self, period: date) -> None:
        """Borra la KB cacheada de un periodo (tras re-escribir sus clee_propagado_radio)."""
        cache_path = self._kb_cache_path(period)
        if cache_path.exists():
            cache_path.unlink()

    def _clear_kb_cache(self) -> None:
        """Borra todo el cache de KBs (modo --force)."""
        if not self.kb_cache_dir.exists():
            return
        removed = 0
        for f in self.kb_cache_dir.glob("*.parquet"):
            f.unlink()
            removed += 1
        for f in self.kb_cache_dir.glob("*.parquet.tmp"):
            f.unlink()
        if removed:
            logger.info(f"   KB cache limpiado ({removed} archivos)")

    def _get_total_rows(self) -> int:
        """Get total row count in establecimientos."""
        with self._get_connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM establecimientos").fetchone()[0]

    def _write_period_results(self, mapping: pl.DataFrame) -> int:
        """
        Write clee_propagado_radio assignments for a single period to Silver DuckDB.

        Uses batched UPDATE statements via temp table for efficiency.

        Args:
            mapping: DataFrame with columns [id, clee_propagado_radio]

        Returns:
            Number of rows updated
        """
        if mapping.is_empty():
            return 0

        total = mapping.height
        batch_size = 50_000
        updated = 0

        with self._get_connection() as conn:
            for offset in range(0, total, batch_size):
                batch = mapping.slice(offset, batch_size)

                conn.execute(
                    "CREATE OR REPLACE TEMP TABLE _clee_updates (id VARCHAR, clee_propagado_radio VARCHAR)"
                )
                conn.execute("INSERT INTO _clee_updates SELECT * FROM batch")
                result = conn.execute("""
                    UPDATE establecimientos e
                    SET clee_propagado_radio = u.clee_propagado_radio
                    FROM _clee_updates u
                    WHERE e.id = u.id
                """)
                batch_updated = result.fetchone()[0] if result.description else batch.height
                updated += batch_updated
                conn.execute("DROP TABLE IF EXISTS _clee_updates")

        return updated

    # ------------------------------------------------------------------
    # Phase 1: Deterministic resolution (periods with CLEE from INEGI)
    # ------------------------------------------------------------------

    def _resolve_deterministic_periods(
        self,
        periods: list[date],
        clee_coverage: dict[date, bool],
        force: bool,
    ) -> list[date]:
        """
        Resolve periods that have 100% CLEE coverage deterministically.

        For these periods, clee_propagado_radio = clee (direct copy via SQL UPDATE).
        No BallTree, no fuzzy matching needed.

        Args:
            periods: All periods sorted newest-first.
            clee_coverage: Dict mapping period -> has_full_clee.
            force: If True, overwrite existing clee_propagado_radio values.

        Returns:
            List of periods that were resolved (available as references).
        """
        deterministic_periods = [p for p in periods if clee_coverage.get(p, False)]

        if not deterministic_periods:
            return []

        logger.info(
            f"[FASE 1] Resolucion deterministica: {len(deterministic_periods)} periodos con CLEE"
        )

        with self._get_connection() as conn:
            if force:
                # Reset all clee_propagado_radio first
                conn.execute("UPDATE establecimientos SET clee_propagado_radio = NULL")

            # Copy clee -> clee_propagado_radio for all rows with CLEE
            conn.execute("""
                UPDATE establecimientos
                SET clee_propagado_radio = clee
                WHERE clee IS NOT NULL AND clee != ''
            """)

            # Count seeded rows
            count = conn.execute("""
                SELECT COUNT(*) FROM establecimientos
                WHERE clee_propagado_radio IS NOT NULL
            """).fetchone()[0]

        logger.info(
            f"   clee_propagado_radio = clee para {count:,} filas ({len(deterministic_periods)} periodos)"
        )

        return deterministic_periods

    # ------------------------------------------------------------------
    # Phase 2: Probabilistic resolution (sliding window)
    # ------------------------------------------------------------------

    def _resolve_probabilistic_periods(
        self,
        periods: list[date],
        clee_coverage: dict[date, bool],
        resolved_periods: list[date],
        region_prefix: str,
    ) -> int:
        """
        Resolve periods without CLEE using sliding-window approach.

        For each target period P (no CLEE, newest-first):
          1. Load P from DuckDB (~3M rows, all clee_propagado_radio NULL)
          2. For each already-resolved period R (newest-first):
             - Load R, build KB (scalar, ~6M rows max)
             - Run propagate_clees(P, KB_R) to assign matches
             - Free R from RAM
          3. Assign NEW_prefix-uuid to remaining unresolved rows
          4. Write results to DuckDB
          5. Mark P as resolved (available as reference for next periods)

        Args:
            periods: All periods sorted newest-first.
            clee_coverage: Dict mapping period -> has_full_clee.
            resolved_periods: Periods already resolved (from Phase 1).
            region_prefix: Prefix for generating temporal CLEEs.

        Returns:
            Total number of rows resolved in this phase.
        """
        probabilistic_periods = [p for p in periods if not clee_coverage.get(p, False)]

        if not probabilistic_periods:
            logger.info("[FASE 2] No hay periodos sin CLEE. Nada por resolver.")
            return 0

        logger.info(
            f"[FASE 2] Resolucion probabilistica: "
            f"{len(probabilistic_periods)} periodos sin CLEE "
            f"(ventana deslizante, radio={self.spatial_tol}m)"
        )

        total_resolved = 0

        for p_idx, target_period in enumerate(probabilistic_periods):
            logger.info(
                f"🔗 [{p_idx + 1}/{len(probabilistic_periods)}] "
                f"Resolviendo periodo {target_period}..."
            )

            # Load target period (all rows, clee_propagado_radio will be NULL)
            df_target = self._load_period(target_period)
            if df_target.is_empty():
                logger.info(f"   {target_period}: vacio, saltando.")
                resolved_periods.append(target_period)
                continue

            target_total = df_target.height

            # Ensure clee_propagado_radio column exists and is NULL for this period
            if "clee_propagado_radio" not in df_target.columns:
                df_target = df_target.with_columns(pl.lit(None).cast(pl.Utf8).alias("clee_propagado_radio"))
            else:
                df_target = df_target.with_columns(
                    pl.when(pl.col("clee_propagado_radio") == "")
                    .then(pl.lit(None).cast(pl.Utf8))
                    .otherwise(pl.col("clee_propagado_radio"))
                    .alias("clee_propagado_radio")
                )

            # Sliding window: compare against each resolved period
            for ref_period in resolved_periods:
                # Count how many still need resolution
                still_missing = df_target.filter(pl.col("clee_propagado_radio").is_null()).height
                if still_missing == 0:
                    break

                logger.info(f"   vs {ref_period} ({still_missing:,} pendientes)...")

                # Load reference KB (from parquet cache after first build)
                ref_kb = self._get_ref_kb(ref_period)

                # Propagate CLEEs from reference to target
                df_target = propagate_clees(df_target, ref_kb, self.spatial_tol)

                del ref_kb
                gc.collect()

            # Assign NEW CLEEs to remaining unresolved rows
            still_missing = df_target.filter(pl.col("clee_propagado_radio").is_null()).height
            if still_missing > 0:
                logger.info(f"   Asignando {still_missing:,} NEW_{region_prefix}-* CLEEs")
                df_target = assign_new_clees(df_target, region_prefix)

            # Write results to DuckDB immediately (no accumulation in RAM)
            mapping = df_target.select(["id", "clee_propagado_radio"])
            rows_written = self._write_period_results(mapping)
            total_resolved += rows_written

            # Sus clee_propagado_radio cambiaron: invalidar KB cacheada (si existia)
            self._invalidate_kb_cache(target_period)

            logger.info(
                f"   ✅ {target_period} resuelto: {rows_written:,} filas escritas "
                f"({target_total - still_missing:,} propagados, "
                f"{still_missing:,} nuevos)"
            )

            # Mark this period as resolved (available as reference)
            resolved_periods.append(target_period)

            # Free memory
            del df_target, mapping
            gc.collect()

        return total_resolved

    # ------------------------------------------------------------------
    # Main orchestrator
    # ------------------------------------------------------------------

    def process_national(self, force: bool = False) -> dict:
        """
        Process all periods using sliding-window entity resolution.

        Phase 1 (deterministic): Periods with CLEE get clee_propagado_radio = clee
        Phase 2 (probabilistic): Periods without CLEE are resolved via
        sliding-window comparison against all already-resolved periods.

        Args:
            force: If True, reset all clee_propagado_radio and re-resolve from scratch.

        Returns:
            Dict with resolution statistics.
        """
        periods = self._get_sorted_periods()
        if not periods:
            return {"total_resolved": 0, "periods_processed": 0}

        logger.info(f"   Periodos detectados ({len(periods)}): {periods}")

        # Classify periods by CLEE coverage
        clee_coverage = self._get_periods_with_clee_coverage()

        # Get region prefix
        with self._get_connection() as conn:
            state_rows = conn.execute(
                "SELECT DISTINCT cve_ent FROM establecimientos ORDER BY cve_ent"
            ).fetchall()
        state_list = [r[0] for r in state_rows] if state_rows else ["NATIONAL"]
        prefix = get_region_prefix(state_list)
        logger.info(f"   Prefijo de region (namespace): {prefix}")

        # --- Phase 1: Deterministic resolution ---
        resolved_periods = self._resolve_deterministic_periods(periods, clee_coverage, force)

        # --- Phase 2: Probabilistic resolution (sliding window) ---
        total_prob = self._resolve_probabilistic_periods(
            periods, clee_coverage, resolved_periods, prefix
        )

        return {
            "total_resolved": total_prob,
            "periods_processed": len(periods),
            "deterministic_periods": len([p for p in periods if clee_coverage.get(p, False)]),
            "probabilistic_periods": len([p for p in periods if not clee_coverage.get(p, False)]),
            "region_prefix": prefix,
        }

    def run(self, force: bool = False) -> bool:
        """
        Run CLEE resolution pipeline.

        Reads from Silver DuckDB period by period, resolves entity
        identities, and writes `clee_propagado_radio` back to the same table.

        Args:
            force: If True, reset all clee_propagado_radio values and re-run from scratch.
                   If False, only process rows where clee_propagado_radio IS NULL (incremental).

        Returns:
            True if successful, False otherwise
        """
        if force:
            logger.warning(
                "MODO FORCE ACTIVADO: Se resetearan y re-resolveran todos los clee_propagado_radio."
            )
            self._clear_kb_cache()

        # Create job run
        run_id = self.tracker.create_run(
            job_name="denue_clee_resolver",
            job_type="CLEE_RESOLUTION",
            trigger_type="MANUAL",
        )

        # Create a single CLEE resolution task for the national run
        task_id = self.tracker.create_clee_resolution_task(
            run_id=run_id,
            task_name="clee_resolution_national",
            region_group="NATIONAL",
            state_list=["ALL"],
        )

        self.tracker.start_clee_resolution_task(task_id)

        try:
            # Check how many rows need resolution before starting
            total_rows = self._get_total_rows()
            unresolved_before = self._count_unresolved() if not force else total_rows

            logger.info(
                f"Iniciando resolucion CLEE: {total_rows:,} filas totales, "
                f"{unresolved_before:,} sin resolver, radio={self.spatial_tol}m"
            )

            if unresolved_before == 0 and not force:
                logger.info("Todas las entidades ya resueltas. Nada por procesar.")
                self.tracker.complete_clee_resolution_task(
                    task_id=task_id,
                    status=TaskStatus.SKIPPED,
                    error_message="No unresolved rows",
                )
                self.tracker.complete_run(run_id, JobStatus.SUCCESS)
                return True

            # Run the two-phase resolution
            stats = self.process_national(force=force)

            # Count remaining unresolved
            remaining = self._count_unresolved()

            # Complete task
            self.tracker.complete_clee_resolution_task(
                task_id=task_id,
                status=TaskStatus.SUCCESS,
                entities_input=total_rows,
                entities_matched=total_rows - remaining,
                master_clees_created=total_rows - remaining,
                periods_processed=stats["periods_processed"],
                output_path=str(self.silver_db_path),
            )

            logger.info(
                f"Resolucion CLEE terminada. "
                f"{stats['total_resolved']:,} filas actualizadas (prob.), "
                f"{remaining:,} pendientes."
            )
            logger.info(
                f"   Resumen: {stats['deterministic_periods']} periodos deterministicos, "
                f"{stats['probabilistic_periods']} periodos probabilisticos"
            )

            # Cleanup
            gc.collect()

            self.tracker.complete_run(run_id, JobStatus.SUCCESS)
            return True

        except Exception as e:
            logger.error(f"   Error en resolucion CLEE: {e}")
            self.tracker.complete_clee_resolution_task(
                task_id=task_id,
                status=TaskStatus.FAILED,
                error_message=str(e),
            )
            self.tracker.complete_run(run_id, JobStatus.FAILED)
            return False
