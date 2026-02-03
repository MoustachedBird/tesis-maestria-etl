"""
Database schema for metadata tracking.

This schema follows industry-standard patterns from Apache Airflow and Prefect,
adapted for DuckDB. Provides comprehensive tracking of job execution, task
instances, data lineage, and performance metrics.
"""

# DDL for metadata.duckdb
METADATA_SCHEMA_DDL = """
-- ============================================================================
-- JOB RUNS: Job-level execution tracking
-- ============================================================================
CREATE TABLE IF NOT EXISTS job_runs (
    run_id VARCHAR(36) PRIMARY KEY,
    job_name VARCHAR(100) NOT NULL,
    job_type VARCHAR(50) NOT NULL,           -- DOWNLOAD, PROCESS, HARMONIZE
    run_date TIMESTAMP NOT NULL,
    start_time TIMESTAMP,
    end_time TIMESTAMP,
    duration_seconds DOUBLE,
    status VARCHAR(20) NOT NULL,             -- RUNNING, SUCCESS, FAILED, PARTIAL
    trigger_type VARCHAR(20),                -- MANUAL, SCHEDULED, RETRY
    triggered_by VARCHAR(50),
    
    -- Aggregated metrics
    total_tasks INTEGER DEFAULT 0,
    tasks_succeeded INTEGER DEFAULT 0,
    tasks_failed INTEGER DEFAULT 0,
    tasks_skipped INTEGER DEFAULT 0,
    
    -- Resource usage
    total_bytes_downloaded BIGINT DEFAULT 0,
    avg_download_speed_mbps DOUBLE,
    
    -- Configuration snapshot (JSON string)
    config VARCHAR,
    
    -- Error summary
    error_summary VARCHAR,
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================================
-- DOWNLOAD TASKS: File-level tracking
-- ============================================================================
CREATE TABLE IF NOT EXISTS download_tasks (
    task_id VARCHAR(36) PRIMARY KEY,
    run_id VARCHAR(36) NOT NULL,
    
    -- Task identification
    task_name VARCHAR(200) NOT NULL,
    task_type VARCHAR(50) NOT NULL,          -- DOWNLOAD, VALIDATE, CLEANUP
    task_order INTEGER,
    
    -- File information
    source_url VARCHAR NOT NULL,
    target_path VARCHAR NOT NULL,
    period VARCHAR(20),
    
    -- Execution tracking
    start_time TIMESTAMP,
    end_time TIMESTAMP,
    duration_seconds DOUBLE,
    status VARCHAR(20) NOT NULL,             -- PENDING, RUNNING, SUCCESS, FAILED, SKIPPED
    
    -- Retry logic
    attempt_number INTEGER DEFAULT 1,
    max_retries INTEGER DEFAULT 5,
    retry_strategy VARCHAR(50),              -- EXPONENTIAL, FIXED, NONE
    next_retry_time TIMESTAMP,
    
    -- File validation
    file_size_bytes BIGINT,
    expected_size_bytes BIGINT,
    sha256_hash VARCHAR(64),
    expected_sha256 VARCHAR(64),
    validation_status VARCHAR(20),           -- PENDING, PASSED, FAILED
    validation_errors VARCHAR,
    
    -- Performance metrics
    download_speed_mbps DOUBLE,
    network_latency_ms INTEGER,
    
    -- Error handling
    error_type VARCHAR(50),                  -- NETWORK, HTTP_404, HTTP_500, TIMEOUT, VALIDATION, DISK_FULL
    error_message VARCHAR,
    error_traceback VARCHAR,
    is_retryable BOOLEAN DEFAULT TRUE,
    
    -- Audit
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    
    -- Note: Foreign key constraints removed due to DuckDB UPDATE limitations
    -- Referential integrity is maintained at application level
);

-- ============================================================================
-- DATA LINEAGE: Source-to-target tracking
-- ============================================================================
CREATE TABLE IF NOT EXISTS data_lineage (
    lineage_id VARCHAR(36) PRIMARY KEY,
    run_id VARCHAR(36),
    task_id VARCHAR(36),
    
    source_type VARCHAR(20) NOT NULL,        -- WEB, API, FILE, DATABASE
    source_location VARCHAR NOT NULL,
    source_checksum VARCHAR(64),
    
    target_type VARCHAR(20) NOT NULL,        -- FILE, TABLE, OBJECT_STORE
    target_location VARCHAR NOT NULL,
    target_checksum VARCHAR(64),
    
    transformation VARCHAR(100),             -- NONE, DOWNLOAD, EXTRACT, TRANSFORM
    
    rows_read BIGINT,
    rows_written BIGINT,
    bytes_processed BIGINT,
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    
    -- Note: Foreign key constraints removed due to DuckDB UPDATE limitations
    -- Referential integrity is maintained at application level
);

-- ============================================================================
-- PERFORMANCE INDEXES
-- ============================================================================

-- Job runs indexes
CREATE INDEX IF NOT EXISTS idx_job_runs_date ON job_runs(run_date DESC);
CREATE INDEX IF NOT EXISTS idx_job_runs_status ON job_runs(status);
CREATE INDEX IF NOT EXISTS idx_job_runs_name ON job_runs(job_name);
CREATE INDEX IF NOT EXISTS idx_job_runs_type ON job_runs(job_type);

-- Download tasks indexes
CREATE INDEX IF NOT EXISTS idx_tasks_run_id ON download_tasks(run_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON download_tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_type ON download_tasks(task_type);
CREATE INDEX IF NOT EXISTS idx_tasks_period ON download_tasks(period);
CREATE INDEX IF NOT EXISTS idx_tasks_error_type ON download_tasks(error_type);
CREATE INDEX IF NOT EXISTS idx_tasks_attempt ON download_tasks(attempt_number);

-- Data lineage indexes
CREATE INDEX IF NOT EXISTS idx_lineage_run_id ON data_lineage(run_id);
CREATE INDEX IF NOT EXISTS idx_lineage_task_id ON data_lineage(task_id);
CREATE INDEX IF NOT EXISTS idx_lineage_source_type ON data_lineage(source_type);
"""


def get_schema_ddl() -> str:
    """
    Get the complete DDL for creating metadata schema.

    Returns:
        str: SQL DDL statements
    """
    return METADATA_SCHEMA_DDL


# Common queries for metadata operations
QUERIES = {
    "get_run_summary": """
        SELECT 
            r.run_id,
            r.job_name,
            r.job_type,
            r.run_date,
            r.start_time,
            r.end_time,
            r.duration_seconds,
            r.status,
            r.trigger_type,
            r.triggered_by,
            r.total_tasks,
            r.tasks_succeeded,
            r.tasks_failed,
            r.tasks_skipped,
            r.total_bytes_downloaded,
            r.avg_download_speed_mbps,
            r.config,
            r.error_summary,
            r.created_at,
            r.updated_at,
            COUNT(t.task_id) as actual_total_tasks,
            SUM(CASE WHEN t.status = 'SUCCESS' THEN 1 ELSE 0 END) as actual_succeeded,
            SUM(CASE WHEN t.status = 'FAILED' THEN 1 ELSE 0 END) as actual_failed
        FROM job_runs r
        LEFT JOIN download_tasks t ON r.run_id = t.run_id
        WHERE r.run_id = ?
        GROUP BY r.run_id, r.job_name, r.job_type, r.run_date, r.start_time, r.end_time,
                 r.duration_seconds, r.status, r.trigger_type, r.triggered_by,
                 r.total_tasks, r.tasks_succeeded, r.tasks_failed, r.tasks_skipped,
                 r.total_bytes_downloaded, r.avg_download_speed_mbps, r.config,
                 r.error_summary, r.created_at, r.updated_at
    """,
    "get_failed_tasks": """
        SELECT * FROM download_tasks
        WHERE status = 'FAILED'
        AND is_retryable = TRUE
        AND attempt_number < max_retries
        ORDER BY created_at DESC
    """,
    "get_recent_runs": """
        SELECT * FROM job_runs
        ORDER BY run_date DESC
        LIMIT ?
    """,
    "get_error_summary": """
        SELECT 
            error_type,
            COUNT(*) as error_count,
            COUNT(DISTINCT run_id) as affected_runs
        FROM download_tasks
        WHERE status = 'FAILED'
        AND error_type IS NOT NULL
        GROUP BY error_type
        ORDER BY error_count DESC
    """,
    "get_performance_metrics": """
        SELECT 
            period,
            COUNT(*) as total_downloads,
            AVG(file_size_bytes) / 1024 / 1024 as avg_file_size_mb,
            AVG(download_speed_mbps) as avg_speed_mbps,
            AVG(duration_seconds) as avg_duration_sec
        FROM download_tasks
        WHERE status = 'SUCCESS'
        AND period IS NOT NULL
        GROUP BY period
        ORDER BY period DESC
    """,
}
