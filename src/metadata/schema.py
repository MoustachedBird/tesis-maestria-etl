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

-- ============================================================================
-- PROCESS TASKS: ZIP → Warehouse transformation tracking
-- ============================================================================
CREATE TABLE IF NOT EXISTS process_tasks (
    task_id VARCHAR(36) PRIMARY KEY,
    run_id VARCHAR(36) NOT NULL,
    
    -- Task identification
    task_name VARCHAR(200) NOT NULL,
    source_path VARCHAR NOT NULL,
    target_table VARCHAR(100),
    period VARCHAR(20),
    
    -- Execution tracking
    start_time TIMESTAMP,
    end_time TIMESTAMP,
    duration_seconds DOUBLE,
    status VARCHAR(20) NOT NULL,             -- PENDING, RUNNING, SUCCESS, FAILED, SKIPPED
    
    -- Processing metrics
    rows_extracted BIGINT,
    rows_loaded BIGINT,
    rows_rejected BIGINT,
    file_size_bytes BIGINT,
    
    -- Error handling
    error_type VARCHAR(50),
    error_message VARCHAR,
    
    -- Audit
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================================
-- HARMONIZE TASKS: Entity resolution tracking
-- ============================================================================
CREATE TABLE IF NOT EXISTS harmonize_tasks (
    task_id VARCHAR(36) PRIMARY KEY,
    run_id VARCHAR(36) NOT NULL,
    
    -- Task identification
    task_name VARCHAR(200) NOT NULL,
    region_group VARCHAR(100),              -- e.g., "NORTE", "SUR", or single state
    state_list VARCHAR,                     -- JSON array of states
    
    -- Execution tracking
    start_time TIMESTAMP,
    end_time TIMESTAMP,
    duration_seconds DOUBLE,
    status VARCHAR(20) NOT NULL,            -- PENDING, RUNNING, SUCCESS, FAILED, SKIPPED
    
    -- Harmonization metrics
    entities_input BIGINT,
    entities_matched BIGINT,
    entities_new BIGINT,
    master_clees_created BIGINT,
    periods_processed INTEGER,
    match_confidence_avg DOUBLE,
    
    -- Quality metrics
    spatial_matches INTEGER,
    identity_matches INTEGER,
    activity_matches INTEGER,
    
    -- Output
    output_path VARCHAR,
    
    -- Error handling
    error_type VARCHAR(50),
    error_message VARCHAR,
    
    -- Audit
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================================
-- PROCESS TASKS INDEXES
-- ============================================================================
CREATE INDEX IF NOT EXISTS idx_process_tasks_run_id ON process_tasks(run_id);
CREATE INDEX IF NOT EXISTS idx_process_tasks_status ON process_tasks(status);
CREATE INDEX IF NOT EXISTS idx_process_tasks_period ON process_tasks(period);
CREATE INDEX IF NOT EXISTS idx_process_tasks_source ON process_tasks(source_path);

-- ============================================================================
-- HARMONIZE TASKS INDEXES
-- ============================================================================
CREATE INDEX IF NOT EXISTS idx_harmonize_tasks_run_id ON harmonize_tasks(run_id);
CREATE INDEX IF NOT EXISTS idx_harmonize_tasks_status ON harmonize_tasks(status);
CREATE INDEX IF NOT EXISTS idx_harmonize_tasks_region ON harmonize_tasks(region_group);
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
    "get_pending_process_tasks": """
        SELECT 
            dt.target_path as source_path,
            dt.period,
            dt.task_id as download_task_id
        FROM download_tasks dt
        LEFT JOIN process_tasks pt ON dt.target_path = pt.source_path
        WHERE dt.status = 'SUCCESS'
          AND (pt.task_id IS NULL OR pt.status = 'FAILED')
        ORDER BY dt.period DESC, dt.created_at DESC
    """,
    "get_process_task_by_path": """
        SELECT task_id FROM process_tasks
        WHERE source_path = ?
        ORDER BY created_at DESC
        LIMIT 1
    """,
    "get_process_metrics": """
        SELECT 
            period,
            COUNT(*) as total_processed,
            SUM(rows_loaded) as total_rows,
            AVG(duration_seconds) as avg_duration_sec,
            AVG(rows_loaded) as avg_rows_loaded
        FROM process_tasks
        WHERE status = 'SUCCESS'
        AND period IS NOT NULL
        GROUP BY period
        ORDER BY period DESC
    """,
    "get_harmonize_metrics": """
        SELECT 
            region_group,
            COUNT(*) as total_harmonizations,
            SUM(entities_input) as total_entities,
            SUM(entities_matched) as total_matched,
            SUM(entities_new) as total_new,
            AVG(match_confidence_avg) as avg_confidence
        FROM harmonize_tasks
        WHERE status = 'SUCCESS'
        GROUP BY region_group
        ORDER BY region_group
    """,
}
