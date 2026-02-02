"""
Metadata tracker for job execution and task management.

Implements industry-standard patterns from Apache Airflow and Prefect
for tracking ETL job execution, task instances, and data lineage.
"""

import uuid
import duckdb
import json
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

from src.metadata.models import (
    JobRun,
    DownloadTask,
    DownloadResult,
    JobStatus,
    TaskStatus,
    ErrorType,
    RunSummary,
)
from src.metadata.schema import get_schema_ddl, QUERIES


class MetadataTracker:
    """
    Manages job execution metadata following ETL industry standards.

    Provides:
    - Job run lifecycle management
    - Task instance tracking
    - Performance metrics collection
    - Error tracking and classification
    - Retry coordination
    - Data lineage recording

    Thread-safe for concurrent task execution.
    """

    def __init__(self, db_path: Path):
        """
        Initialize metadata tracker.

        Args:
            db_path: Path to metadata.duckdb file
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @contextmanager
    def _get_connection(self):
        """Get database connection with proper cleanup."""
        conn = duckdb.connect(str(self.db_path))
        try:
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self):
        """Create metadata tables if they don't exist."""
        with self._get_connection() as conn:
            conn.execute(get_schema_ddl())

    # ========================================================================
    # JOB RUN MANAGEMENT
    # ========================================================================

    def create_run(
        self,
        job_name: str,
        job_type: str = "DOWNLOAD",
        trigger_type: str = "MANUAL",
        triggered_by: str = "system",
        config: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Create new job run and return run_id.

        Args:
            job_name: Name of the job (e.g., "denue_downloader")
            job_type: Type of job: DOWNLOAD, PROCESS, HARMONIZE
            trigger_type: What triggered this run: MANUAL, SCHEDULED, RETRY
            triggered_by: Who/what triggered this run
            config: Optional configuration dictionary

        Returns:
            str: Unique run_id (UUID)

        Example:
            >>> tracker = MetadataTracker(Path("data/metadata.duckdb"))
            >>> run_id = tracker.create_run("denue_downloader", config={"max_retries": 5})
        """
        run_id = str(uuid.uuid4())
        run_date = datetime.now()

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO job_runs (
                    run_id, job_name, job_type, run_date, start_time,
                    status, trigger_type, triggered_by, config
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                [
                    run_id,
                    job_name,
                    job_type,
                    run_date,
                    run_date,
                    JobStatus.RUNNING.value,
                    trigger_type,
                    triggered_by,
                    json.dumps(config) if config else None,
                ],
            )

        return run_id

    def complete_run(
        self,
        run_id: str,
        status: Optional[JobStatus] = None,
        error_summary: Optional[str] = None,
    ):
        """
        Mark job run as complete and calculate aggregated metrics.

        Args:
            run_id: Job run identifier
            status: Final status (SUCCESS, FAILED, PARTIAL) - auto-determined if not provided
            error_summary: Optional error summary if failed
        """
        with self._get_connection() as conn:
            # Get task statistics
            stats = conn.execute(
                """
                SELECT 
                    COUNT(*) as total_tasks,
                    SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) as tasks_succeeded,
                    SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) as tasks_failed,
                    SUM(CASE WHEN status = 'SKIPPED' THEN 1 ELSE 0 END) as tasks_skipped,
                    SUM(COALESCE(file_size_bytes, 0)) as total_bytes,
                    AVG(COALESCE(download_speed_mbps, 0)) as avg_speed
                FROM download_tasks
                WHERE run_id = ?
            """,
                [run_id],
            ).fetchone()

            # Calculate duration
            run_info = conn.execute(
                """
                SELECT start_time FROM job_runs WHERE run_id = ?
            """,
                [run_id],
            ).fetchone()

            if run_info and run_info[0]:
                duration = (datetime.now() - run_info[0]).total_seconds()
            else:
                duration = 0

            # Auto-determine status if not provided
            if status is None:
                total_tasks = stats[0] or 0
                failed_tasks = stats[2] or 0
                succeeded_tasks = stats[1] or 0

                if total_tasks == 0:
                    status = JobStatus.SUCCESS  # No tasks = success
                elif failed_tasks == 0:
                    status = JobStatus.SUCCESS  # All succeeded
                elif succeeded_tasks == 0:
                    status = JobStatus.FAILED  # All failed
                else:
                    status = JobStatus.PARTIAL  # Mixed results

            # Update job run
            conn.execute(
                """
                UPDATE job_runs SET
                    end_time = ?,
                    duration_seconds = ?,
                    status = ?,
                    total_tasks = ?,
                    tasks_succeeded = ?,
                    tasks_failed = ?,
                    tasks_skipped = ?,
                    total_bytes_downloaded = ?,
                    avg_download_speed_mbps = ?,
                    error_summary = ?,
                    updated_at = ?
                WHERE run_id = ?
            """,
                [
                    datetime.now(),
                    duration,
                    status.value,
                    stats[0],
                    stats[1],
                    stats[2],
                    stats[3],
                    stats[4],
                    stats[5],
                    error_summary,
                    datetime.now(),
                    run_id,
                ],
            )

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """
        Get job run details.

        Args:
            run_id: Job run identifier

        Returns:
            Dict with run details or None if not found
        """
        with self._get_connection() as conn:
            result = conn.execute(
                """
                SELECT * FROM job_runs WHERE run_id = ?
            """,
                [run_id],
            ).fetchone()

            if result:
                columns = [desc[0] for desc in conn.description]
                return dict(zip(columns, result))
            return None

    # ========================================================================
    # TASK MANAGEMENT
    # ========================================================================

    def create_task(
        self,
        run_id: str,
        task_name: str,
        source_url: str,
        target_path: str,
        period: Optional[str] = None,
        max_retries: int = 5,
    ) -> str:
        """
        Create new download task.

        Args:
            run_id: Parent job run ID
            task_name: Human-readable task name
            source_url: URL to download from
            target_path: Local file path destination
            period: Optional data period (e.g., "2024_11")
            max_retries: Maximum retry attempts

        Returns:
            str: Unique task_id (UUID)
        """
        task_id = str(uuid.uuid4())

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO download_tasks (
                    task_id, run_id, task_name, task_type, source_url,
                    target_path, period, status, max_retries, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                [
                    task_id,
                    run_id,
                    task_name,
                    "DOWNLOAD",
                    source_url,
                    target_path,
                    period,
                    TaskStatus.PENDING.value,
                    max_retries,
                    datetime.now(),
                ],
            )

        return task_id

    def start_task(self, task_id: str, attempt_number: int = 1):
        """
        Mark task as running.

        Args:
            task_id: Task identifier
            attempt_number: Current attempt number (for retries)
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE download_tasks SET
                    status = ?,
                    start_time = ?,
                    attempt_number = ?,
                    updated_at = ?
                WHERE task_id = ?
            """,
                [
                    TaskStatus.RUNNING.value,
                    datetime.now(),
                    attempt_number,
                    datetime.now(),
                    task_id,
                ],
            )

    def complete_task(
        self,
        task_id: str,
        status: TaskStatus,
        result: Optional[DownloadResult] = None,
        # Individual parameters (alternative to result object)
        actual_size_bytes: Optional[int] = None,
        actual_hash: Optional[str] = None,
        download_speed_mbps: Optional[float] = None,
        validation_passed: Optional[bool] = None,
        error_type: Optional[ErrorType] = None,
        error_message: Optional[str] = None,
        duration_seconds: Optional[float] = None,
        attempt_number: Optional[int] = None,
    ):
        """
        Update task with completion results.

        Args:
            task_id: Task identifier
            status: Final status (SUCCESS or FAILED)
            result: DownloadResult with metrics (optional if individual params provided)
            actual_size_bytes: File size in bytes
            actual_hash: SHA256 hash
            download_speed_mbps: Download speed
            validation_passed: Whether validation passed
            error_type: Error classification
            error_message: Error details
            duration_seconds: Download duration
            attempt_number: Retry attempt number
        """
        # Use result object if provided, otherwise use individual parameters
        if result:
            file_size = result.file_size_bytes
            hash_value = result.sha256_hash
            speed = result.download_speed_mbps
            validation = result.validation_passed
            err_type = result.error_type
            err_msg = result.error_message
            duration = result.duration_seconds
            attempts = result.attempt_number
        else:
            file_size = actual_size_bytes or 0
            hash_value = actual_hash
            speed = download_speed_mbps or 0.0
            validation = validation_passed
            err_type = error_type
            err_msg = error_message
            duration = duration_seconds or 0.0
            attempts = attempt_number or 1

        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE download_tasks SET
                    status = ?,
                    end_time = ?,
                    duration_seconds = ?,
                    file_size_bytes = ?,
                    sha256_hash = ?,
                    download_speed_mbps = ?,
                    error_type = ?,
                    error_message = ?,
                    validation_status = ?,
                    attempt_number = ?,
                    updated_at = ?
                WHERE task_id = ?
            """,
                [
                    status.value,
                    datetime.now(),
                    duration,
                    file_size,
                    hash_value,
                    speed,
                    err_type.value if err_type else None,
                    err_msg,
                    "PASSED"
                    if validation
                    else ("FAILED" if validation is False else None),
                    attempts,
                    datetime.now(),
                    task_id,
                ],
            )

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        Get task details.

        Args:
            task_id: Task identifier

        Returns:
            Dict with task details or None if not found
        """
        with self._get_connection() as conn:
            result = conn.execute(
                """
                SELECT * FROM download_tasks WHERE task_id = ?
            """,
                [task_id],
            ).fetchone()

            if result:
                columns = [desc[0] for desc in conn.description]
                return dict(zip(columns, result))
            return None

    def get_tasks_by_run(self, run_id: str) -> List[Dict[str, Any]]:
        """
        Get all tasks for a job run.

        Args:
            run_id: Job run identifier

        Returns:
            List of task dictionaries
        """
        with self._get_connection() as conn:
            results = conn.execute(
                """
                SELECT * FROM download_tasks WHERE run_id = ? ORDER BY created_at
            """,
                [run_id],
            ).fetchall()

            if results:
                columns = [desc[0] for desc in conn.description]
                return [dict(zip(columns, row)) for row in results]
            return []

    def get_failed_tasks(
        self, run_id: Optional[str] = None, retryable_only: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Get failed tasks that can be retried.

        Args:
            run_id: Optional filter by specific run
            retryable_only: Only return tasks that can be retried

        Returns:
            List of failed task dictionaries
        """
        with self._get_connection() as conn:
            query = """
                SELECT * FROM download_tasks
                WHERE status = 'FAILED'
            """
            params = []

            if run_id:
                query += " AND run_id = ?"
                params.append(run_id)

            if retryable_only:
                query += " AND is_retryable = TRUE AND attempt_number < max_retries"

            query += " ORDER BY created_at DESC"

            results = conn.execute(query, params).fetchall()

            if results:
                columns = [desc[0] for desc in conn.description]
                return [dict(zip(columns, row)) for row in results]
            return []

    def get_tasks_by_status(
        self, status: TaskStatus, process_status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get tasks by download status and optional process status.

        This method provides compatibility with the old reporter interface
        for the processor to query which files need processing.

        Args:
            status: Download status to filter by
            process_status: Optional process status (for compatibility)

        Returns:
            List of task dictionaries
        """
        with self._get_connection() as conn:
            # For now, we just filter by download status
            # TODO: Add process_status tracking in future iteration
            query = """
                SELECT * FROM download_tasks
                WHERE status = ?
                ORDER BY created_at DESC
            """

            results = conn.execute(query, [status.value]).fetchall()

            if results:
                columns = [desc[0] for desc in conn.description]
                return [dict(zip(columns, row)) for row in results]
            return []

    # ========================================================================
    # QUERY & REPORTING
    # ========================================================================

    def get_run_summary(self, run_id: str) -> Dict[str, Any]:
        """
        Get comprehensive run statistics.

        Args:
            run_id: Job run identifier

        Returns:
            Dict with aggregated statistics
        """
        with self._get_connection() as conn:
            result = conn.execute(QUERIES["get_run_summary"], [run_id]).fetchone()

            if result:
                columns = [desc[0] for desc in conn.description]
                return dict(zip(columns, result))
            return {}

    def get_recent_runs(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Get recent job runs.

        Args:
            limit: Maximum number of runs to return

        Returns:
            List of run dictionaries
        """
        with self._get_connection() as conn:
            results = conn.execute(QUERIES["get_recent_runs"], [limit]).fetchall()

            if results:
                columns = [desc[0] for desc in conn.description]
                return [dict(zip(columns, row)) for row in results]
            return []

    def get_error_summary(self) -> List[Dict[str, Any]]:
        """
        Get summary of errors grouped by type.

        Returns:
            List of error statistics
        """
        with self._get_connection() as conn:
            results = conn.execute(QUERIES["get_error_summary"]).fetchall()

            if results:
                columns = [desc[0] for desc in conn.description]
                return [dict(zip(columns, row)) for row in results]
            return []

    def get_performance_metrics(self) -> List[Dict[str, Any]]:
        """
        Get performance metrics by period.

        Returns:
            List of performance statistics by period
        """
        with self._get_connection() as conn:
            results = conn.execute(QUERIES["get_performance_metrics"]).fetchall()

            if results:
                columns = [desc[0] for desc in conn.description]
                return [dict(zip(columns, row)) for row in results]
            return []

    # ========================================================================
    # DATA LINEAGE
    # ========================================================================

    def record_lineage(
        self,
        run_id: str,
        task_id: str,
        source_location: str,
        target_location: str,
        source_type: str = "WEB",
        target_type: str = "FILE",
        bytes_processed: Optional[int] = None,
    ):
        """
        Record data lineage for audit trail.

        Args:
            run_id: Job run identifier
            task_id: Task identifier
            source_location: Source URL/path
            target_location: Target file path
            source_type: Type of source (WEB, API, FILE, DATABASE)
            target_type: Type of target (FILE, TABLE, OBJECT_STORE)
            bytes_processed: Optional bytes processed
        """
        lineage_id = str(uuid.uuid4())

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO data_lineage (
                    lineage_id, run_id, task_id,
                    source_type, source_location,
                    target_type, target_location,
                    transformation, bytes_processed,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                [
                    lineage_id,
                    run_id,
                    task_id,
                    source_type,
                    source_location,
                    target_type,
                    target_location,
                    "DOWNLOAD",
                    bytes_processed,
                    datetime.now(),
                ],
            )
