"""
Metadata tracking package for ETL job execution.

This package implements industry-standard metadata tracking patterns
used by orchestration frameworks like Apache Airflow and Prefect.

Provides:
- Job run lifecycle management
- Task instance tracking
- Data lineage recording
- Performance metrics
- Error tracking and classification
"""

from src_metadata.models import (
    JobRun,
    DownloadTask,
    DownloadResult,
    JobStatus,
    TaskStatus,
    ErrorType,
)
from src_metadata.tracker import MetadataTracker

__all__ = [
    "JobRun",
    "DownloadTask",
    "DownloadResult",
    "JobStatus",
    "TaskStatus",
    "ErrorType",
    "MetadataTracker",
]
