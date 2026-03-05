"""
Loader Stage: Silver -> Gold

Promotes the Silver DuckDB warehouse to Gold by copying the file.
This is the final stage of the ETL pipeline, producing the
production-ready dataset.
"""

import logging
import shutil
from pathlib import Path

from src_etl.core.config import settings
from src_metadata.models import JobStatus, TaskStatus
from src_metadata.tracker import MetadataTracker

logger = logging.getLogger(__name__)


class LoaderStage:
    """
    Promotes Silver DuckDB to Gold DuckDB via file copy.

    The Loader stage is intentionally simple: it copies the fully
    transformed Silver warehouse to the Gold location, making it
    available for the API and downstream consumers.

    Handles:
    - Validation that Silver DB exists and is non-empty
    - Atomic file copy to Gold location
    - MetadataTracker job/task tracking
    """

    def __init__(
        self,
        tracker: MetadataTracker,
        silver_path: Path | None = None,
        gold_path: Path | None = None,
    ):
        """
        Initialize loader stage.

        Args:
            tracker: MetadataTracker instance for job/task tracking
            silver_path: Path to Silver DuckDB (default: from settings)
            gold_path: Path to Gold DuckDB (default: from settings)
        """
        self.tracker = tracker

        if silver_path is not None:
            self.silver_path = Path(silver_path)
        else:
            self.silver_path = settings.SILVER_DUCKDB_PATH  # type: ignore[assignment]

        if gold_path is not None:
            self.gold_path = Path(gold_path)
        else:
            self.gold_path = settings.GOLD_DUCKDB_PATH  # type: ignore[assignment]

    def _validate_silver(self) -> bool:
        """
        Validate that the Silver DuckDB exists and has data.

        Returns:
            True if Silver is valid, False otherwise
        """
        if not self.silver_path.exists():
            logger.error(f"Silver DB not found: {self.silver_path}")
            return False

        if self.silver_path.stat().st_size == 0:
            logger.error(f"Silver DB is empty: {self.silver_path}")
            return False

        return True

    def _should_load(self, force: bool = False) -> bool:
        """
        Determine if Gold needs to be updated.

        Args:
            force: If True, always copy

        Returns:
            True if Gold should be updated
        """
        if force:
            return True

        if not self.gold_path.exists():
            return True

        # Copy if Silver is newer than Gold
        silver_mtime = self.silver_path.stat().st_mtime
        gold_mtime = self.gold_path.stat().st_mtime
        return silver_mtime > gold_mtime

    def run(self, force: bool = False) -> bool:
        """
        Run loader: copy Silver DuckDB to Gold.

        Args:
            force: If True, copy even if Gold is up-to-date

        Returns:
            True if successful, False otherwise
        """
        # Create job run
        run_id = self.tracker.create_run(
            job_name="denue_loader",
            job_type="LOAD",
            trigger_type="MANUAL",
        )

        # Validate Silver
        if not self._validate_silver():
            logger.error("Silver validation failed. Run 'transform' first.")
            self.tracker.complete_run(run_id, JobStatus.FAILED)
            return False

        # Check if load is needed
        if not self._should_load(force=force):
            logger.info("Gold is up-to-date. Nothing to load.")
            self.tracker.complete_run(run_id, JobStatus.SUCCESS)
            return True

        # Create load task
        task_id = self.tracker.create_process_task(
            run_id=run_id,
            task_name="copy_silver_to_gold",
            source_path=str(self.silver_path),
            period=None,
            target_table="gold_warehouse",
        )
        self.tracker.start_process_task(task_id)

        try:
            silver_size = self.silver_path.stat().st_size
            logger.info(f"Loading Silver -> Gold ({silver_size / 1024 / 1024:.1f} MB)")
            logger.info(f"   Source: {self.silver_path}")
            logger.info(f"   Target: {self.gold_path}")

            # Ensure Gold directory exists
            self.gold_path.parent.mkdir(parents=True, exist_ok=True)

            # Atomic copy: write to temp file, then rename
            temp_path = self.gold_path.with_suffix(".duckdb.tmp")
            shutil.copy2(self.silver_path, temp_path)
            temp_path.rename(self.gold_path)

            # Also copy WAL file if it exists
            silver_wal = self.silver_path.with_suffix(".duckdb.wal")
            if silver_wal.exists():
                gold_wal = self.gold_path.with_suffix(".duckdb.wal")
                shutil.copy2(silver_wal, gold_wal)

            logger.info("Gold warehouse updated successfully.")

            self.tracker.complete_process_task(
                task_id=task_id,
                status=TaskStatus.SUCCESS,
                rows_loaded=0,
                file_size_bytes=silver_size,
            )
            self.tracker.complete_run(run_id, JobStatus.SUCCESS)
            return True

        except Exception as e:
            logger.error(f"Load failed: {e}")
            self.tracker.complete_process_task(
                task_id=task_id,
                status=TaskStatus.FAILED,
                error_message=str(e),
            )
            self.tracker.complete_run(run_id, JobStatus.FAILED)
            return False
