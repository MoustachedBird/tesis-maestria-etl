"""
Metadata tracker for job execution and task management.

Implements industry-standard patterns from Apache Airflow and Prefect
for tracking ETL job execution, task instances, and data lineage.
"""

import json
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from src.meta.models import (
    DownloadResult,
    ErrorType,
    JobStatus,
    TaskStatus,
)
from src.meta.schema import QUERIES, get_schema_ddl


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
            # Explicitly commit transaction to ensure data is written
            try:
                conn.commit()
            except Exception:
                # Ignore commit errors, may not have active transaction
                pass
        except Exception:
            # Rollback on error (only if transaction is active)
            try:
                conn.rollback()
            except Exception:
                # Ignore rollback errors
                pass
            raise
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
        config: dict[str, Any] | None = None,
    ) -> str:
        """
        Create new job run and return run_id.

        Args:
            job_name: Name of the job (e.g., "denue_downloader")
            job_type: Type of job: DOWNLOAD, PROCESS, CLEE_RESOLUTION
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
        status: JobStatus | None = None,
        error_summary: str | None = None,
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

    def get_run(self, run_id: str) -> dict[str, Any] | None:
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
        period: str | None = None,
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
        result: DownloadResult | None = None,
        # Individual parameters (alternative to result object)
        actual_size_bytes: int | None = None,
        actual_hash: str | None = None,
        download_speed_mbps: float | None = None,
        validation_passed: bool | None = None,
        error_type: ErrorType | None = None,
        error_message: str | None = None,
        duration_seconds: float | None = None,
        attempt_number: int | None = None,
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
                    "PASSED" if validation else ("FAILED" if validation is False else None),
                    attempts,
                    datetime.now(),
                    task_id,
                ],
            )

    def get_task(self, task_id: str) -> dict[str, Any] | None:
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

    def get_tasks_by_run(self, run_id: str) -> list[dict[str, Any]]:
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

    def get_successful_download_urls(self) -> set[str]:
        """
        Get the source URLs of every download that ever succeeded.

        Cruza TODOS los runs a proposito: el scraper la usa para saltarse
        lo ya descargado, y filtrar por el run en curso (que al momento
        del scrape esta vacio) hacia que cada corrida re-registrara desde
        disco los cientos de archivos ya presentes.

        Returns:
            Set of source_url strings with at least one SUCCESS task
        """
        with self._get_connection() as conn:
            results = conn.execute(
                """
                SELECT DISTINCT source_url FROM download_tasks
                WHERE status = 'SUCCESS' AND source_url IS NOT NULL
            """
            ).fetchall()
            return {row[0] for row in results}

    def get_failed_tasks(
        self, run_id: str | None = None, retryable_only: bool = True
    ) -> list[dict[str, Any]]:
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
        self, status: TaskStatus, process_status: str | None = None
    ) -> list[dict[str, Any]]:
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

    def get_run_summary(self, run_id: str) -> dict[str, Any]:
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

    def get_recent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
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

    def get_error_summary(self) -> list[dict[str, Any]]:
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

    def get_performance_metrics(self) -> list[dict[str, Any]]:
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
        bytes_processed: int | None = None,
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

    # ========================================================================
    # UTILITY METHODS
    # ========================================================================

    def fix_incomplete_run(self, run_id: str) -> bool:
        """
        Fix a run that failed to complete properly.

        This is useful when complete_run() failed due to transaction issues
        but all tasks were actually completed.

        Args:
            run_id: Job run identifier to fix

        Returns:
            bool: True if run was fixed, False if already complete
        """
        # Check if run needs fixing
        run = self.get_run(run_id)
        if not run:
            return False

        if run["status"] != "RUNNING":
            # Already completed
            return False

        # Force complete the run
        try:
            self.complete_run(run_id)
            return True
        except Exception:
            # If it still fails, try direct SQL update
            with self._get_connection() as conn:
                # Get task stats
                stats = conn.execute(
                    """
                    SELECT 
                        COUNT(*) as total,
                        SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) as succeeded,
                        SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) as failed
                    FROM download_tasks
                    WHERE run_id = ?
                    """,
                    [run_id],
                ).fetchone()

                # Direct update bypassing foreign key checks
                total, succeeded, failed = stats[0] or 0, stats[1] or 0, stats[2] or 0

                if total == 0:
                    final_status = "SUCCESS"
                elif failed == 0:
                    final_status = "SUCCESS"
                elif succeeded == 0:
                    final_status = "FAILED"
                else:
                    final_status = "PARTIAL"

                # Calculate duration
                run_start = run.get("start_time")
                if run_start:
                    from datetime import datetime as dt

                    duration = (dt.now() - run_start).total_seconds()
                else:
                    duration = 0

                # Flush any pending writes first
                conn.commit()

                # Update job run - this should work now that child records are committed
                conn.execute(
                    """
                    UPDATE job_runs 
                    SET status = ?, 
                        end_time = CURRENT_TIMESTAMP,
                        duration_seconds = ?,
                        total_tasks = ?,
                        tasks_succeeded = ?,
                        tasks_failed = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE run_id = ?
                    """,
                    [final_status, duration, total, succeeded, failed, run_id],
                )

                conn.commit()
                return True

    # ========================================================================
    # PROCESS TASK MANAGEMENT
    # ========================================================================

    def create_process_task(
        self,
        run_id: str,
        task_name: str,
        source_path: str,
        period: str | None = None,
        target_table: str | None = None,
    ) -> str:
        """
        Create new processing task.

        Args:
            run_id: Parent job run ID
            task_name: Human-readable task name
            source_path: Path to source ZIP file
            period: Optional data period (e.g., "2024_11")
            target_table: Target warehouse table name

        Returns:
            str: Unique task_id (UUID)
        """
        task_id = str(uuid.uuid4())

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO process_tasks (
                    task_id, run_id, task_name, source_path, target_table,
                    period, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                [
                    task_id,
                    run_id,
                    task_name,
                    source_path,
                    target_table,
                    period,
                    TaskStatus.PENDING.value,
                    datetime.now(),
                ],
            )

        return task_id

    def start_process_task(self, task_id: str):
        """
        Mark process task as running.

        Args:
            task_id: Task identifier
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE process_tasks SET
                    status = ?,
                    start_time = ?,
                    updated_at = ?
                WHERE task_id = ?
            """,
                [TaskStatus.RUNNING.value, datetime.now(), datetime.now(), task_id],
            )

    def complete_process_task(
        self,
        task_id: str,
        status: TaskStatus,
        rows_extracted: int | None = None,
        rows_loaded: int | None = None,
        rows_rejected: int | None = None,
        file_size_bytes: int | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ):
        """
        Complete process task with results.

        Args:
            task_id: Task identifier
            status: Final task status
            rows_extracted: Number of rows extracted from ZIP
            rows_loaded: Number of rows loaded to warehouse
            rows_rejected: Number of rows rejected (validation)
            file_size_bytes: Source file size
            error_type: Error classification if failed
            error_message: Error details if failed
        """
        end_time = datetime.now()

        with self._get_connection() as conn:
            # Get start time to calculate duration
            result = conn.execute(
                "SELECT start_time FROM process_tasks WHERE task_id = ?", [task_id]
            ).fetchone()

            duration = None
            if result and result[0]:
                start_time = datetime.fromisoformat(str(result[0]))
                duration = (end_time - start_time).total_seconds()

            conn.execute(
                """
                UPDATE process_tasks SET
                    end_time = ?,
                    duration_seconds = ?,
                    status = ?,
                    rows_extracted = ?,
                    rows_loaded = ?,
                    rows_rejected = ?,
                    file_size_bytes = ?,
                    error_type = ?,
                    error_message = ?,
                    updated_at = ?
                WHERE task_id = ?
            """,
                [
                    end_time,
                    duration,
                    status.value,
                    rows_extracted,
                    rows_loaded,
                    rows_rejected,
                    file_size_bytes,
                    error_type,
                    error_message,
                    datetime.now(),
                    task_id,
                ],
            )

    def get_pending_process_tasks(self, include_failed: bool = False) -> list[dict[str, Any]]:
        """
        Get list of files pending processing.

        Queries download_tasks joined with process_tasks to find
        successfully downloaded files that haven't been processed yet.

        Args:
            include_failed: Include previously failed tasks for retry

        Returns:
            List of dicts with keys: source_path, period, download_task_id
        """
        with self._get_connection() as conn:
            if include_failed:
                query = QUERIES["get_pending_process_tasks_with_failed"]
            else:
                query = QUERIES["get_pending_process_tasks"]

            result = conn.execute(query).fetchall()
            columns = ["source_path", "period", "download_task_id"]
            return [dict(zip(columns, row)) for row in result]

    def get_process_task_by_path(self, source_path: str) -> str | None:
        """
        Find existing process task for a file path.

        Args:
            source_path: Source file path to search for

        Returns:
            task_id if found, None otherwise
        """
        with self._get_connection() as conn:
            result = conn.execute(QUERIES["get_process_task_by_path"], [source_path]).fetchone()

            return result[0] if result else None

    # ========================================================================
    # CLEE RESOLUTION TASK MANAGEMENT
    # ========================================================================

    def create_clee_resolution_task(
        self,
        run_id: str,
        task_name: str,
        region_group: str,
        state_list: list[str],
    ) -> str:
        """
        Create new CLEE resolution task.

        Args:
            run_id: Parent job run ID
            task_name: Human-readable task name
            region_group: Region/state group identifier
            state_list: List of state codes in this group

        Returns:
            str: Unique task_id (UUID)
        """
        import json

        task_id = str(uuid.uuid4())
        state_list_json = json.dumps(state_list)

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO clee_resolution_tasks (
                    task_id, run_id, task_name, region_group, state_list,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                [
                    task_id,
                    run_id,
                    task_name,
                    region_group,
                    state_list_json,
                    TaskStatus.PENDING.value,
                    datetime.now(),
                ],
            )

        return task_id

    def start_clee_resolution_task(self, task_id: str):
        """
        Mark CLEE resolution task as running.

        Args:
            task_id: Task identifier
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE clee_resolution_tasks SET
                    status = ?,
                    start_time = ?,
                    updated_at = ?
                WHERE task_id = ?
            """,
                [TaskStatus.RUNNING.value, datetime.now(), datetime.now(), task_id],
            )

    def complete_clee_resolution_task(
        self,
        task_id: str,
        status: TaskStatus,
        entities_input: int | None = None,
        entities_matched: int | None = None,
        entities_new: int | None = None,
        master_clees_created: int | None = None,
        periods_processed: int | None = None,
        match_confidence_avg: float | None = None,
        spatial_matches: int | None = None,
        identity_matches: int | None = None,
        activity_matches: int | None = None,
        output_path: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ):
        """
        Complete CLEE resolution task with results.

        Args:
            task_id: Task identifier
            status: Final task status
            entities_input: Number of input entities
            entities_matched: Number of matched entities
            entities_new: Number of new entities discovered
            master_clees_created: Number of master CLEEs created
            periods_processed: Number of time periods processed
            match_confidence_avg: Average match confidence score
            spatial_matches: Number of spatial-based matches
            identity_matches: Number of identity-based matches
            activity_matches: Number of activity-based matches
            output_path: Path to output files
            error_type: Error classification if failed
            error_message: Error details if failed
        """
        end_time = datetime.now()

        with self._get_connection() as conn:
            # Get start time to calculate duration
            result = conn.execute(
                "SELECT start_time FROM clee_resolution_tasks WHERE task_id = ?", [task_id]
            ).fetchone()

            duration = None
            if result and result[0]:
                start_time = datetime.fromisoformat(str(result[0]))
                duration = (end_time - start_time).total_seconds()

            conn.execute(
                """
                UPDATE clee_resolution_tasks SET
                    end_time = ?,
                    duration_seconds = ?,
                    status = ?,
                    entities_input = ?,
                    entities_matched = ?,
                    entities_new = ?,
                    master_clees_created = ?,
                    periods_processed = ?,
                    match_confidence_avg = ?,
                    spatial_matches = ?,
                    identity_matches = ?,
                    activity_matches = ?,
                    output_path = ?,
                    error_type = ?,
                    error_message = ?,
                    updated_at = ?
                WHERE task_id = ?
            """,
                [
                    end_time,
                    duration,
                    status.value,
                    entities_input,
                    entities_matched,
                    entities_new,
                    master_clees_created,
                    periods_processed,
                    match_confidence_avg,
                    spatial_matches,
                    identity_matches,
                    activity_matches,
                    output_path,
                    error_type,
                    error_message,
                    datetime.now(),
                    task_id,
                ],
            )

    # ========================================================================
    # NORMALIZE TASK MANAGEMENT
    # ========================================================================

    def create_normalize_task(
        self,
        run_id: str,
        task_name: str,
        period: str,
    ) -> str:
        """
        Create new date normalization task.

        Args:
            run_id: Parent job run ID
            task_name: Human-readable task name
            period: Data period as DATE string (e.g., "2024-11-01")

        Returns:
            str: Unique task_id (UUID)
        """
        task_id = str(uuid.uuid4())

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO normalize_tasks (
                    task_id, run_id, task_name, period,
                    status, created_at
                ) VALUES (?, ?, ?, CAST(? AS DATE), ?, ?)
            """,
                [
                    task_id,
                    run_id,
                    task_name,
                    period,
                    TaskStatus.PENDING.value,
                    datetime.now(),
                ],
            )

        return task_id

    def start_normalize_task(self, task_id: str):
        """
        Mark normalize task as running.

        Args:
            task_id: Task identifier
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE normalize_tasks SET
                    status = ?,
                    start_time = ?,
                    updated_at = ?
                WHERE task_id = ?
            """,
                [TaskStatus.RUNNING.value, datetime.now(), datetime.now(), task_id],
            )

    def complete_normalize_task(
        self,
        task_id: str,
        status: TaskStatus,
        rows_total: int | None = None,
        rows_normalized: int | None = None,
        rows_unrecognized: int | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ):
        """
        Complete normalize task with results.

        Args:
            task_id: Task identifier
            status: Final task status
            rows_total: Total rows in this period
            rows_normalized: Rows successfully normalized
            rows_unrecognized: Rows with unrecognized fecha_alta_str format
            error_type: Error classification if failed
            error_message: Error details if failed
        """
        end_time = datetime.now()

        with self._get_connection() as conn:
            # Get start time to calculate duration
            result = conn.execute(
                "SELECT start_time FROM normalize_tasks WHERE task_id = ?", [task_id]
            ).fetchone()

            duration = None
            if result and result[0]:
                start_time = datetime.fromisoformat(str(result[0]))
                duration = (end_time - start_time).total_seconds()

            conn.execute(
                """
                UPDATE normalize_tasks SET
                    end_time = ?,
                    duration_seconds = ?,
                    status = ?,
                    rows_total = ?,
                    rows_normalized = ?,
                    rows_unrecognized = ?,
                    error_type = ?,
                    error_message = ?,
                    updated_at = ?
                WHERE task_id = ?
            """,
                [
                    end_time,
                    duration,
                    status.value,
                    rows_total,
                    rows_normalized,
                    rows_unrecognized,
                    error_type,
                    error_message,
                    datetime.now(),
                    task_id,
                ],
            )

    # ========================================================================
    # METRICS AND REPORTING
    # ========================================================================

    def get_process_metrics(self) -> list[dict[str, Any]]:
        """
        Get processing performance metrics by period.

        Returns:
            List of dicts with metrics per period
        """
        with self._get_connection() as conn:
            result = conn.execute(QUERIES["get_process_metrics"]).fetchall()
            columns = [
                "period",
                "total_processed",
                "total_rows",
                "avg_duration_sec",
                "avg_rows_loaded",
            ]
            return [dict(zip(columns, row)) for row in result]

    def get_clee_resolution_metrics(self) -> list[dict[str, Any]]:
        """
        Get CLEE resolution performance metrics by region.

        Returns:
            List of dicts with metrics per region
        """
        with self._get_connection() as conn:
            result = conn.execute(QUERIES["get_clee_resolution_metrics"]).fetchall()
            columns = [
                "region_group",
                "total_resolutions",
                "total_entities",
                "total_matched",
                "total_new",
                "avg_confidence",
            ]
            return [dict(zip(columns, row)) for row in result]

    def get_normalize_metrics(self) -> list[dict[str, Any]]:
        """
        Get date normalization metrics by period.

        Returns:
            List of dicts with metrics per period
        """
        with self._get_connection() as conn:
            result = conn.execute(QUERIES["get_normalize_metrics"]).fetchall()
            columns = [
                "period",
                "total_normalizations",
                "total_rows",
                "total_normalized",
                "total_unrecognized",
                "avg_duration_sec",
            ]
            return [dict(zip(columns, row)) for row in result]
