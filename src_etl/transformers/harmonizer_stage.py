"""
Harmonization Stage: Entity Resolution (Silver → Gold)

Resolves entity identities across temporal snapshots, matching
establishments over time and generating master identifiers (CLEEs).

This module contains the core harmonization logic extracted from the
original DenueHarmonizer class, now integrated with MetadataTracker
for comprehensive observability.
"""

import polars as pl
import logging
import gc
from pathlib import Path
from typing import List, Tuple, Dict, Any
from src_metadata.tracker import MetadataTracker
from src_metadata.models import JobStatus, TaskStatus
from src_etl.utils.matching import clean_contact_columns

# Import refactored harmonization utilities
from src_etl.utils.harmonizer_config import (
    MEGA_REGIONS,
    SILVER_SCHEMA,
    COLS_NEEDED_FOR_MATCHING,
    TEXT_COLS_TO_SANITIZE,
)
from src_etl.utils.harmonizer_core import (
    get_region_prefix,
    initialize_knowledge_base,
    resolve_period_logic,
    update_kb_logic,
    consolidate_global_catalog_lazy,
)


class HarmonizerStage:
    """
    Resolves entity identities across temporal snapshots.

    Handles:
    - Entity identity matching across time periods
    - Master CLEE generation
    - Knowledge base management
    - Region-based processing
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
        Initialize harmonizer stage.

        Args:
            data_root: Root data directory
            tracker: MetadataTracker instance for job/task tracking
            input_dir: Directory containing processed/silver data
            output_dir: Directory for harmonized/curated output
        """
        self.root = Path(data_root)
        self.tracker = tracker
        self.silver_dir = Path(input_dir) / "denue"  # Processed data location
        self.curated_root = Path(output_dir)
        self.curated_dir = self.curated_root / "denue"
        self.master_catalog_path = self.curated_dir / "master_catalog.parquet"

        # Ensure output directory exists
        self.curated_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.curated_dir / "harmonization_process.log"

        # Harmonization parameters
        self.spatial_tol = 60
        self.identity_thresh = 80
        self.activity_thresh = 60

        self._setup_logging()

    def _setup_logging(self):
        """Configure logging for harmonization process."""
        self.logger = logging.getLogger("Harmonizer")
        self.logger.setLevel(logging.INFO)

        if not self.logger.handlers:
            formatter = logging.Formatter(
                "%(asctime)s - %(message)s", datefmt="%H:%M:%S"
            )

            # Console handler
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            self.logger.addHandler(console_handler)

            # File handler
            try:
                file_handler = logging.FileHandler(
                    self.log_path, mode="a", encoding="utf-8"
                )
                file_handler.setFormatter(formatter)
                self.logger.addHandler(file_handler)
            except Exception as e:
                print(f"⚠️ No se pudo crear el archivo de log en {self.log_path}: {e}")

    def get_processing_groups(self) -> List[List[str]]:
        """
        Detect available state folders and organize into processing groups.

        Returns:
            List of state lists (mega-regions + individual states)
        """
        available_states = set()
        for path in self.silver_dir.glob("state=*"):
            if path.is_dir():
                available_states.add(path.name.split("=")[1])

        groups = []
        handled_states = set()

        # Group mega-regions
        for region_name, states_in_region in MEGA_REGIONS.items():
            current_group = [s for s in states_in_region if s in available_states]
            if current_group:
                groups.append(current_group)
                self.logger.info(
                    f"🔗 Mega-Región detectada ({region_name}): {current_group}"
                )
                handled_states.update(current_group)

        # Add individual states
        for state in sorted(list(available_states)):
            if state not in handled_states:
                groups.append([state])

        return groups

    def load_group_history(self, state_list: List[str]) -> pl.DataFrame:
        """
        Load historical data for a group of states.

        Args:
            state_list: List of state codes to load

        Returns:
            Polars DataFrame with complete history for all states
        """
        dfs = []

        # Columns to read (exclude source_paths which we'll add)
        cols_to_read = [c for c in COLS_NEEDED_FOR_MATCHING if c != "source_paths"]

        for state in state_list:
            try:
                state_path = self.silver_dir / f"state={state}"
                if not state_path.exists():
                    continue

                # Find all parquet files
                parquet_files = list(state_path.glob("**/*.parquet"))
                if not parquet_files:
                    continue

                self.logger.info(f"   📂 Cargando: {state}")

                for file_path in parquet_files:
                    # Read file and inject its path for lineage tracking
                    df_part = pl.read_parquet(
                        file_path, columns=cols_to_read, hive_partitioning=True
                    ).with_columns(
                        pl.lit(str(file_path.relative_to(self.root))).alias(
                            "source_paths"
                        )
                    )
                    dfs.append(df_part)

            except Exception as e:
                self.logger.error(f"❌ Error cargando {state}: {e}")

        if not dfs:
            return pl.DataFrame()

        # Concatenate all files
        full_df = pl.concat(dfs)

        # Sanitize nulls in text columns
        full_df = full_df.with_columns(
            [
                pl.col(c).fill_null("")
                for c in TEXT_COLS_TO_SANITIZE
                if c in full_df.columns
            ]
        )

        return clean_contact_columns(full_df)

    def process_group(self, state_list: List[str]) -> Tuple[pl.DataFrame, pl.DataFrame]:
        """
        Process a single group/region for entity harmonization.

        Args:
            state_list: List of state codes in this group

        Returns:
            Tuple of (mapping DataFrame, knowledge base DataFrame)
        """
        prefix = get_region_prefix(state_list)
        self.logger.info(f"   🔑 Namespace de Región: {prefix}")

        df_group = self.load_group_history(state_list)
        if df_group.is_empty():
            return pl.DataFrame(), pl.DataFrame()

        # Get unique periods sorted chronologically (newest first)
        unique_periods = df_group["period"].unique().to_list()
        periods = sorted(
            unique_periods,
            key=lambda x: (int(x.split("_")[1]), int(x.split("_")[0])),
            reverse=True,
        )

        self.logger.info(f"   📅 Periodos detectados: {periods}")

        dict_dfs = {p: df_group.filter(pl.col("period") == p) for p in periods}

        # Initialize knowledge base with most recent period
        self.logger.info(
            f"      🌱 Sembrando Knowledge Base (KB) con año base: {periods[0]}..."
        )
        map_accum, kb = initialize_knowledge_base(dict_dfs[periods[0]], prefix)
        self.logger.info(f"         📚 Entidades iniciales en memoria: {kb.height:,}")

        # Process older periods
        for i in range(1, len(periods)):
            period = periods[i]
            target_rows = dict_dfs[period].height
            kb_before = kb.height

            self.logger.info(
                f"      🔄 [{i}/{len(periods) - 1}] Analizando periodo {period} ({target_rows:,} negocios)..."
            )
            self.logger.info(
                f"         ↳ Propagando CLEEs del futuro hacia {period}..."
            )

            # Resolve entity identities
            df_resolved = resolve_period_logic(
                dict_dfs[period],
                kb,
                prefix,
                self.spatial_tol,
                self.identity_thresh,
                self.activity_thresh,
            )

            map_accum = map_accum.vstack(df_resolved.select(["row_id", "master_clee"]))

            # Update knowledge base
            kb = update_kb_logic(kb, df_resolved)

            # Log growth
            new_entities = kb.height - kb_before
            self.logger.info(f"         🧠 Memoria actualizada con {period}:")
            self.logger.info(
                f"            📈 Nuevas entidades (no estaban en el futuro): +{new_entities:,}"
            )
            self.logger.info(
                f"            📚 Total acumulado en KB: {kb.height:,} registros"
            )
            self.logger.info(f"         ✅ {period} Terminado.")

        return map_accum, kb

    def _save_local_results(
        self, state_list: List[str], mapping: pl.DataFrame
    ) -> List[str]:
        """
        Save harmonization results to partitioned Parquet files.

        Args:
            state_list: List of states processed
            mapping: Mapping DataFrame with row_id → master_clee

        Returns:
            List of paths where files were saved
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
        Consolidate regional snapshots into master catalog.

        Args:
            snapshot_dir: Directory containing snapshot files
        """
        snapshot_files = list(snapshot_dir.glob("kb_*.parquet"))

        if not snapshot_files:
            self.logger.warning("⚠️ No se encontraron snapshots.")
            return

        self.logger.info(f"   📦 Uniendo {len(snapshot_files)} archivos regionales...")

        lazy_frames = [pl.scan_parquet(f) for f in snapshot_files]
        master_kb = consolidate_global_catalog_lazy(lazy_frames).collect()

        master_kb.write_parquet(self.master_catalog_path)
        self.logger.info(
            f"   ✅ Master Catalog: {self.master_catalog_path.name} ({master_kb.height:,} entidades)"
        )

    def run(self) -> bool:
        """
        Run harmonization pipeline using MetadataTracker.

        Returns:
            True if successful, False otherwise
        """
        self.curated_root.mkdir(parents=True, exist_ok=True)

        # Create snapshot directory
        snapshot_dir = self.curated_dir / "snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        # Create job run
        run_id = self.tracker.create_run(
            job_name="denue_harmonizer",
            job_type="HARMONIZE",
            trigger_type="MANUAL",
        )

        all_groups = self.get_processing_groups()

        if not all_groups:
            self.logger.info("❌ No hay grupos para procesar.")
            self.tracker.complete_run(run_id, JobStatus.SUCCESS)
            return True

        self.logger.info(
            f"🚀 Iniciando harmonización: {len(all_groups)} grupos detectados"
        )

        tasks_succeeded = 0
        tasks_failed = 0

        for i, group in enumerate(all_groups, 1):
            prefix = get_region_prefix(group)
            group_name = "+".join(group)

            # Create harmonize task
            task_id = self.tracker.create_harmonize_task(
                run_id=run_id,
                task_name=f"harmonize_{group_name}",
                region_group=prefix,
                state_list=group,
            )

            self.logger.info(f"🚀 [{i}/{len(all_groups)}] Procesando: {group_name}")

            self.tracker.start_harmonize_task(task_id)

            try:
                group_map, group_kb = self.process_group(group)

                if not group_map.is_empty():
                    # Save results
                    updated_paths = self._save_local_results(group, group_map)

                    # Save snapshot
                    snapshot_path = snapshot_dir / f"kb_{prefix}.parquet"
                    group_kb.write_parquet(snapshot_path)
                    self.logger.info(f"   📸 Snapshot guardado: {snapshot_path.name}")

                    # Complete task
                    self.tracker.complete_harmonize_task(
                        task_id=task_id,
                        status=TaskStatus.SUCCESS,
                        entities_input=group_map.height,
                        entities_matched=group_kb.height,
                        master_clees_created=group_kb.height,
                        periods_processed=len(group_map["period"].unique())
                        if "period" in group_map.columns
                        else 0,
                        output_path=str(snapshot_path),
                    )
                    tasks_succeeded += 1
                else:
                    self.tracker.complete_harmonize_task(
                        task_id=task_id,
                        status=TaskStatus.SKIPPED,
                        error_message="No data to harmonize",
                    )

                # Cleanup memory
                del group_map
                del group_kb
                gc.collect()

            except Exception as e:
                self.logger.error(f"❌ Error procesando {group_name}: {e}")
                self.tracker.complete_harmonize_task(
                    task_id=task_id,
                    status=TaskStatus.FAILED,
                    error_message=str(e),
                )
                tasks_failed += 1

        # Consolidate snapshots
        self.logger.info("📚 Consolidando Master Catalog desde snapshots...")
        try:
            self._consolidate_snapshots(snapshot_dir)
        except Exception as e:
            self.logger.error(f"❌ Error consolidando snapshots: {e}")

        # Complete job run
        final_status = JobStatus.SUCCESS if tasks_failed == 0 else JobStatus.PARTIAL
        self.tracker.complete_run(run_id, final_status)

        self.logger.info("✨ Proceso Finalizado.")
        self.logger.info(
            f"   📊 Resumen: {tasks_succeeded} exitosos, {tasks_failed} fallidos"
        )

        return tasks_failed == 0
