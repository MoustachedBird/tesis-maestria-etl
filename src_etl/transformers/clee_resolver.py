"""
CLEE Resolution Stage: Entity Resolution (Silver -> Gold)

Resolves entity identities across temporal snapshots, matching
establishments over time and generating master identifiers (CLEEs).

Propagates CLEEs from the most recent period backward through time
(future -> past). Uses a spatial tolerance radius (BallTree) as
pre-filter and a conditional matching cascade for identity resolution.

No mega-regions: processes all states in a single national Knowledge Base.
"""

import gc
import logging
from pathlib import Path

import polars as pl

from src_etl.utils.clee_resolution_config import (
    COLS_NEEDED_FOR_MATCHING,
    SPATIAL_RADIUS_M,
    TEXT_COLS_TO_SANITIZE,
)
from src_etl.utils.entity_resolution import (
    consolidate_global_catalog_lazy,
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

    Handles:
    - Entity identity matching across time periods
    - Master CLEE generation and backward propagation
    - National Knowledge Base management (single KB, all states)
    - Snapshot generation and consolidation
    """

    def __init__(
        self,
        data_root: Path,
        tracker: MetadataTracker,
        input_dir: Path,
        output_dir: Path,
    ):
        """
        Initialize CLEE resolution stage.

        Args:
            data_root: Root data directory
            tracker: MetadataTracker instance for job/task tracking
            input_dir: Directory containing processed/silver data
            output_dir: Directory for resolved/curated output
        """
        self.root = Path(data_root)
        self.tracker = tracker
        self.silver_dir = Path(input_dir) / "denue"
        self.curated_root = Path(output_dir)
        self.curated_dir = self.curated_root / "denue"
        self.master_catalog_path = self.curated_dir / "master_catalog.parquet"

        # Ensure output directory exists
        self.curated_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.curated_dir / "clee_resolution_process.log"

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

            # File handler
            try:
                file_handler = logging.FileHandler(self.log_path, mode="a", encoding="utf-8")
                file_handler.setFormatter(formatter)
                self.logger.addHandler(file_handler)
            except Exception as e:
                self.logger.warning(f"Could not create log file at {self.log_path}: {e}")

    def get_available_states(self) -> list[str]:
        """
        Detect available state folders in the silver/processed directory.

        Returns:
            Sorted list of state codes found in data/processed/denue/state=*
        """
        available_states = set()
        for path in self.silver_dir.glob("state=*"):
            if path.is_dir():
                available_states.add(path.name.split("=")[1])

        return sorted(available_states)

    def load_all_history(self, state_list: list[str]) -> pl.DataFrame:
        """
        Load historical data for all states into a single national DataFrame.

        Args:
            state_list: List of all state codes to load

        Returns:
            Polars DataFrame with complete national history
        """
        dfs = []

        # Columns to read (exclude source_paths which we'll add)
        cols_to_read = [c for c in COLS_NEEDED_FOR_MATCHING if c != "source_paths"]

        for state in state_list:
            try:
                state_path = self.silver_dir / f"state={state}"
                if not state_path.exists():
                    continue

                parquet_files = list(state_path.glob("**/*.parquet"))
                if not parquet_files:
                    continue

                self.logger.info(f"   Loading: {state} ({len(parquet_files)} files)")

                for file_path in parquet_files:
                    df_part = pl.read_parquet(
                        file_path, columns=cols_to_read, hive_partitioning=True
                    ).with_columns(
                        pl.lit(str(file_path.relative_to(self.root))).alias("source_paths")
                    )
                    dfs.append(df_part)

            except Exception as e:
                self.logger.error(f"Error loading {state}: {e}")

        if not dfs:
            return pl.DataFrame()

        full_df = pl.concat(dfs)

        # Sanitize nulls in text columns
        full_df = full_df.with_columns(
            [pl.col(c).fill_null("") for c in TEXT_COLS_TO_SANITIZE if c in full_df.columns]
        )

        return clean_contact_columns(full_df)

    def process_national(self, state_list: list[str]) -> tuple[pl.DataFrame, pl.DataFrame]:
        """
        Process all states as a single national Knowledge Base.

        Propagates CLEEs from the most recent period backward through time.
        Each period is resolved against the accumulated KB, then the KB
        is updated with the new period's data.

        Args:
            state_list: List of all state codes to process

        Returns:
            Tuple of (mapping DataFrame [row_id -> master_clee], knowledge base DataFrame)
        """
        prefix = get_region_prefix(state_list)
        self.logger.info(f"   Region prefix (namespace): {prefix}")

        df_national = self.load_all_history(state_list)
        if df_national.is_empty():
            return pl.DataFrame(), pl.DataFrame()

        total_records = df_national.height
        self.logger.info(f"   Total records loaded: {total_records:,}")

        # Get unique periods sorted chronologically (newest first)
        unique_periods = df_national["period"].unique().to_list()
        periods = sorted(
            unique_periods,
            key=lambda x: (int(x.split("_")[1]), int(x.split("_")[0])),
            reverse=True,
        )

        self.logger.info(f"   Periods detected ({len(periods)}): {periods}")

        dict_dfs = {p: df_national.filter(pl.col("period") == p) for p in periods}

        # Free the full national DF now that we have per-period splits
        del df_national
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

            map_accum = map_accum.vstack(df_resolved.select(["row_id", "master_clee"]))

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

    def _save_local_results(self, state_list: list[str], mapping: pl.DataFrame) -> list[str]:
        """
        Save CLEE resolution results to partitioned Parquet files.

        Joins the row_id -> master_clee mapping onto the original Silver
        Parquets and writes enriched files to the curated directory.

        Args:
            state_list: List of states processed
            mapping: Mapping DataFrame with row_id -> master_clee

        Returns:
            List of relative paths where files were saved
        """
        updated_paths = []

        for state in state_list:
            state_path = self.silver_dir / f"state={state}"
            if not state_path.exists():
                continue

            for parquet_file in state_path.glob("**/*.parquet"):
                # Read original data
                df_original = pl.read_parquet(parquet_file, hive_partitioning=True)

                # Join with mapping to add master_clee
                df_enriched = df_original.join(mapping, on="row_id", how="left").select(
                    [*df_original.columns, "master_clee"]
                )

                # Save to curated directory maintaining partition structure
                rel_path = parquet_file.relative_to(self.silver_dir)
                output_path = self.curated_dir / rel_path
                output_path.parent.mkdir(parents=True, exist_ok=True)

                df_enriched.write_parquet(output_path)
                updated_paths.append(str(rel_path))

        return updated_paths

    def _consolidate_snapshots(self, snapshot_dir: Path):
        """
        Consolidate snapshot(s) into master catalog.

        Args:
            snapshot_dir: Directory containing snapshot files
        """
        snapshot_files = list(snapshot_dir.glob("kb_*.parquet"))

        if not snapshot_files:
            self.logger.warning("No snapshots found.")
            return

        self.logger.info(f"   Consolidating {len(snapshot_files)} snapshot(s)...")

        lazy_frames = [pl.scan_parquet(f) for f in snapshot_files]
        master_kb = consolidate_global_catalog_lazy(lazy_frames).collect()

        master_kb.write_parquet(self.master_catalog_path)
        self.logger.info(
            f"   Master Catalog: {self.master_catalog_path.name} ({master_kb.height:,} entities)"
        )

    def run(self) -> bool:
        """
        Run CLEE resolution pipeline.

        Loads all states into a single national KB, resolves entity
        identities backward through time, and generates master catalog.

        Returns:
            True if successful, False otherwise
        """
        self.curated_root.mkdir(parents=True, exist_ok=True)

        # Create snapshot directory
        snapshot_dir = self.curated_dir / "snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        # Create job run
        run_id = self.tracker.create_run(
            job_name="denue_clee_resolver",
            job_type="CLEE_RESOLUTION",
            trigger_type="MANUAL",
        )

        all_states = self.get_available_states()

        if not all_states:
            self.logger.info("No states found to process.")
            self.tracker.complete_run(run_id, JobStatus.SUCCESS)
            return True

        self.logger.info(
            f"Starting CLEE resolution: {len(all_states)} states, "
            f"national KB, radius={self.spatial_tol}m"
        )

        # Create a single CLEE resolution task for the national run
        task_id = self.tracker.create_clee_resolution_task(
            run_id=run_id,
            task_name="clee_resolution_national",
            region_group="NATIONAL",
            state_list=all_states,
        )

        self.tracker.start_clee_resolution_task(task_id)

        try:
            national_map, national_kb = self.process_national(all_states)

            if not national_map.is_empty():
                # Save enriched parquets
                self.logger.info("Saving enriched Parquet files...")
                updated_paths = self._save_local_results(all_states, national_map)
                self.logger.info(f"   Saved {len(updated_paths)} files")

                # Save national KB snapshot
                snapshot_path = snapshot_dir / "kb_national.parquet"
                national_kb.write_parquet(snapshot_path)
                self.logger.info(f"   Snapshot saved: {snapshot_path.name}")

                # Count periods
                n_periods = 0
                if "periods" in national_kb.columns:
                    # KB has list column 'periods' — count unique across all entities
                    n_periods = national_kb.select(
                        pl.col("periods").list.explode().unique().len()
                    ).item()

                # Complete task
                self.tracker.complete_clee_resolution_task(
                    task_id=task_id,
                    status=TaskStatus.SUCCESS,
                    entities_input=national_map.height,
                    entities_matched=national_kb.height,
                    master_clees_created=national_kb.height,
                    periods_processed=n_periods,
                    output_path=str(snapshot_path),
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

            # Consolidate snapshots into master catalog
            self.logger.info("Consolidating Master Catalog from snapshots...")
            self._consolidate_snapshots(snapshot_dir)

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
