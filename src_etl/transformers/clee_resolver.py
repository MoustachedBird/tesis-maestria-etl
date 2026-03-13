"""
CLEE Resolution Stage: Entity Resolution (Silver DuckDB)

Resolves entity identities across temporal snapshots, matching
establishments over time and generating master identifiers (CLEEs).

Propagates CLEEs from the most recent period backward through time
(future -> past). Uses a spatial tolerance radius (BallTree) as
pre-filter and a conditional matching cascade for identity resolution.

Reads from and writes back to the Silver DuckDB warehouse directly,
populating the `clee_master` column on the `establecimientos` table.

Memory-efficient: loads one period at a time from DuckDB instead of
loading all ~76M rows into RAM simultaneously.
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
    get_region_prefix,
    initialize_knowledge_base,
    resolve_period_logic,
    update_kb_logic,
)
from src_etl.utils.matching import clean_contact_columns
from src_metadata.models import JobStatus, TaskStatus
from src_metadata.tracker import MetadataTracker

logger = logging.getLogger(__name__)


class CLEEResolver:
    """
    Resolves entity identities across temporal snapshots.

    Reads establishment data from Silver DuckDB one period at a time,
    runs entity resolution (future -> past propagation), and writes
    `clee_master` back to the same table via UPDATE statements.

    Memory usage: KB + 1 period (~5-8 GB) instead of all periods (~40+ GB).

    Modes:
        force=True:  Copy all existing `clee` values to `clee_master`,
                     then run the algorithm on remaining NULLs.
        force=False: Only process records where `clee_master IS NULL`
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

    def _seed_clee_master_from_clee(self) -> int:
        """
        Copy existing `clee` values into `clee_master` for all rows.

        Used in force mode to reset and re-seed before running the
        resolution algorithm on remaining NULLs.

        Returns:
            Number of rows seeded.
        """
        with self._get_connection() as conn:
            # First, NULL out all clee_master to start fresh
            conn.execute("UPDATE establecimientos SET clee_master = NULL")

            # Copy non-empty clee values to clee_master
            conn.execute("""
                UPDATE establecimientos
                SET clee_master = clee
                WHERE clee IS NOT NULL AND clee != ''
            """)

            # Count how many got seeded
            count = conn.execute("""
                SELECT COUNT(*) FROM establecimientos
                WHERE clee_master IS NOT NULL
            """).fetchone()[0]

            logger.info(f"   🌱 clee_master sembrado desde clee: {count:,} filas")
            return count

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

    def _write_results_to_silver(self, mapping: pl.DataFrame) -> int:
        """
        Write clee_master assignments back to Silver DuckDB.

        Uses batched UPDATE statements for efficiency.

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

        logger.info(f"   💾 Escribiendo {total:,} valores clee_master en Silver DuckDB...")

        with self._get_connection() as conn:
            for offset in range(0, total, batch_size):
                batch = mapping.slice(offset, batch_size)

                # Build a temp table approach for efficient batch update
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

                if (offset + batch_size) % 200_000 == 0 or offset + batch_size >= total:
                    logger.info(
                        f"      Actualizados {min(offset + batch_size, total):,}/{total:,} filas"
                    )

        logger.info(f"   ✅ Total filas actualizadas: {updated:,}")
        return updated

    def process_national(self) -> tuple[pl.DataFrame, pl.DataFrame]:
        """
        Process all periods as a single national Knowledge Base.

        Loads one period at a time from DuckDB to keep memory usage low.
        Propagates CLEEs from the most recent period backward through time.
        Each period is resolved against the accumulated KB, then the KB
        is updated with the new period's data.

        Peak memory: KB (~2-4 GB) + 1 period (~3 GB) + mapping (~1 GB).

        Returns:
            Tuple of (mapping DataFrame [id -> clee_master], knowledge base DataFrame)
        """
        # Get periods sorted newest-first
        periods = self._get_sorted_periods()

        if not periods:
            return pl.DataFrame(), pl.DataFrame()

        logger.info(f"   📅 Periodos detectados ({len(periods)}): {periods}")

        # Get region prefix from distinct cve_ent values
        with self._get_connection() as conn:
            state_rows = conn.execute(
                "SELECT DISTINCT cve_ent FROM establecimientos ORDER BY cve_ent"
            ).fetchall()
        state_list = [r[0] for r in state_rows] if state_rows else ["NATIONAL"]

        prefix = get_region_prefix(state_list)
        logger.info(f"   🏷️ Prefijo de región (namespace): {prefix}")

        # --- Step 1: Load base period (most recent) and initialize KB ---
        logger.info(f"🧠 Inicializando KB con periodo base: {periods[0]}...")
        df_base = self._load_period(periods[0])

        if df_base.is_empty():
            return pl.DataFrame(), pl.DataFrame()

        logger.info(f"   📦 Filas del periodo base: {df_base.height:,}")
        map_accum, kb = initialize_knowledge_base(df_base, prefix)
        logger.info(f"   ✅ Entidades iniciales en KB: {kb.height:,}")

        del df_base
        gc.collect()

        # --- Step 2: Process older periods one by one (future -> past) ---
        for i in range(1, len(periods)):
            period = periods[i]

            logger.info(f"🔗 [{i}/{len(periods) - 1}] Cargando periodo {period}...")
            df_period = self._load_period(period)

            if df_period.is_empty():
                logger.info(f"   ⚠️ {period}: vacío, saltando.")
                continue

            target_rows = df_period.height
            kb_before = kb.height

            logger.info(
                f"   ⚙️ Resolviendo {period} ({target_rows:,} registros, "
                f"radio={self.spatial_tol}m)..."
            )

            # Resolve entity identities
            df_resolved = resolve_period_logic(
                df_period,
                kb,
                prefix,
                self.spatial_tol,
            )

            # Free period data immediately — we only need the resolved result
            del df_period
            gc.collect()

            map_accum = map_accum.vstack(df_resolved.select(["id", "clee_master"]))

            # Update knowledge base
            kb = update_kb_logic(kb, df_resolved)

            # Log growth
            new_entities = kb.height - kb_before
            logger.info(
                f"   ✅ {period} resuelto: +{new_entities:,} entidades nuevas, "
                f"KB total: {kb.height:,}"
            )

            # Free resolved DF for this period
            del df_resolved
            gc.collect()

        return map_accum, kb

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
                "⚠️ MODO FORCE ACTIVADO: Se resetearán y re-resolverán todos los clee_master."
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
            # --- Force mode: seed clee_master from clee, then resolve NULLs ---
            if force:
                self._seed_clee_master_from_clee()

            # --- Check how many rows need resolution ---
            unresolved_count = self._count_unresolved()
            logger.info(
                f"🔍 Filas pendientes de resolución (clee_master IS NULL): {unresolved_count:,}"
            )

            if unresolved_count == 0:
                logger.info("✨ Todas las entidades ya resueltas. Nada por procesar.")
                self.tracker.complete_clee_resolution_task(
                    task_id=task_id,
                    status=TaskStatus.SKIPPED,
                    error_message="No unresolved rows",
                )
                self.tracker.complete_run(run_id, JobStatus.SUCCESS)
                return True

            total_rows = self._get_total_rows()
            logger.info(
                f"⚙️ Iniciando resolución CLEE: {total_rows:,} filas totales, "
                f"{unresolved_count:,} sin resolver, radio={self.spatial_tol}m"
            )

            # --- Run entity resolution (period by period) ---
            national_map, national_kb = self.process_national()

            if not national_map.is_empty():
                # Filter mapping to only include rows that were unresolved
                # (don't overwrite already-resolved clee_master in incremental mode)
                if not force:
                    # Get the set of unresolved IDs
                    with self._get_connection() as conn:
                        unresolved_ids = pl.from_arrow(
                            conn.execute("""
                                SELECT id FROM establecimientos
                                WHERE clee_master IS NULL
                            """).fetch_arrow_table()
                        )
                    # Filter mapping to only unresolved IDs
                    national_map = national_map.join(unresolved_ids, on="id", how="semi")
                    logger.info(
                        f"   🔽 Filtrado a {national_map.height:,} filas sin resolver "
                        f"(preservando clee_master existentes)"
                    )

                # Write results back to Silver DuckDB
                rows_updated = self._write_results_to_silver(
                    national_map.select(["id", "clee_master"])
                )

                # Count remaining unresolved
                remaining = self._count_unresolved()

                # Count periods processed
                n_periods = 0
                if "periods" in national_kb.columns:
                    n_periods = national_kb.select(
                        pl.col("periods").list.explode().unique().len()
                    ).item()

                # Complete task
                self.tracker.complete_clee_resolution_task(
                    task_id=task_id,
                    status=TaskStatus.SUCCESS,
                    entities_input=total_rows,
                    entities_matched=national_kb.height,
                    master_clees_created=national_kb.height,
                    periods_processed=n_periods,
                    output_path=str(self.silver_db_path),
                )

                logger.info(
                    f"✨ Resolución CLEE terminada. {rows_updated:,} filas actualizadas, "
                    f"{remaining:,} pendientes."
                )
                logger.info(
                    f"   📊 Resumen: {national_kb.height:,} entidades únicas, "
                    f"{n_periods} periodos procesados"
                )
            else:
                self.tracker.complete_clee_resolution_task(
                    task_id=task_id,
                    status=TaskStatus.SKIPPED,
                    error_message="No data to resolve",
                )

            # Cleanup memory
            del national_map
            del national_kb
            gc.collect()

            self.tracker.complete_run(run_id, JobStatus.SUCCESS)
            return True

        except Exception as e:
            logger.error(f"   ❌ Error en resolución CLEE: {e}")
            self.tracker.complete_clee_resolution_task(
                task_id=task_id,
                status=TaskStatus.FAILED,
                error_message=str(e),
            )
            self.tracker.complete_run(run_id, JobStatus.FAILED)
            return False
