"""
CLEE Resolution Stage: Entity Resolution (Silver DuckDB)

Resolves entity identities across temporal snapshots, matching
establishments over time and generating master identifiers (CLEEs).

Propagates CLEEs from the most recent period backward through time
(future -> past). Uses a sliding-window approach: each target period
is compared against one reference period at a time (instead of a
monolithic growing KB), keeping peak RAM to ~5-8 GB.

For periods that already have CLEE assigned by INEGI (2021+), resolution
is deterministic (clee_master = clee). For older periods without CLEE
(2015-2020), a spatial tolerance radius (BallTree) pre-filter and a
conditional matching cascade resolve identities probabilistically.

Reads from and writes back to the Silver DuckDB warehouse directly,
populating the `clee_master` column on the `establecimientos` table.
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
    TEXT_COLS_TO_SANITIZE,
)
from src_etl.utils.entity_resolution import (
    assign_new_clees,
    build_single_period_kb,
    get_region_prefix,
    propagate_clees,
)
from src_etl.utils.matching import clean_contact_columns
from src_metadata.models import JobStatus, TaskStatus
from src_metadata.tracker import MetadataTracker

logger = logging.getLogger(__name__)


class CLEEResolver:
    """
    Resolves entity identities across temporal snapshots.

    Uses a sliding-window approach: for each target period without CLEE,
    iterates over all already-resolved periods one at a time as reference,
    propagating clee_master where spatial + fuzzy matching finds a hit.

    Peak memory: ~5-8 GB (1 reference period + 1 target period).

    Modes:
        force=True:  Reset all clee_master, re-resolve from scratch.
        force=False: Only process records where clee_master IS NULL
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

    def _get_connection(self) -> duckdb.DuckDBPyConnection:
        """Get a DuckDB connection to Silver warehouse."""
        return duckdb.connect(str(self.silver_db_path))

    def _count_unresolved(self) -> int:
        """Count rows where clee_master IS NULL."""
        with self._get_connection() as conn:
            return conn.execute("""
                SELECT COUNT(*) FROM establecimientos
                WHERE clee_master IS NULL
            """).fetchone()[0]

    def _get_sorted_periods(self) -> list[date]:
        """
        Get all periods from Silver DuckDB sorted newest-first.

        Returns:
            List of date objects sorted chronologically descending.
            Example: [date(2024, 11, 1), date(2024, 5, 1), ...]
        """
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT DISTINCT period
                FROM establecimientos
                WHERE period IS NOT NULL
                ORDER BY period DESC
            """).fetchall()

        return [r[0] for r in rows]

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

        # Sanitize nulls in text columns
        df = df.with_columns(
            [pl.col(c).fill_null("") for c in TEXT_COLS_TO_SANITIZE if c in df.columns]
        )

        return clean_contact_columns(df)

    def _get_total_rows(self) -> int:
        """Get total row count in establecimientos."""
        with self._get_connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM establecimientos").fetchone()[0]

    def _write_period_results(self, mapping: pl.DataFrame) -> int:
        """
        Write clee_master assignments for a single period to Silver DuckDB.

        Uses batched UPDATE statements via temp table for efficiency.

        Args:
            mapping: DataFrame with columns [id, clee_master]

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
                    "CREATE OR REPLACE TEMP TABLE _clee_updates (id VARCHAR, clee_master VARCHAR)"
                )
                conn.execute("INSERT INTO _clee_updates SELECT * FROM batch")
                result = conn.execute("""
                    UPDATE establecimientos e
                    SET clee_master = u.clee_master
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

        For these periods, clee_master = clee (direct copy via SQL UPDATE).
        No BallTree, no fuzzy matching needed.

        Args:
            periods: All periods sorted newest-first.
            clee_coverage: Dict mapping period -> has_full_clee.
            force: If True, overwrite existing clee_master values.

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
                # Reset all clee_master first
                conn.execute("UPDATE establecimientos SET clee_master = NULL")

            # Copy clee -> clee_master for all rows with CLEE
            conn.execute("""
                UPDATE establecimientos
                SET clee_master = clee
                WHERE clee IS NOT NULL AND clee != ''
            """)

            # Count seeded rows
            count = conn.execute("""
                SELECT COUNT(*) FROM establecimientos
                WHERE clee_master IS NOT NULL
            """).fetchone()[0]

        logger.info(
            f"   clee_master = clee para {count:,} filas ({len(deterministic_periods)} periodos)"
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
          1. Load P from DuckDB (~3M rows, all clee_master NULL)
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

            # Load target period (all rows, clee_master will be NULL)
            df_target = self._load_period(target_period)
            if df_target.is_empty():
                logger.info(f"   {target_period}: vacio, saltando.")
                resolved_periods.append(target_period)
                continue

            target_total = df_target.height

            # Ensure clee_master column exists and is NULL for this period
            if "clee_master" not in df_target.columns:
                df_target = df_target.with_columns(pl.lit(None).cast(pl.Utf8).alias("clee_master"))
            else:
                df_target = df_target.with_columns(
                    pl.when(pl.col("clee_master") == "")
                    .then(pl.lit(None).cast(pl.Utf8))
                    .otherwise(pl.col("clee_master"))
                    .alias("clee_master")
                )

            # Sliding window: compare against each resolved period
            for ref_period in resolved_periods:
                # Count how many still need resolution
                still_missing = df_target.filter(pl.col("clee_master").is_null()).height
                if still_missing == 0:
                    break

                logger.info(f"   vs {ref_period} ({still_missing:,} pendientes)...")

                # Load reference period and build lightweight KB
                df_ref = self._load_period(ref_period)
                ref_kb = build_single_period_kb(df_ref)

                del df_ref
                gc.collect()

                # Propagate CLEEs from reference to target
                df_target = propagate_clees(df_target, ref_kb, self.spatial_tol)

                del ref_kb
                gc.collect()

            # Assign NEW CLEEs to remaining unresolved rows
            still_missing = df_target.filter(pl.col("clee_master").is_null()).height
            if still_missing > 0:
                logger.info(f"   Asignando {still_missing:,} NEW_{region_prefix}-* CLEEs")
                df_target = assign_new_clees(df_target, region_prefix)

            # Write results to DuckDB immediately (no accumulation in RAM)
            mapping = df_target.select(["id", "clee_master"])
            rows_written = self._write_period_results(mapping)
            total_resolved += rows_written

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

        Phase 1 (deterministic): Periods with CLEE get clee_master = clee
        Phase 2 (probabilistic): Periods without CLEE are resolved via
        sliding-window comparison against all already-resolved periods.

        Args:
            force: If True, reset all clee_master and re-resolve from scratch.

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
        identities, and writes `clee_master` back to the same table.

        Args:
            force: If True, reset all clee_master values and re-run from scratch.
                   If False, only process rows where clee_master IS NULL (incremental).

        Returns:
            True if successful, False otherwise
        """
        if force:
            logger.warning(
                "MODO FORCE ACTIVADO: Se resetearan y re-resolveran todos los clee_master."
            )

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
