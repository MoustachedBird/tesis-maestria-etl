# AGENTS.md - Development Guide for ETL Core

This file contains essential information for agentic coding agents working in this Python ETL project.

## Build Commands

### Environment Setup
```bash
# Install dependencies
uv sync

# Install specific dependency groups
uv sync --group etl
```

### Running the Application
```bash
# Main CLI entry point
uv run python -m src.main

# Development server (FastAPI)
uv run fastapi dev src/api/main.py

# Run ETL pipeline
uv run python -m src.main pipeline
```

### Testing
```bash
# Run all tests
uv run pytest

# Run single test file
uv run pytest tests/test_specific_file.py

# Run test with specific function/method
uv run pytest tests/test_file.py::test_function_name

# Run with verbose output
uv run pytest -v

# Run with coverage
uv run pytest --cov=src
```

### Linting & Code Quality
```bash
# Clean cache and compiled files
make clean

# Lint with ruff
uv run ruff check src/ tests/

# Auto-fix lint issues
uv run ruff check src/ tests/ --fix

# Format code with ruff
uv run ruff format src/ tests/
```

### Makefile Commands
```bash
# General
make help           # Show all available commands
make setup          # Install dependencies
make clean          # Clean cache and compiled files

# ETL Pipeline
make download       # Download INEGI data (incremental)
make download ARGS="--force"  # Force re-download all files
make process        # Process RAW → Warehouse
make process-parallel # Process in parallel mode
make normalize      # Normalize dates
make harmonize      # Run harmonization
make validate       # Validate warehouse
make pipeline       # Run complete pipeline

# Database Operations
make db-migrate     # Run pending Alembic migrations
make db-downgrade   # Rollback last migration
make db-seed        # Reload seed data (catalog tables)
make db-reset       # Full database reset (requires confirmation)
make db-status      # Show migration status and history

# Testing
make test           # Run all tests
make test-unit      # Run unit tests only
make test-integration # Run integration tests
make test-db        # Run database tests
make test-cov       # Run tests with coverage report
make test-verbose   # Run tests with verbose output
make test-fast      # Run fast tests only (unit, no slow)
```

## Project Architecture

### Structure
- **Language:** Python 3.12+
- **Package Manager:** uv (ultra-fast Python package manager)
- **Main Framework:** FastAPI (for API), Typer (for CLI)
- **Data Engine:** DuckDB (OLAP SQL) + Polars (DataFrames)
- **Storage:** Apache Parquet (partitioned by year)
- **Architecture Pattern:** Hexagonal/Clean Architecture with ETL layers

### Directory Layout
```
src/
├── core/           # Core configuration and settings
├── extractors/     # Data extraction modules (APIs, DBs, CSVs)
├── transformers/   # Business logic and data transformation
│   ├── denue_transformer.py   # Unified orchestrator
│   ├── processor_stage.py     # ZIP → Warehouse processing
│   ├── harmonizer_stage.py    # Entity resolution (Silver → Gold)
│   └── _legacy/               # Archived old processor/harmonizer
├── loaders/        # Data loading modules (Warehouse, S3)
├── metadata/       # ETL observability (Airflow-style tracking)
│   ├── schema.py   # DDL for metadata.duckdb
│   ├── models.py   # Pydantic models (JobRun, tasks, lineage)
│   └── tracker.py  # MetadataTracker class
├── utils/          # Shared utilities (loggers, connections)
├── api/            # FastAPI REST API (health checks, CRUD stubs)
│   ├── main.py     # FastAPI app with lifespan, CORS
│   └── schemas.py  # Pydantic request/response models
├── db/             # Database models, Alembic migrations, seed loading
│   ├── models.py   # SQLAlchemy ORM models
│   ├── database.py # Database engine and session management
│   ├── seed_loader.py # Seed data loading utilities
│   └── ...
└── main.py         # CLI entry point using Typer

data/
├── bronze/         # Raw data (never commit)
├── silver/         # Processed data (never commit)
├── gold/           # Final data warehouse (DuckDB) (never commit)
└── seeds/          # Seed data in Parquet format (commit these)
    ├── seed_entidades.parquet       # 32 states
    ├── seed_municipios.parquet      # 2,478 municipalities
    ├── seed_localidades.parquet     # 39,140 localities
    └── seed_actividades.parquet     # 1,127 economic activities

alembic/
└── versions/       # Database migration files
    ├── cbc954c59583_db_schema_to_match_denue_csv_files.py
    └── dff9d0804542_seed_catalog_tables_with_initial_data.py

docs/               # All project documentation
├── README.md       # Documentation index
├── AGENTS.md       # This file - development guide
├── DATABASE_SETUP.md # Database configuration
├── SEEDING.md      # Seed data loading guide
├── SEED_QUICKSTART.md # Quick reference for seeds
├── TRANSACTION_MANAGEMENT.md # Alembic best practices
└── ...
```

## Code Style Guidelines

### General Principles
- **Pythonic code:** Follow PEP 8 with modern Python practices
- **Type hints:** Use type annotations for all public functions and methods
- **Documentation:** Docstrings for all classes and public functions
- **Error handling:** Use proper exception handling with specific error types

### Import Organization
```python
# Standard library imports
import os
from pathlib import Path
from typing import Dict, List, Optional

# Third-party imports
import polars as pl
import duckdb
from pydantic import BaseModel
from fastapi import FastAPI

# Local imports (following the new structure)
from src.core.config import settings
from src.utils.database import BaseWarehouse
from src.transformers.denue_transformer import DenueTransformer
from src.loaders.denue_stores import DataStoreManager
from src.metadata.tracker import MetadataTracker
```

### Naming Conventions
- **Variables & functions:** `snake_case`
- **Classes:** `PascalCase` (CamelCase)
- **Constants:** `UPPER_SNAKE_CASE`
- **Private members:** Prefix with `_` (single underscore)
- **Files:** `snake_case.py`

### Class and Function Design
```python
class DataProcessor:
    """Process data using the established ETL pipeline."""
    
    def __init__(self, config: dict) -> None:
        self.config = config
    
    def process_data(self, input_path: Path) -> pl.DataFrame:
        """
        Process the input data and return cleaned DataFrame.
        
        Args:
            input_path: Path to input data file
            
        Returns:
            Cleaned Polars DataFrame
            
        Raises:
            FileNotFoundError: If input file doesn't exist
            ProcessingError: If data processing fails
        """
        pass
```

### Configuration Management
- Use `pydantic-settings` for configuration (see `src/core/config.py`)
- Environment variables defined in `.env` files
- Use `settings` singleton instance throughout the codebase
- Separate production/development configurations properly

### Data Processing Patterns
- **Polars for DataFrames:** Use Polars instead of pandas for performance
- **Type safety:** Use Polars data types with explicit schemas
- **Lazy evaluation:** Prefer Polars lazy API for large datasets
- **Memory efficiency:** Use streaming/chunked processing for large files

### Database Patterns
- **DuckDB for analytics:** Use DuckDB for analytical queries and data warehousing
- **SQLAlchemy for ORM:** Use SQLAlchemy 2.0 for database models and migrations
- **Alembic for migrations:** All schema changes through versioned migrations
- **Seed data via migrations:** Catalog data loaded automatically via Alembic (see `docs/SEEDING.md`)
- **Connection management:** Use context managers for database connections
- **Query optimization:** Write SQL that works well with DuckDB's columnar storage
- **Transaction management:** NEVER call `commit()` or `rollback()` in Alembic migrations - let Alembic handle transactions (see `docs/TRANSACTION_MANAGEMENT.md`)

### Error Handling
```python
# Specific exception handling
try:
    result = processor.process_data(input_path)
except FileNotFoundError as e:
    logger.error(f"Input file not found: {e}")
    raise
except ProcessingError as e:
    logger.error(f"Processing failed: {e}")
    # Retry logic or cleanup here
    raise
except Exception as e:
    logger.error(f"Unexpected error: {e}")
    raise ProcessingError(f"Failed to process {input_path}") from e
```

### Logging
- Use structured logging with proper levels
- Include context in log messages (file paths, record counts, timing)
- Use `rich` console library for CLI output with formatting
- Log files should go to `logs/` directory

### Testing Guidelines
- Use `pytest` for testing framework
- Write unit tests for core business logic
- Integration tests for database operations and ETL pipelines
- Mock external services (APIs, web scraping)
- Use fixtures for test data setup

### CLI Development
- Use `typer` for CLI commands
- Provide help text for all commands and options
- Use `rich` for beautiful terminal output
- Follow the command pattern established in `src/main.py`

### Performance Considerations
- Use Polars for data manipulation (avoid pandas)
- Leverage DuckDB's analytical capabilities
- Implement parallel processing where beneficial
- Use streaming/chunked processing for large files
- Profile and optimize bottlenecks in data pipelines

### Security Best Practices
- Never commit secrets (use `.env` files)
- Validate all external data inputs
- Use parameterized queries for database operations
- Sanitize file paths and user inputs
- Follow principle of least privilege for database access

## Database Operations

### Initial Database Setup
```bash
# Run all migrations (creates schema + loads seed data)
alembic upgrade head

# OR use Makefile
make db-migrate
```

This will:
- Create all database tables (DENUE schema with 5 tables)
- Load 42,777 catalog records from Parquet seed files:
  - 32 states (entidades)
  - 2,478 municipalities (municipios)
  - 39,140 localities (localidades)
  - 1,127 economic activities (actividades económicas)

### Database Commands
```bash
# Check current migration state
make db-status
alembic current

# Reload seed data (idempotent)
make db-seed

# Rollback last migration
alembic downgrade -1
make db-downgrade

# Full database reset (DESTRUCTIVE)
make db-reset

# View migration history
alembic history
```

### Working with Migrations

**Creating New Migrations:**
```bash
# Auto-generate migration from model changes
alembic revision --autogenerate -m "description of changes"

# Create empty migration (for data operations)
alembic revision -m "description"
```

**Migration Best Practices:**
- ✅ Use Alembic's autogenerate for schema changes
- ✅ Let Alembic manage transactions (never call `commit()` in migrations)
- ✅ Test both `upgrade()` and `downgrade()` functions
- ✅ Keep migrations focused and single-purpose
- ❌ Never modify existing migrations (create new ones instead)
- ❌ Never call `connection.commit()` or `connection.rollback()` in migrations

See `docs/TRANSACTION_MANAGEMENT.md` for detailed guidance.

### Seed Data Management

Seed data is stored as Parquet files in `data/seeds/` and loaded automatically during migrations.

**Updating Seed Data:**
```python
import polars as pl

# Generate new seed data
df = pl.DataFrame({
    "cve_ent": ["01", "02", ...],
    "entidad": ["Aguascalientes", "Baja California", ...]
})

# Save as Parquet
df.write_parquet("data/seeds/seed_entidades.parquet")

# Reload seeds
# make db-seed
```

**Accessing Seed Data:**
```python
from src.core.config import settings
from src.db.seed_loader import validate_seed_data, seed_catalog_tables

# Validate seed files
validate_seed_data(settings.SEEDS_DIR)

# In migration context (automatic)
stats = seed_catalog_tables(connection, settings.SEEDS_DIR)
```

See `docs/SEEDING.md` for comprehensive guide and `docs/SEED_QUICKSTART.md` for quick reference.

## Development Workflow

1. **Setup:** `make setup` or `uv sync`
2. **Database:** `make db-migrate` to setup database and load seeds
3. **Development:** Write code in `src/` following the patterns above
4. **Testing:** `uv run pytest` to run tests
5. **Code Quality:** Run linting and type checking if configured
6. **Documentation:** Update docstrings and comments as needed

## Documentation

All project documentation is located in the `docs/` directory:

- **docs/README.md** - Documentation index and navigation guide
- **docs/AGENTS.md** - This file - development guide for agents
- **docs/DATABASE_SETUP.md** - Database configuration (DuckDB/PostgreSQL)
- **docs/DATABASE_MIGRATION_SUMMARY.md** - Migration system overview
- **docs/SEEDING.md** - Complete seed data loading guide
- **docs/SEED_QUICKSTART.md** - Quick reference for seed operations
- **docs/TRANSACTION_MANAGEMENT.md** - Alembic transaction best practices
- **docs/TESTING_SETUP.md** - Testing framework and guidelines
- **docs/ENV_VARIABLES_UPDATE.md** - Environment variables reference
- **docs/GITIGNORE_UPDATE.md** - Git ignore patterns explained

**Quick Links:**
- New to the project? Start with `docs/README.md`
- Database work? See `docs/DATABASE_SETUP.md` and `docs/SEEDING.md`
- Writing migrations? Read `docs/TRANSACTION_MANAGEMENT.md` first
- Writing tests? Check `docs/TESTING_SETUP.md`

## Important Notes

- **Data files** in `data/` directories are never committed (see `.gitignore`)
  - Exception: `data/seeds/*.parquet` ARE committed (catalog seed data)
- **Local databases** (`*.duckdb`, `*.db`) are excluded from version control
- **Environment variables** should be managed through `.env` files
- **Dependencies** are managed through `uv` with `pyproject.toml`
- **Lock file** (`uv.lock`) should be committed for reproducibility
- **Seed files** in `data/seeds/` should be committed (Parquet format, ~700KB total)

## Common Pitfalls to Avoid

### General Development
- ❌ Don't use pandas - prefer Polars for performance
- ❌ Don't hardcode paths - use the settings configuration
- ❌ Don't ignore type hints - they're required for maintainability
- ❌ Don't commit data files or local databases (except seed files)
- ❌ Don't use print() for logging - use proper logging utilities
- ❌ Don't forget to handle exceptions properly throughout the pipeline

### Database & Migrations
- ❌ **NEVER** call `connection.commit()` or `connection.rollback()` in Alembic migrations
- ❌ Don't modify existing migrations - create new ones instead
- ❌ Don't skip testing both `upgrade()` and `downgrade()` functions
- ❌ Don't forget to update seed files when catalog data changes
- ❌ Don't hardcode database credentials - use environment variables

### Seed Data
- ✅ **DO** commit seed files (`data/seeds/*.parquet`)
- ❌ Don't manually edit seed files - regenerate from source data
- ❌ Don't load seed data outside of migrations - use `make db-seed`
- ❌ Don't forget to validate seed files before committing

See `docs/TRANSACTION_MANAGEMENT.md` for critical information about Alembic transaction handling.

---

## ETL Download System Architecture (Industry Standards)

### Overview

The DENUE downloader has been refactored to follow **industry-standard ETL patterns** used by Apache Airflow, Prefect, Netflix, and Uber. This implementation provides enterprise-grade tracking, observability, and reliability.

### Architecture Pattern: Metadata-Driven ETL

```
┌─────────────────────────────────────────────────────────────┐
│                    METADATA LAYER                           │
│              data/metadata.duckdb                           │
│  ┌──────────────┬──────────────┬──────────────┐            │
│  │  job_runs    │ download_tasks│ data_lineage │            │
│  │  (run-level) │ (file-level)  │ (provenance) │            │
│  └──────────────┴──────────────┴──────────────┘            │
└─────────────────────────────────────────────────────────────┘
                          ↕
┌─────────────────────────────────────────────────────────────┐
│                   EXECUTION LAYER                           │
│  DenueDownloader → Downloader → FileValidator               │
│  (orchestrator)    (base class) (validation)                │
└─────────────────────────────────────────────────────────────┘
                          ↕
┌─────────────────────────────────────────────────────────────┐
│                    STORAGE LAYER                            │
│  data/bronze/denue/{period}/*.zip                           │
└─────────────────────────────────────────────────────────────┘
                          ↕
┌─────────────────────────────────────────────────────────────┐
│                    LOGGING LAYER                            │
│  logs/downloads/YYYY-MM-DD/run_TIMESTAMP/                   │
│  ├── structured.jsonl     (machine-readable)                │
│  ├── application.log      (human-readable)                  │
│  ├── errors.log           (errors only)                     │
│  └── summary.json         (run statistics)                  │
└─────────────────────────────────────────────────────────────┘
```

### New Package Structure

```
src/
├── metadata/                    # NEW: Industry-standard metadata tracking
│   ├── __init__.py
│   ├── schema.py               # DDL for metadata.duckdb
│   ├── models.py               # Pydantic models (JobRun, DownloadTask, etc.)
│   └── tracker.py              # MetadataTracker class
│
├── extractors/
│   ├── download_config.py      # NEW: Configuration dataclass
│   ├── file_validator.py       # NEW: Multi-level validation
│   ├── downloader.py           # NEW: Generic base class with retry logic
│   └── denue_downloader.py     # REFACTORED: Orchestrator with parallel downloads
│
└── core/
    └── logging.py              # NEW: Structured logging + Rich progress bars
```

### Key Components

#### 1. Metadata Tracker (`src/metadata/tracker.py`)

**Purpose**: Centralized tracking of job execution and task instances.

**Key Methods**:
```python
tracker = MetadataTracker(Path("data/metadata.duckdb"))

# Job run lifecycle
run_id = tracker.create_run("denue_downloader", config={...})
tracker.complete_run(run_id, JobStatus.SUCCESS)

# Task management
task_id = tracker.create_task(run_id, "download_comercio_2024_11", url, path)
tracker.start_task(task_id)
tracker.complete_task(task_id, TaskStatus.SUCCESS, result)

# Queries
failed_tasks = tracker.get_failed_tasks(retryable_only=True)
summary = tracker.get_run_summary(run_id)
metrics = tracker.get_performance_metrics()
```

**Database Schema**: See `src/metadata/schema.py` for complete DDL.

#### 2. File Validator (`src/extractors/file_validator.py`)

**Multi-Level Validation**:
1. ✅ File exists
2. ✅ Minimum size check (10KB default)
3. ✅ Expected size comparison (from Content-Length header)
4. ✅ SHA256 hash verification
5. ✅ ZIP integrity test

**Usage**:
```python
from src.extractors.file_validator import FileValidator

validation = FileValidator.validate(
    file_path=Path("data/bronze/denue/2024_11/comercio.zip"),
    expected_size=52428800,
    expected_hash="abc123...",
    min_size_kb=10,
    check_zip=True
)

if validation.is_valid:
    print(f"✅ Valid file: {validation.sha256_hash}")
else:
    print(f"❌ Validation failed: {validation.error_message}")
```

#### 3. Generic Downloader (`src/extractors/downloader.py`)

**Features**:
- Exponential backoff retry (2s, 4s, 8s, 16s, 32s, max 60s)
- Streaming downloads with progress callbacks
- Automatic file validation after download
- Error classification (NETWORK, HTTP_404, HTTP_500, TIMEOUT, VALIDATION)
- Cleanup of failed downloads

**Retry Logic** (using `tenacity` library):
```python
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    retry=retry_if_exception_type((ConnectionError, Timeout))
)
def download_with_retry(url, dest):
    # Download logic with automatic retries
    pass
```

#### 4. DENUE Downloader Orchestrator (`src/extractors/denue_downloader.py`)

**Responsibilities**:
- Scrape INEGI website for download URLs
- Create job run and task instances in metadata
- Execute parallel downloads (max 3 concurrent)
- Track execution with Rich progress bars
- Generate comprehensive run summaries
- Handle retries with exponential backoff

**Usage**:
```python
from pathlib import Path
from src.extractors.denue_downloader import DenueDownloader
from src.extractors.download_config import DownloadConfig

config = DownloadConfig(
    max_retries=5,
    validate_sha256=True,
    enable_progress_bars=True,
    max_concurrent_downloads=3
)

downloader = DenueDownloader(
    output_dir=Path("data/bronze/denue"),
    metadata_db=Path("data/metadata.duckdb"),
    config=config,
    headless=True
)

success = downloader.run()
```

#### 5. Structured Logging (`src/core/logging.py`)

**Three-Tier Logging**:
1. **Structured JSON** (`structured.jsonl`) - Machine-parseable events
2. **Human-readable** (`application.log`) - Formatted log messages
3. **Error-only** (`errors.log`) - Failed operations with stack traces

**Log Directory Structure**:
```
logs/downloads/
├── 2026-01-29/
│   ├── run_20260129_143045/
│   │   ├── structured.jsonl
│   │   ├── application.log
│   │   ├── errors.log
│   │   └── summary.json
│   └── run_20260129_150122/
│       └── ...
└── latest -> 2026-01-29/run_20260129_150122/
```

**Usage**:
```python
from src.core.logging import StructuredLogger

log_system = StructuredLogger(
    run_id=run_id,
    job_name="denue_downloader",
    log_dir=Path("logs/downloads")
)

# Get loggers
logger = log_system.get_logger(__name__)
struct_logger = log_system.get_structured_logger()

# Log events
logger.info("Processing file...")
struct_logger.info("download_started", file="comercio.zip", size_mb=50.5)
```

### Configuration Management

All download parameters are externalized in `DownloadConfig`:

```python
from src.extractors.download_config import DownloadConfig

config = DownloadConfig(
    # Retry configuration
    max_retries=5,
    retry_strategy="exponential",
    retry_base_delay=2,
    retry_max_delay=60,
    
    # Network configuration
    timeout_seconds=60,
    chunk_size_bytes=8192,
    max_concurrent_downloads=3,
    
    # Validation configuration
    validate_size=True,
    validate_sha256=True,
    validate_zip_integrity=True,
    min_file_size_kb=10,
    
    # Logging configuration
    enable_progress_bars=True,
    log_level="INFO"
)
```

### CLI Commands

**Download with options**:
```bash
# Download with custom configuration
python -m src.main download-inegi \
    --max-retries 5 \
    --validate-hash \
    --show-progress \
    --parallel \
    --max-concurrent 3

# Retry failed downloads
python -m src.main retry-downloads

# Show download statistics
python -m src.main download-stats --last-n 10
```

### Metadata Queries

**Common queries** in `metadata.duckdb`:

```sql
-- Get recent job runs
SELECT * FROM job_runs 
ORDER BY run_date DESC 
LIMIT 10;

-- Get failed downloads that can be retried
SELECT * FROM download_tasks 
WHERE status = 'FAILED' 
  AND is_retryable = TRUE 
  AND attempt_number < max_retries;

-- Get performance metrics by period
SELECT 
    period,
    COUNT(*) as total_downloads,
    AVG(file_size_bytes) / 1024 / 1024 as avg_size_mb,
    AVG(download_speed_mbps) as avg_speed_mbps,
    AVG(duration_seconds) as avg_duration_sec
FROM download_tasks
WHERE status = 'SUCCESS'
GROUP BY period
ORDER BY period DESC;

-- Get error summary
SELECT 
    error_type,
    COUNT(*) as error_count,
    COUNT(DISTINCT run_id) as affected_runs
FROM download_tasks
WHERE status = 'FAILED'
GROUP BY error_type
ORDER BY error_count DESC;
```

### Migration from Old System

**Migration Script**: `scripts/migrate_reports_to_metadata.py`

```bash
# Migrate historical data from reports.db to metadata.duckdb
python scripts/migrate_reports_to_metadata.py

# Verify migration
python -c "
import duckdb
conn = duckdb.connect('data/metadata.duckdb')
print('Tasks migrated:', conn.execute('SELECT COUNT(*) FROM download_tasks').fetchone()[0])
"
```

**Old vs New**:

| Aspect | Old (reports.db) | New (metadata.duckdb) |
|--------|------------------|----------------------|
| **Tracking** | Basic status | Job runs + task instances |
| **Metrics** | None | Duration, speed, size |
| **Retries** | 1 attempt | 5 attempts with backoff |
| **Validation** | None | SHA256 + size + ZIP |
| **Logging** | DuckDB only | DuckDB + structured JSON logs |
| **Queries** | Limited | Rich analytics |
| **Standard** | Custom | Airflow/Prefect pattern |

### Testing

**Unit Tests**:
```bash
# Test file validator
pytest tests/unit/test_file_validator.py -v

# Test downloader with mocked HTTP
pytest tests/unit/test_downloader.py -v

# Test metadata tracker
pytest tests/unit/test_metadata_tracker.py -v
```

**Integration Tests**:
```bash
# Test full download workflow
pytest tests/integration/test_denue_downloader.py -v
```

**E2E Tests**:
```bash
# Test complete pipeline (slow)
pytest tests/e2e/test_full_pipeline.py -v --slow
```

### Performance Characteristics

**Parallel Downloads**:
- Max 3 concurrent downloads (configurable)
- ThreadPoolExecutor for I/O-bound operations
- Rich progress bars track all downloads simultaneously

**Retry Strategy**:
- Exponential backoff: 2s, 4s, 8s, 16s, 32s (capped at 60s)
- Max 5 retry attempts (configurable)
- Only retries transient errors (not 404, validation failures)

**Validation Overhead**:
- SHA256 calculation: ~50-100 MB/s (depends on disk speed)
- ZIP integrity test: O(n) where n = number of files in archive
- Minimal performance impact (<5% overhead)

### Error Handling

**Error Classification** (see `src/metadata/models.py`):
```python
class ErrorType(str, Enum):
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
```

**Retryable Errors**:
- ✅ NETWORK_ERROR (connection issues)
- ✅ HTTP_500 (server errors)
- ✅ HTTP_TIMEOUT (timeout)
- ❌ HTTP_404 (not found - permanent)
- ❌ VALIDATION_ERROR (corrupt file - permanent)

### Best Practices

#### When Writing Download Logic

```python
# ✅ DO: Use the generic Downloader base class
from src.extractors.downloader import Downloader

downloader = Downloader(config)
result = downloader.download(url, dest, task_id)

# ❌ DON'T: Use requests directly without retry/validation
import requests
response = requests.get(url)  # No retry, no validation!
```

#### When Tracking Execution

```python
# ✅ DO: Use MetadataTracker for all tracking
tracker = MetadataTracker(metadata_db)
run_id = tracker.create_run("job_name")
# ... do work ...
tracker.complete_run(run_id, JobStatus.SUCCESS)

# ❌ DON'T: Use the old ReportManager
from src.reports.reporter import ReportManager  # DEPRECATED!
```

#### When Logging

```python
# ✅ DO: Use structured logging for events
struct_logger.info("download_completed", 
    file="comercio.zip",
    size_bytes=52428800,
    duration_sec=45.2
)

# ❌ DON'T: Use print statements
print(f"Downloaded {file} in {duration}s")  # Not structured, not searchable!
```

### Monitoring & Observability

**Dashboards** (query `metadata.duckdb`):

1. **Job Success Rate**:
```sql
SELECT 
    DATE(run_date) as date,
    COUNT(*) as total_runs,
    SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) as successful,
    ROUND(100.0 * SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) / COUNT(*), 2) as success_rate_pct
FROM job_runs
WHERE job_name = 'denue_downloader'
GROUP BY DATE(run_date)
ORDER BY date DESC;
```

2. **Download Performance Trends**:
```sql
SELECT 
    period,
    COUNT(*) as files,
    SUM(file_size_bytes) / 1024 / 1024 / 1024 as total_gb,
    AVG(download_speed_mbps) as avg_speed,
    AVG(duration_seconds) as avg_duration
FROM download_tasks
WHERE status = 'SUCCESS'
GROUP BY period
ORDER BY period DESC;
```

3. **Error Analysis**:
```sql
SELECT 
    error_type,
    COUNT(*) as occurrences,
    AVG(attempt_number) as avg_attempts,
    GROUP_CONCAT(DISTINCT period) as affected_periods
FROM download_tasks
WHERE status = 'FAILED'
GROUP BY error_type;
```

### Dependencies Added

```toml
[project]
dependencies = [
    # ... existing ...
    "tenacity>=8.2.3",      # Retry logic with exponential backoff
    "structlog>=24.1.0",    # Structured JSON logging
]

[dependency-groups]
dev = [
    # ... existing ...
    "pytest-httpserver>=1.0.8",  # HTTP mocking for tests
]
```

### Files Modified/Created

**NEW Files** (16):
```
src/metadata/__init__.py
src/metadata/schema.py
src/metadata/models.py
src/metadata/tracker.py
src/core/logging.py
src/extractors/download_config.py
src/extractors/file_validator.py
src/extractors/downloader.py
tests/unit/test_file_validator.py
tests/unit/test_downloader.py
tests/unit/test_metadata_tracker.py
tests/integration/test_denue_downloader.py
tests/e2e/test_full_pipeline.py
docs/ETL_DOWNLOAD_ARCHITECTURE.md
docs/DOWNLOAD_FIX_SUMMARY.md              # Documents database fix (Feb 2026)
docs/DATA_LINEAGE_IMPLEMENTATION.md       # Documents lineage implementation (Feb 2026)
```

**REFACTORED Files** (3):
```
src/extractors/denue_downloader.py     # Complete rewrite with auto-lineage recording
src/utils/denue_scraping.py            # Updated to use MetadataTracker + force mode
src/main.py                            # Updated CLI commands with --force flag
```

**DEPRECATED Files** (1):
```
src/reports/reporter.py                # Replaced by metadata/tracker.py
```

### Recent Fixes & Enhancements (Feb 2026)

#### 1. Database Transaction Fix
**Problem**: 24-hour download (459 files, 8.6 GB) crashed at end with foreign key constraint error.

**Solution**:
- Added explicit `commit()`/`rollback()` in `src/metadata/tracker.py` (lines 54-73)
- Removed foreign key constraints from `data_lineage` and `download_tasks` tables
- Manual fix script successfully completed the stuck run
- See `docs/DOWNLOAD_FIX_SUMMARY.md` for full details

#### 2. Force Download Mode
**Feature**: Added `--force` flag to re-download all files ignoring cache.

**Files Modified**:
- `Makefile` (line 22): Added `$(ARGS)` parameter support
- `src/main.py` (lines 28-32): Added `--force` CLI option
- `src/extractors/denue_downloader.py` (lines 40, 43, 137-141, 160): Force mode logic
- `src/utils/denue_scraping.py` (lines 27, 72-86, 104-111, 172-177): Skip logic

**Usage**:
```bash
make download                    # Normal incremental download
make download ARGS="--force"     # Force re-download all files
```

#### 3. Data Lineage Auto-Recording
**Problem**: `data_lineage` table was empty - tracking existed but was never called.

**Solution**:
- Added auto-recording call in `src/extractors/denue_downloader.py` (lines 261-268)
- Backfilled 459 historical lineage records (8.60 GB tracked)
- Future downloads automatically create lineage records
- See `docs/DATA_LINEAGE_IMPLEMENTATION.md` for complete documentation

**Current State**:
```sql
SELECT COUNT(*) FROM data_lineage;                    -- 459 records
SELECT SUM(bytes_processed)/1e9 FROM data_lineage;    -- 8.60 GB tracked
```

#### 4. Codebase Cleanup
**Removed**: One-time scripts no longer needed in production:
- ~~`fix_incomplete_run.py`~~ (one-time fix, job complete)
- ~~`scripts/backfill_download_lineage.py`~~ (one-time backfill, job complete)
- ~~`scripts/`~~ directory (empty, removed)

**Retained**: Production code with auto-recording at line 262 of `denue_downloader.py`

### Breaking Changes

⚠️ **Clean Break Migration**: The old `reports.db` system has been completely replaced.

**Migration Required**:
1. ~~Run migration script: `python scripts/migrate_reports_to_metadata.py`~~ (DEPRECATED - script removed Feb 2026)
2. Verify data: Check `data/metadata.duckdb` has all historical records
3. Update processor: Ensure `denue_processor.py` reads from `metadata.duckdb`
4. Archive old database: `mv data/reports.db data/reports.db.backup`

**Processor Compatibility**:
```python
# OLD (deprecated)
from src.reports.reporter import ReportManager
tasks = report_mgr.download_report.get_raw_tasks()

# NEW (standard)
from src.metadata.tracker import MetadataTracker
tasks = tracker.get_tasks_by_status(TaskStatus.SUCCESS)
```

---

## Unified Transformer Architecture (Feb 2026)

### Overview

The ETL pipeline has been unified under a single `DenueTransformer` orchestrator that coordinates all transformation stages with complete metadata tracking.

### Architecture: Unified Pipeline

```
┌─────────────────────────────────────────────────────────────┐
│                 DENUE TRANSFORMER                           │
│                 (Unified Orchestrator)                      │
│  ┌──────────────────┬─────────────────┬───────────────────┐│
│  │ ProcessorStage   │ HarmonizerStage │ MetadataTracker   ││
│  │ (ZIP→Warehouse)  │ (Entity Resolution) │ (Observability) ││
│  └──────────────────┴─────────────────┴───────────────────┘│
└─────────────────────────────────────────────────────────────┘
```

### Core Components

#### 1. DenueTransformer (`src/transformers/denue_transformer.py`)

**Unified orchestrator for all transformation stages**:

```python
from src.transformers.denue_transformer import DenueTransformer

# Initialize
transformer = DenueTransformer(data_root=Path("data"))

# Run individual stages
transformer.run_process_stage(force=False)     # ZIP → Warehouse
transformer.run_harmonize_stage()              # Entity Resolution

# Or run complete pipeline
transformer.run_full_pipeline(force=False)     # Process + Harmonize

# Get status
status = transformer.get_pipeline_status()
```

#### 2. ProcessorStage (`src/transformers/processor_stage.py`)

**Transforms raw ZIP files into warehouse**:
- ZIP extraction and CSV parsing
- Data cleaning and normalization
- Schema validation
- Warehouse loading (DuckDB)
- Seed catalog bootstrapping

#### 3. HarmonizerStage (`src/transformers/harmonizer_stage.py`)

**Resolves entity identities across time**:
- Entity matching across periods
- Master CLEE generation
- Knowledge base management
- Region-based processing
- Snapshot consolidation

#### 4. Extended MetadataTracker

**New tracking capabilities**:

```python
# Process task tracking
task_id = tracker.create_process_task(run_id, task_name, source_path, period)
tracker.start_process_task(task_id)
tracker.complete_process_task(task_id, status, rows_loaded=1000)

# Harmonize task tracking
task_id = tracker.create_harmonize_task(run_id, task_name, region, states)
tracker.complete_harmonize_task(task_id, status, entities_matched=8000)

# Metrics
process_metrics = tracker.get_process_metrics()
harmonize_metrics = tracker.get_harmonize_metrics()
```

### Data Flow

```
1. DOWNLOAD → download_tasks (metadata.duckdb)
              ↓ data_lineage: WEB → FILE
              
2. PROCESS →  process_tasks (metadata.duckdb)
              ↓ data_lineage: FILE → TABLE
              
3. HARMONIZE → harmonize_tasks (metadata.duckdb)
               ↓ data_lineage: TABLE → FILE (curated)
```

### Database Schema

**New tables in `metadata.duckdb`**:

```sql
-- Process tracking
CREATE TABLE process_tasks (
    task_id VARCHAR(36) PRIMARY KEY,
    run_id VARCHAR(36) NOT NULL,
    source_path VARCHAR NOT NULL,
    period VARCHAR(20),
    status VARCHAR(20),
    rows_loaded BIGINT,
    rows_rejected BIGINT,
    duration_seconds DOUBLE,
    ...
);

-- Harmonization tracking
CREATE TABLE harmonize_tasks (
    task_id VARCHAR(36) PRIMARY KEY,
    run_id VARCHAR(36) NOT NULL,
    region_group VARCHAR(100),
    state_list VARCHAR,
    status VARCHAR(20),
    entities_matched BIGINT,
    entities_new BIGINT,
    master_clees_created BIGINT,
    ...
);
```

### CLI Commands (Updated)

```bash
# Process stage
python -m src.main process-denue              # Incremental
python -m src.main process-denue --force      # Force reprocess all

# Harmonize stage
python -m src.main harmonize

# Complete pipeline
python -m src.main pipeline                   # Unified pipeline
python -m src.main pipeline --force           # Pipeline with force mode
```

### Makefile Commands (Updated)

```bash
# Individual stages
make process                  # Process ZIPs → Warehouse
make process-force            # Force reprocess all
make harmonize                # Entity resolution

# Complete pipeline
make pipeline                 # Unified pipeline command
make pipeline-force           # Pipeline with force mode
make pipeline-steps           # Step-by-step execution

# Metrics
make metadata-stats           # Show pipeline statistics
```

### Environment Configuration

**New environment variables**:

```bash
# Processing output directories
PROCESSED_OUTPUT_DIR=data/processed    # ZIP → Warehouse transformed data
CURATED_OUTPUT_DIR=data/curated        # Harmonized/Gold layer data
```

### Migration from Legacy System

**COMPLETED (Feb 3, 2026)**:

```
✅ Legacy files ARCHIVED:
   - src/transformers/_legacy/denue_processor.py
   - src/transformers/_legacy/denue_harmonizer.py
   - data/archive/reports.db.backup

❌ Legacy files REMOVED:
   - src/reports/reporter.py (fully replaced by MetadataTracker)

✅ New unified system:
   - src/transformers/denue_transformer.py
   - src/transformers/processor_stage.py
   - src/transformers/harmonizer_stage.py
```

**Backward compatibility**: CLI commands still work, now using new backend.

**Migration documentation**: See `docs/TRANSFORMER_MIGRATION_SUMMARY.md`

### Future Enhancements

**Roadmap** (not yet implemented):

1. **Orchestration**: Add Prefect/Dagster integration
2. **Monitoring**: Prometheus metrics + Grafana dashboards
3. **Alerting**: Email/Slack notifications on failures
4. **Distributed**: Support for distributed downloads (multi-node)
5. **Cloud Storage**: Direct upload to S3/GCS after download
6. **Data Quality**: Integration with Great Expectations
7. **Workflow**: dbt integration for downstream transformations

### References

**Industry Standards**:
- Apache Airflow metadata model: https://airflow.apache.org/docs/apache-airflow/stable/database-erd-ref.html
- Prefect job tracking: https://docs.prefect.io/core/concepts/results.html
- Tenacity retry patterns: https://tenacity.readthedocs.io/
- Structlog practices: https://www.structlog.org/

**Related Documentation**:
- Complete implementation plan: See conversation history
- Database schema: `src/metadata/schema.py`
- Pydantic models: `src/metadata/models.py`
- Configuration options: `src/extractors/download_config.py`

---

## Quick Reference: ETL Download Commands

```bash
# Download DENUE data (incremental - skips existing files)
make download

# Force re-download all files (ignores metadata and disk cache)
make download ARGS="--force"

# Or with custom options
python -m src.main download-inegi \
    --max-retries 5 \
    --validate-hash \
    --show-progress \
    --parallel \
    --force

# Retry failed downloads
python -m src.main retry-downloads

# Show statistics
python -m src.main download-stats --last-n 10

# Check metadata database
duckdb data/metadata.duckdb "SELECT * FROM job_runs ORDER BY run_date DESC LIMIT 5"

# View data lineage (NEW - tracks data provenance)
duckdb data/metadata.duckdb "SELECT * FROM data_lineage ORDER BY created_at DESC LIMIT 10"
duckdb data/metadata.duckdb "SELECT COUNT(*), SUM(bytes_processed)/1e9 as total_gb FROM data_lineage"

# View logs
cat logs/downloads/latest/application.log
cat logs/downloads/latest/summary.json | jq
```