"""
CLEE Resolution Stage: Entity Resolution (Silver DuckDB)

Resolves entity identities across temporal snapshots, matching
establishments over time and generating master identifiers (CLEEs).

Propagates CLEEs from the most recent period backward through time
(future -> past). Uses a spatial tolerance radius (BallTree) as
pre-filter and a conditional matching cascade for identity resolution.

Reads from and writes back to the Silver DuckDB warehouse directly,
populating the `clee_master` column on the `establecimientos` table.
"""

import gc
import logging
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


class CLEEResolver:
    """
    Resolves entity identities across temporal snapshots.

    Reads establishment data from Silver DuckDB, runs entity resolution
    (future -> past propagation), and writes `clee_master` back to the
    same table via UPDATE statements.

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

        self._setup_logging()

    def _setup_logging(self):
        """Configure logging for CLEE resolution process."""
        self.logger = logging.getLogger("CLEEResolver")
        self.logger.setLevel(logging.INFO)

        if not self.logger.handlers:
            formatter = logging.Formatter("%(asctime)s - %(message)s", datefmt="%H:%M:%S")

            # Console handler
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            self.logger.addHandler(console_handler)

    def _get_connection(self) -> duckdb.DuckDBPyConnection:
        """Get a DuckDB connection to Silver warehouse."""
        return duckdb.connect(str(self.silver_db_path))

    def _seed_clee_master_from_clee(self) -> int:
        """
        Copy existing `clee` values into `clee_master` for all rows.

        Used in force mode to reset and re-seed before running the
        resolution algorithm on remaining NULLs.

        Returns:
            Number of rows updated.
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

            self.logger.info(f"   Seeded clee_master from clee: {count:,} rows")
            return count

    def _count_unresolved(self) -> int:
        """Count rows where clee_master IS NULL."""
        with self._get_connection() as conn:
            return conn.execute("""
                SELECT COUNT(*) FROM establecimientos
                WHERE clee_master IS NULL
            """).fetchone()[0]

    def _load_data_for_resolution(self, only_unresolved: bool = False) -> pl.DataFrame:
        """
        Load establishment data from Silver DuckDB for resolution.

        Uses SILVER_READ_QUERY which JOINs with catalog tables to get
        `nombre_act` and casts ENUM columns to VARCHAR.

        Args:
            only_unresolved: If True, only load rows where clee_master IS NULL.
                           If False, load ALL rows (needed for KB construction).

        Returns:
            Polars DataFrame with columns from COLS_NEEDED_FOR_MATCHING
        """
        query = SILVER_READ_QUERY
        if only_unresolved:
            query += "\n    WHERE e.clee_master IS NULL"

        self.logger.info("   Loading data from Silver DuckDB...")
        with self._get_connection() as conn:
            result = conn.execute(query)
            df = pl.from_arrow(result.fetch_arrow_table())

        self.logger.info(f"   Loaded {df.height:,} rows")

        if df.is_empty():
            return df

        # Sanitize nulls in text columns
        df = df.with_columns(
            [pl.col(c).fill_null("") for c in TEXT_COLS_TO_SANITIZE if c in df.columns]
        )

        return clean_contact_columns(df)

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

        self.logger.info(f"   Writing {total:,} clee_master values to Silver DuckDB...")

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
                    self.logger.info(
                        f"      Updated {min(offset + batch_size, total):,}/{total:,} rows"
                    )

        self.logger.info(f"   Total rows updated: {updated:,}")
        return updated

    def process_national(self, df_all: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
        """
        Process all data as a single national Knowledge Base.

        Propagates CLEEs from the most recent period backward through time.
        Each period is resolved against the accumulated KB, then the KB
        is updated with the new period's data.

        Args:
            df_all: Full national DataFrame with all periods

        Returns:
            Tuple of (mapping DataFrame [id -> clee_master], knowledge base DataFrame)
        """
        # Use all unique cve_ent values as state list for region prefix
        if "cve_ent" in df_all.columns:
            state_list = sorted(df_all["cve_ent"].unique().to_list())
        else:
            state_list = ["NATIONAL"]

        prefix = get_region_prefix(state_list)
        self.logger.info(f"   Region prefix (namespace): {prefix}")

        total_records = df_all.height
        self.logger.info(f"   Total records for resolution: {total_records:,}")

        # Get unique periods sorted chronologically (newest first)
        unique_periods = df_all["period"].unique().to_list()
        periods = sorted(
            unique_periods,
            key=lambda x: (int(x.split("_")[1]), int(x.split("_")[0])),
            reverse=True,
        )

        self.logger.info(f"   Periods detected ({len(periods)}): {periods}")

        dict_dfs = {p: df_all.filter(pl.col("period") == p) for p in periods}

        # Free the full national DF now that we have per-period splits
        del df_all
        gc.collect()

        # Initialize Knowledge Base with the most recent period
        self.logger.info(f"      Seeding KB with base period: {periods[0]}...")
        map_accum, kb = initialize_knowledge_base(dict_dfs[periods[0]], prefix)
        self.logger.info(f"         Initial entities in KB: {kb.height:,}")

        # Process older periods (propagate CLEEs from future to past)
        for i in range(1, len(periods)):
            period = periods[i]
            target_rows = dict_dfs[period].height
            kb_before = kb.height

            self.logger.info(
                f"      [{i}/{len(periods) - 1}] Resolving period {period} "
                f"({target_rows:,} records)..."
            )
            self.logger.info(
                f"         Propagating CLEEs from future -> {period} (radius={self.spatial_tol}m)"
            )

            # Resolve entity identities
            df_resolved = resolve_period_logic(
                dict_dfs[period],
                kb,
                prefix,
                self.spatial_tol,
            )

            map_accum = map_accum.vstack(df_resolved.select(["id", "clee_master"]))

            # Update knowledge base
            kb = update_kb_logic(kb, df_resolved)

            # Log growth
            new_entities = kb.height - kb_before
            self.logger.info(f"         KB updated with {period}:")
            self.logger.info(f"            New entities (not in future): +{new_entities:,}")
            self.logger.info(f"            Total KB size: {kb.height:,} records")
            self.logger.info(f"         {period} done.")

            # Free resolved DF for this period
            del df_resolved
            gc.collect()

        return map_accum, kb

    def run(self, force: bool = False) -> bool:
        """
        Run CLEE resolution pipeline.

        Reads from Silver DuckDB, resolves entity identities, and writes
        `clee_master` back to the same table.

        Args:
            force: If True, reset all clee_master values and re-run from scratch.
                   If False, only process rows where clee_master IS NULL (incremental).

        Returns:
            True if successful, False otherwise
        """
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
                self.logger.info("FORCE MODE: Resetting clee_master and re-seeding from clee...")
                self._seed_clee_master_from_clee()

            # --- Check how many rows need resolution ---
            unresolved_count = self._count_unresolved()
            self.logger.info(f"Rows needing resolution (clee_master IS NULL): {unresolved_count:,}")

            if unresolved_count == 0:
                self.logger.info("No unresolved rows. CLEE resolution complete (nothing to do).")
                self.tracker.complete_clee_resolution_task(
                    task_id=task_id,
                    status=TaskStatus.SKIPPED,
                    error_message="No unresolved rows",
                )
                self.tracker.complete_run(run_id, JobStatus.SUCCESS)
                return True

            # --- Load ALL data for KB construction ---
            # We need the full history (including already-resolved rows) to build
            # the Knowledge Base, but we only assign clee_master to unresolved rows.
            self.logger.info("Loading full history for KB construction...")
            df_all = self._load_data_for_resolution(only_unresolved=False)

            if df_all.is_empty():
                self.logger.info("No data found in Silver DuckDB.")
                self.tracker.complete_clee_resolution_task(
                    task_id=task_id,
                    status=TaskStatus.SKIPPED,
                    error_message="No data in Silver DuckDB",
                )
                self.tracker.complete_run(run_id, JobStatus.SUCCESS)
                return True

            total_rows = df_all.height
            self.logger.info(
                f"Starting CLEE resolution: {total_rows:,} total rows, "
                f"{unresolved_count:,} unresolved, "
                f"radius={self.spatial_tol}m"
            )

            # --- Run entity resolution ---
            national_map, national_kb = self.process_national(df_all)

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
                    self.logger.info(
                        f"   Filtered to {national_map.height:,} unresolved rows "
                        f"(preserving existing clee_master)"
                    )

                # Write results back to Silver DuckDB
                rows_updated = self._write_results_to_silver(
                    national_map.select(["id", "clee_master"])
                )

                # Count remaining unresolved
                remaining = self._count_unresolved()
                self.logger.info(
                    f"   Resolution complete: {rows_updated:,} rows updated, "
                    f"{remaining:,} still unresolved"
                )

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
            self.logger.info("CLEE resolution complete.")
            return True

        except Exception as e:
            self.logger.error(f"Error during CLEE resolution: {e}")
            self.tracker.complete_clee_resolution_task(
                task_id=task_id,
                status=TaskStatus.FAILED,
                error_message=str(e),
            )
            self.tracker.complete_run(run_id, JobStatus.FAILED)
            return False
