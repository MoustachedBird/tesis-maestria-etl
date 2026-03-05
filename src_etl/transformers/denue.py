"""
Unified DENUE Data Transformer

Orchestrates the complete ETL transformation pipeline:
1. TRANSFORM Stage: ZIP -> Silver DuckDB (Bronze -> Silver)
2. HARMONIZE Stage: Entity Resolution (Silver -> Curated)
3. LOAD Stage: Silver DuckDB -> Gold DuckDB (Silver -> Gold)

Uses MetadataTracker for comprehensive tracking and observability.
"""

import logging
from pathlib import Path

from src_etl.core.config import settings
from src_etl.loaders.loader import SilverToGoldLoader
from src_etl.transformers.harmonizer import EntityResolutionHarmonizer
from src_etl.transformers.processor import BronzeToSilverProcessor
from src_metadata.tracker import MetadataTracker

logger = logging.getLogger(__name__)


class DenuePipelineOrchestrator:
    """
    Unified transformer for DENUE data pipeline.

    Coordinates transform, harmonize, and load stages with full
    metadata tracking and observability.

    Usage:
        transformer = DenuePipelineOrchestrator(data_root=Path("data"))

        # Run individual stages
        transformer.run_transform_stage(force=False)
        transformer.run_load_stage()

        # Or run complete pipeline
        transformer.run_full_pipeline(force=False)
    """

    def __init__(
        self,
        data_root: Path,
        metadata_db: Path | None = None,
        silver_db_path: Path | None = None,
        gold_db_path: Path | None = None,
        curated_output_dir: Path | None = None,
    ):
        """
        Initialize unified transformer.

        Args:
            data_root: Root data directory
            metadata_db: Path to metadata.duckdb (default: data/metadata.duckdb)
            silver_db_path: Path to Silver DuckDB (default: from settings)
            gold_db_path: Path to Gold DuckDB (default: from settings)
            curated_output_dir: Output for curated/harmonized data (default: from settings)
        """
        self.data_root = Path(data_root)

        # Setup metadata tracker
        if metadata_db is None:
            metadata_db = self.data_root / "metadata.duckdb"
        self.metadata_db = Path(metadata_db)
        self.tracker = MetadataTracker(self.metadata_db)

        # Resolve paths
        self.silver_db_path = (
            Path(silver_db_path) if silver_db_path else settings.SILVER_DUCKDB_PATH
        )
        self.gold_db_path = Path(gold_db_path) if gold_db_path else settings.GOLD_DUCKDB_PATH
        self.curated_output_dir = (
            Path(curated_output_dir) if curated_output_dir else settings.CURATED_OUTPUT_DIR
        )

        # Ensure output directories exist
        self.curated_output_dir.mkdir(parents=True, exist_ok=True)

        # Initialize stages
        self.transformer = BronzeToSilverProcessor(
            data_root=self.data_root,
            tracker=self.tracker,
            silver_db_path=self.silver_db_path,
        )

        self.harmonizer = EntityResolutionHarmonizer(
            data_root=self.data_root,
            tracker=self.tracker,
            input_dir=settings.PROCESSED_OUTPUT_DIR,
            output_dir=self.curated_output_dir,
        )

        self.loader = SilverToGoldLoader(
            tracker=self.tracker,
            silver_path=self.silver_db_path,
            gold_path=self.gold_db_path,
        )

        logger.info("DenuePipelineOrchestrator initialized")
        logger.info(f"   Silver DB: {self.silver_db_path}")
        logger.info(f"   Gold DB: {self.gold_db_path}")
        logger.info(f"   Metadata: {self.metadata_db}")
        logger.info(f"   Curated output: {self.curated_output_dir}")

    def run_transform_stage(self, force: bool = False) -> bool:
        """
        Run transform stage: ZIP -> Silver DuckDB.

        Extracts raw ZIP files, cleans and normalizes data,
        and loads it into the Silver DuckDB warehouse.

        Args:
            force: If True, reprocess all files regardless of status

        Returns:
            True if successful, False otherwise
        """
        logger.info("Starting TRANSFORM stage (Bronze -> Silver)")
        logger.info(f"   Mode: {'FORCE (reprocess all)' if force else 'INCREMENTAL'}")

        try:
            success = self.transformer.run(force=force)

            if success:
                logger.info("TRANSFORM stage completed successfully")
            else:
                logger.warning("TRANSFORM stage completed with errors")

            return success

        except Exception as e:
            logger.error(f"TRANSFORM stage failed: {e}")
            raise

    def run_harmonize_stage(self) -> bool:
        """
        Run harmonization stage: Entity Resolution.

        Matches establishments across time periods and generates
        master identifiers (CLEEs) for entity tracking.

        Returns:
            True if successful, False otherwise
        """
        logger.info("Starting HARMONIZE stage (Entity Resolution)")

        try:
            success = self.harmonizer.run()

            if success:
                logger.info("HARMONIZE stage completed successfully")
            else:
                logger.warning("HARMONIZE stage completed with errors")

            return success

        except Exception as e:
            logger.error(f"HARMONIZE stage failed: {e}")
            raise

    def run_load_stage(self, force: bool = False) -> bool:
        """
        Run load stage: Silver DuckDB -> Gold DuckDB.

        Promotes the Silver warehouse to Gold via file copy.

        Args:
            force: If True, copy even if Gold is up-to-date

        Returns:
            True if successful, False otherwise
        """
        logger.info("Starting LOAD stage (Silver -> Gold)")

        try:
            success = self.loader.run(force=force)

            if success:
                logger.info("LOAD stage completed successfully")
            else:
                logger.warning("LOAD stage failed")

            return success

        except Exception as e:
            logger.error(f"LOAD stage failed: {e}")
            raise

    def run_full_pipeline(self, force: bool = False) -> bool:
        """
        Run complete pipeline: Transform -> Load.

        Executes transform and load stages sequentially with full tracking.
        Harmonization is not included by default (run separately).

        Args:
            force: If True, force reprocess in transform stage

        Returns:
            True if all stages successful, False otherwise
        """
        logger.info("Starting FULL PIPELINE (Transform -> Load)")
        logger.info("=" * 60)

        # Stage 1: Transform (Bronze -> Silver)
        logger.info("STAGE 1/2: Transform")
        transform_success = self.run_transform_stage(force=force)

        if not transform_success:
            logger.error("Pipeline halted: Transform stage failed")
            return False

        logger.info("")
        logger.info("=" * 60)

        # Stage 2: Load (Silver -> Gold)
        logger.info("STAGE 2/2: Load")
        load_success = self.run_load_stage(force=force)

        logger.info("")
        logger.info("=" * 60)

        if transform_success and load_success:
            logger.info("FULL PIPELINE completed successfully!")
            logger.info(f"   Silver: {self.silver_db_path}")
            logger.info(f"   Gold: {self.gold_db_path}")
            logger.info(f"   Metadata: {self.metadata_db}")
            return True
        else:
            logger.warning("FULL PIPELINE completed with errors")
            return False

    def get_pipeline_status(self) -> dict:
        """
        Get current pipeline status and metrics.

        Returns:
            Dict with pipeline statistics
        """
        with self.tracker._get_connection() as conn:
            # Get latest runs by type
            latest_transform = conn.execute("""
                SELECT run_id, status, run_date, tasks_succeeded, tasks_failed
                FROM job_runs
                WHERE job_type IN ('TRANSFORM', 'PROCESS')
                ORDER BY run_date DESC
                LIMIT 1
            """).fetchone()

            latest_load = conn.execute("""
                SELECT run_id, status, run_date, tasks_succeeded, tasks_failed
                FROM job_runs
                WHERE job_type = 'LOAD'
                ORDER BY run_date DESC
                LIMIT 1
            """).fetchone()

            latest_harmonize = conn.execute("""
                SELECT run_id, status, run_date, tasks_succeeded, tasks_failed
                FROM job_runs
                WHERE job_type = 'HARMONIZE'
                ORDER BY run_date DESC
                LIMIT 1
            """).fetchone()

            # Get metrics
            process_metrics = self.tracker.get_process_metrics()
            harmonize_metrics = self.tracker.get_harmonize_metrics()

            def _run_dict(row):
                if row is None:
                    return {
                        "run_id": None,
                        "status": None,
                        "date": None,
                        "succeeded": 0,
                        "failed": 0,
                    }
                return {
                    "run_id": row[0],
                    "status": row[1],
                    "date": row[2],
                    "succeeded": row[3],
                    "failed": row[4],
                }

            return {
                "latest_transform_run": _run_dict(latest_transform),
                "latest_load_run": _run_dict(latest_load),
                "latest_harmonize_run": _run_dict(latest_harmonize),
                "process_metrics": process_metrics,
                "harmonize_metrics": harmonize_metrics,
                "metadata_db": str(self.metadata_db),
                "silver_db": str(self.silver_db_path),
                "gold_db": str(self.gold_db_path),
                "curated_output": str(self.curated_output_dir),
            }

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"DenuePipelineOrchestrator(data_root={self.data_root}, "
            f"silver={self.silver_db_path}, gold={self.gold_db_path})"
        )
