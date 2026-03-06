"""
Pydantic models for metadata tracking.

These models provide type-safe representations of job runs, tasks,
and results. They are used throughout the ETL system for data validation
and serialization.
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class JobStatus(str, Enum):
    """Job execution status."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


class TaskStatus(str, Enum):
    """Task execution status."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class ErrorType(str, Enum):
    """Classification of errors that can occur during downloads."""

    NETWORK_ERROR = "NETWORK"
    HTTP_404 = "HTTP_404"
    HTTP_500 = "HTTP_500"
    HTTP_TIMEOUT = "TIMEOUT"
    VALIDATION_ERROR = "VALIDATION"
    DISK_FULL = "DISK_FULL"
    ZIP_CORRUPT = "ZIP_CORRUPT"
    HASH_MISMATCH = "HASH_MISMATCH"
    SIZE_MISMATCH = "SIZE_MISMATCH"
    UNKNOWN = "UNKNOWN"


class JobRun(BaseModel):
    """
    Represents a complete job execution run.

    A job run tracks the execution of an ETL job (e.g., downloader,
    processor, harmonizer) including all its tasks and aggregated metrics.
    """

    run_id: str = Field(..., description="Unique identifier for this run (UUID)")
    job_name: str = Field(..., description="Name of the job (e.g., 'denue_downloader')")
    job_type: str = Field(..., description="Type of job: DOWNLOAD, PROCESS, HARMONIZE")
    run_date: datetime = Field(..., description="Date/time when run was scheduled")
    start_time: datetime | None = Field(None, description="Actual start time")
    end_time: datetime | None = Field(None, description="Actual end time")
    duration_seconds: float | None = Field(None, description="Total execution time")
    status: JobStatus = Field(..., description="Current run status")
    trigger_type: str | None = Field(None, description="What triggered this run")
    triggered_by: str | None = Field(None, description="Who/what triggered this run")

    # Aggregated metrics
    total_tasks: int = Field(0, description="Total number of tasks")
    tasks_succeeded: int = Field(0, description="Number of successful tasks")
    tasks_failed: int = Field(0, description="Number of failed tasks")
    tasks_skipped: int = Field(0, description="Number of skipped tasks")

    # Resource metrics
    total_bytes_downloaded: int = Field(0, description="Total bytes downloaded")
    avg_download_speed_mbps: float | None = Field(None, description="Average download speed")

    # Additional context
    config: dict[str, Any] | None = Field(None, description="Run configuration")
    error_summary: str | None = Field(None, description="Summary of errors if any")

    model_config = ConfigDict(use_enum_values=True)


class DownloadTask(BaseModel):
    """
    Represents a single download task within a job run.

    Each task tracks the download of one file, including retry attempts,
    validation results, and performance metrics.
    """

    task_id: str = Field(..., description="Unique identifier for this task (UUID)")
    run_id: str = Field(..., description="Parent job run ID")
    task_name: str = Field(..., description="Human-readable task name")
    task_type: str = Field(..., description="Type of task: DOWNLOAD, VALIDATE, etc.")

    # File information
    source_url: str = Field(..., description="URL to download from")
    target_path: str = Field(..., description="Local file path destination")
    period: str | None = Field(None, description="Data period (e.g., '2024_11')")

    # Execution tracking
    status: TaskStatus = Field(..., description="Current task status")
    start_time: datetime | None = Field(None, description="Task start time")
    end_time: datetime | None = Field(None, description="Task end time")
    duration_seconds: float | None = Field(None, description="Execution duration")

    # Retry configuration
    attempt_number: int = Field(1, description="Current attempt number")
    max_retries: int = Field(5, description="Maximum retry attempts")
    retry_strategy: str | None = Field(None, description="Retry strategy used")
    next_retry_time: datetime | None = Field(None, description="Scheduled next retry")

    # File validation
    file_size_bytes: int | None = Field(None, description="Actual file size")
    expected_size_bytes: int | None = Field(None, description="Expected file size")
    sha256_hash: str | None = Field(None, description="Calculated SHA256 hash")
    expected_sha256: str | None = Field(None, description="Expected SHA256 hash")
    validation_status: str | None = Field(None, description="Validation result")
    validation_errors: str | None = Field(None, description="Validation error details")

    # Performance metrics
    download_speed_mbps: float | None = Field(None, description="Download speed")
    network_latency_ms: int | None = Field(None, description="Network latency")

    # Error handling
    error_type: ErrorType | None = Field(None, description="Error classification")
    error_message: str | None = Field(None, description="Error message")
    error_traceback: str | None = Field(None, description="Full error traceback")
    is_retryable: bool = Field(True, description="Whether task can be retried")

    model_config = ConfigDict(use_enum_values=True)


class DownloadResult(BaseModel):
    """
    Result object returned by download operations.

    Contains all information about a download attempt, including success
    status, file metrics, validation results, and any errors encountered.
    """

    success: bool = Field(..., description="Whether download succeeded")
    task_id: str = Field(..., description="Associated task ID")

    # File information
    file_path: str | None = Field(None, description="Path to downloaded file")
    file_size_bytes: int = Field(0, description="File size in bytes")
    sha256_hash: str | None = Field(None, description="Calculated file hash")

    # Performance metrics
    duration_seconds: float = Field(0, description="Download duration")
    download_speed_mbps: float = Field(0, description="Average download speed")

    # Retry tracking
    attempt_number: int = Field(1, description="Which attempt this was")

    # Error information
    error_type: ErrorType | None = Field(None, description="Error classification")
    error_message: str | None = Field(None, description="Error details")

    # Validation
    validation_passed: bool = Field(False, description="Whether validation passed")

    model_config = ConfigDict(use_enum_values=True)


class DataLineage(BaseModel):
    """
    Tracks the lineage of data from source to target.

    Records how data flows through the ETL pipeline, enabling
    impact analysis and data provenance tracking.
    """

    lineage_id: str = Field(..., description="Unique lineage identifier")
    run_id: str = Field(..., description="Associated job run")
    task_id: str | None = Field(None, description="Associated task")

    # Source information
    source_type: str = Field(..., description="Source type: WEB, API, FILE, DATABASE")
    source_location: str = Field(..., description="Source location/URL")
    source_checksum: str | None = Field(None, description="Source data checksum")

    # Target information
    target_type: str = Field(..., description="Target type: FILE, TABLE, OBJECT_STORE")
    target_location: str = Field(..., description="Target location/path")
    target_checksum: str | None = Field(None, description="Target data checksum")

    # Transformation
    transformation: str | None = Field(None, description="Transformation applied")

    # Metrics
    rows_read: int | None = Field(None, description="Number of rows read")
    rows_written: int | None = Field(None, description="Number of rows written")
    bytes_processed: int | None = Field(None, description="Bytes processed")

    created_at: datetime = Field(default_factory=datetime.now, description="Creation time")


class ProcessTask(BaseModel):
    """
    Represents a data processing task (ZIP → Warehouse).

    Tracks the transformation of raw ZIP files into the DuckDB warehouse,
    including data cleaning, normalization, and loading.
    """

    task_id: str = Field(..., description="Unique task identifier (UUID)")
    run_id: str = Field(..., description="Parent job run ID")
    task_name: str = Field(..., description="Human-readable task name")
    source_path: str = Field(..., description="Path to source ZIP file")
    target_table: str | None = Field(None, description="Target warehouse table")
    period: str | None = Field(None, description="Data period (e.g., '2024_11')")

    # Execution tracking
    start_time: datetime | None = Field(None, description="Task start time")
    end_time: datetime | None = Field(None, description="Task end time")
    duration_seconds: float | None = Field(None, description="Execution duration")
    status: TaskStatus = Field(..., description="Current task status")

    # Processing metrics
    rows_extracted: int | None = Field(None, description="Rows extracted from ZIP")
    rows_loaded: int | None = Field(None, description="Rows loaded to warehouse")
    rows_rejected: int | None = Field(None, description="Rows rejected (validation)")
    file_size_bytes: int | None = Field(None, description="Source file size")

    # Error handling
    error_type: str | None = Field(None, description="Error classification")
    error_message: str | None = Field(None, description="Error details")

    created_at: datetime = Field(default_factory=datetime.now, description="Creation time")
    updated_at: datetime = Field(default_factory=datetime.now, description="Last update time")

    model_config = ConfigDict(use_enum_values=True)


class HarmonizeTask(BaseModel):
    """
    Represents an entity harmonization task (Silver → Gold).

    Tracks entity resolution across temporal snapshots, matching
    establishments over time and generating master identifiers.
    """

    task_id: str = Field(..., description="Unique task identifier (UUID)")
    run_id: str = Field(..., description="Parent job run ID")
    task_name: str = Field(..., description="Human-readable task name")
    region_group: str = Field(..., description="Region/state group processed")
    state_list: str = Field(..., description="JSON array of states")

    # Execution tracking
    start_time: datetime | None = Field(None, description="Task start time")
    end_time: datetime | None = Field(None, description="Task end time")
    duration_seconds: float | None = Field(None, description="Execution duration")
    status: TaskStatus = Field(..., description="Current task status")

    # Harmonization metrics
    entities_input: int | None = Field(None, description="Input entities")
    entities_matched: int | None = Field(None, description="Matched entities")
    entities_new: int | None = Field(None, description="New entities discovered")
    master_clees_created: int | None = Field(None, description="Master CLEEs created")
    periods_processed: int | None = Field(None, description="Number of periods")
    match_confidence_avg: float | None = Field(None, description="Avg match confidence")

    # Quality metrics
    spatial_matches: int | None = Field(None, description="Spatial-based matches")
    identity_matches: int | None = Field(None, description="Identity-based matches")
    activity_matches: int | None = Field(None, description="Activity-based matches")

    # Output
    output_path: str | None = Field(None, description="Output file path")

    # Error handling
    error_type: str | None = Field(None, description="Error classification")
    error_message: str | None = Field(None, description="Error details")

    created_at: datetime = Field(default_factory=datetime.now, description="Creation time")
    updated_at: datetime = Field(default_factory=datetime.now, description="Last update time")

    model_config = ConfigDict(use_enum_values=True)


class NormalizeTask(BaseModel):
    """
    Represents a date normalization task (per period in Silver DuckDB).

    Tracks the UPDATE of fecha_alta_str -> fecha_alta_date for each
    period in the Silver warehouse.
    """

    task_id: str = Field(..., description="Unique task identifier (UUID)")
    run_id: str = Field(..., description="Parent job run ID")
    task_name: str = Field(..., description="Human-readable task name")
    period: str = Field(..., description="Data period as DATE (e.g., '2024-11-01')")

    # Execution tracking
    start_time: datetime | None = Field(None, description="Task start time")
    end_time: datetime | None = Field(None, description="Task end time")
    duration_seconds: float | None = Field(None, description="Execution duration")
    status: TaskStatus = Field(..., description="Current task status")

    # Normalization metrics
    rows_total: int | None = Field(None, description="Total rows in this period")
    rows_normalized: int | None = Field(None, description="Rows successfully normalized")
    rows_unrecognized: int | None = Field(None, description="Rows with unrecognized format")

    # Error handling
    error_type: str | None = Field(None, description="Error classification")
    error_message: str | None = Field(None, description="Error details")

    created_at: datetime = Field(default_factory=datetime.now, description="Creation time")
    updated_at: datetime = Field(default_factory=datetime.now, description="Last update time")

    model_config = ConfigDict(use_enum_values=True)


class RunSummary(BaseModel):
    """
    Summary statistics for a job run.

    Provides aggregated metrics and status information for dashboards
    and monitoring.
    """

    run_id: str
    job_name: str
    job_type: str
    status: JobStatus
    duration_seconds: float | None = None

    # Task statistics
    total_tasks: int = 0
    tasks_succeeded: int = 0
    tasks_failed: int = 0
    tasks_skipped: int = 0
    success_rate: float | None = None

    # Performance metrics
    total_bytes_downloaded: int = 0
    total_size_mb: float | None = None
    avg_download_speed_mbps: float | None = None

    # Time information
    start_time: datetime | None = None
    end_time: datetime | None = None

    model_config = ConfigDict(use_enum_values=True)

    @property
    def success_rate_pct(self) -> float:
        """Calculate success rate as percentage."""
        if self.total_tasks == 0:
            return 0.0
        return (self.tasks_succeeded / self.total_tasks) * 100
