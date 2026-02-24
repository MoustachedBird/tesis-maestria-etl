"""
Unified DENUE Data Transformer

Orchestrates the complete ETL transformation pipeline:
1. PROCESS Stage: ZIP → Warehouse (Bronze → Silver)
2. HARMONIZE Stage: Entity Resolution (Silver → Gold)

Uses MetadataTracker for comprehensive tracking and observability,
following industry-standard patterns from Apache Airflow and Prefect.
"""

from pathlib import Path
from typing import Optional
import logging

from src_etl.core.config import settings
from src_metadata.tracker import MetadataTracker
from src_metadata.models import JobStatus
from src_etl.transformers.processor import ProcessorStage
from src_etl.transformers.harmonizer import HarmonizerStage

logger = logging.getLogger(__name__)


class DenueTransformer:
    """
    Unified transformer for DENUE data pipeline.

    Coordinates processing and harmonization stages with full
    metadata tracking and observability.

    Usage:
        transformer = DenueTransformer(data_root=Path("data"))

        # Run individual stages
        transformer.run_process_stage(force=False)
        transformer.run_harmonize_stage()

        # Or run complete pipeline
        transformer.run_full_pipeline(force=False)
    """

    def __init__(
        self,
        data_root: Path,
        metadata_db: Optional[Path] = None,
        processed_output_dir: Optional[Path] = None,
        curated_output_dir: Optional[Path] = None,
    ):
        """
        Initialize unified transformer.

        Args:
            data_root: Root data directory
            metadata_db: Path to metadata.duckdb (default: data/metadata.duckdb)
            processed_output_dir: Output for processed data (default: from settings)
            curated_output_dir: Output for curated data (default: from settings)
        """
        self.data_root = Path(data_root)

        # Setup metadata tracker
        if metadata_db is None:
            metadata_db = self.data_root / "metadata.duckdb"
        self.metadata_db = Path(metadata_db)
        self.tracker = MetadataTracker(self.metadata_db)

        # Output directories (use env vars or defaults)
        self.processed_output_dir = (
            Path(processed_output_dir)
            if processed_output_dir
            else settings.PROCESSED_OUTPUT_DIR
        )
        self.curated_output_dir = (
            Path(curated_output_dir)
            if curated_output_dir
            else settings.CURATED_OUTPUT_DIR
        )

        # Ensure output directories exist
        self.processed_output_dir.mkdir(parents=True, exist_ok=True)
        self.curated_output_dir.mkdir(parents=True, exist_ok=True)

        # Initialize stages
        self.processor = ProcessorStage(
            data_root=self.data_root,
            tracker=self.tracker,
            output_dir=self.processed_output_dir,
        )

        self.harmonizer = HarmonizerStage(
            data_root=self.data_root,
            tracker=self.tracker,
            input_dir=self.processed_output_dir,
            output_dir=self.curated_output_dir,
        )

        logger.info(f"✅ DenueTransformer initialized")
        logger.info(f"   📊 Metadata: {self.metadata_db}")
        logger.info(f"   📂 Processed output: {self.processed_output_dir}")
        logger.info(f"   📂 Curated output: {self.curated_output_dir}")

    def run_process_stage(self, force: bool = False) -> bool:
        """
        Run processing stage: ZIP → Warehouse.

        Extracts raw ZIP files, cleans and normalizes data,
        and loads it into the DuckDB warehouse.

        Args:
            force: If True, reprocess all files regardless of status

        Returns:
            True if successful, False otherwise
        """
        logger.info("🔧 Starting PROCESS stage (ZIP → Warehouse)")
        logger.info(f"   Mode: {'FORCE (reprocess all)' if force else 'INCREMENTAL'}")

        try:
            success = self.processor.run(force=force)

            if success:
                logger.info("✅ PROCESS stage completed successfully")
            else:
                logger.warning("⚠️ PROCESS stage completed with errors")

            return success

        except Exception as e:
            logger.error(f"❌ PROCESS stage failed: {e}")
            raise

    def run_harmonize_stage(self) -> bool:
        """
        Run harmonization stage: Entity Resolution.

        Matches establishments across time periods and generates
        master identifiers (CLEEs) for entity tracking.

        Returns:
            True if successful, False otherwise
        """
        logger.info("🔗 Starting HARMONIZE stage (Entity Resolution)")

        try:
            success = self.harmonizer.run()

            if success:
                logger.info("✅ HARMONIZE stage completed successfully")
            else:
                logger.warning("⚠️ HARMONIZE stage completed with errors")

            return success

        except Exception as e:
            logger.error(f"❌ HARMONIZE stage failed: {e}")
            raise

    def run_full_pipeline(self, force: bool = False) -> bool:
        """
        Run complete transformation pipeline: Process + Harmonize.

        Executes both stages sequentially with full tracking.

        Args:
            force: If True, reprocess all files in process stage

        Returns:
            True if both stages successful, False otherwise
        """
        logger.info("🚀 Starting FULL PIPELINE (Process + Harmonize)")
        logger.info("=" * 60)

        # Stage 1: Processing
        logger.info("📍 STAGE 1/2: Processing")
        process_success = self.run_process_stage(force=force)

        if not process_success:
            logger.error("❌ Pipeline halted: Process stage failed")
            return False

        logger.info("")
        logger.info("=" * 60)

        # Stage 2: Harmonization
        logger.info("📍 STAGE 2/2: Harmonization")
        harmonize_success = self.run_harmonize_stage()

        logger.info("")
        logger.info("=" * 60)

        if process_success and harmonize_success:
            logger.info("🎉 FULL PIPELINE completed successfully!")
            logger.info(f"   📊 Processed data: {self.processed_output_dir}")
            logger.info(f"   📊 Curated data: {self.curated_output_dir}")
            logger.info(f"   📊 Metadata: {self.metadata_db}")
            return True
        else:
            logger.warning("⚠️ FULL PIPELINE completed with errors")
            return False

    def get_pipeline_status(self) -> dict:
        """
        Get current pipeline status and metrics.

        Returns:
            Dict with pipeline statistics
        """
        with self.tracker._get_connection() as conn:
            # Get latest runs
            latest_process = conn.execute("""
                SELECT run_id, status, run_date, tasks_succeeded, tasks_failed
                FROM job_runs
                WHERE job_type = 'PROCESS'
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

            return {
                "latest_process_run": {
                    "run_id": latest_process[0] if latest_process else None,
                    "status": latest_process[1] if latest_process else None,
                    "date": latest_process[2] if latest_process else None,
                    "succeeded": latest_process[3] if latest_process else 0,
                    "failed": latest_process[4] if latest_process else 0,
                },
                "latest_harmonize_run": {
                    "run_id": latest_harmonize[0] if latest_harmonize else None,
                    "status": latest_harmonize[1] if latest_harmonize else None,
                    "date": latest_harmonize[2] if latest_harmonize else None,
                    "succeeded": latest_harmonize[3] if latest_harmonize else 0,
                    "failed": latest_harmonize[4] if latest_harmonize else 0,
                },
                "process_metrics": process_metrics,
                "harmonize_metrics": harmonize_metrics,
                "metadata_db": str(self.metadata_db),
                "processed_output": str(self.processed_output_dir),
                "curated_output": str(self.curated_output_dir),
            }

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"DenueTransformer("
            f"data_root={self.data_root}, "
            f"metadata_db={self.metadata_db})"
        )
