# AGENTS.md - Development Guide for ETL Core

## IMPORTANT: Destructive Operations Policy

**NEVER delete, truncate, or overwrite data files (databases, parquets, etc.) without explicit user confirmation first.** This includes:
- Removing `.duckdb`, `.db`, or `.parquet` files
- Running `rm -f` on any data file
- Dropping or truncating database tables with data
- Any operation that could cause data loss

Always ask the user before executing any destructive operation, even if it seems like a necessary intermediate step.

---

## Project Overview

**Name:** `tesis-core` (v0.1.0)
**Purpose:** ETL pipeline analyzing mortality and survival of SMEs in Mexico using INEGI's DENUE microdata.

- **Language:** Python 3.12+
- **Package Manager:** uv
- **Frameworks:** FastAPI (API), Typer (CLI), SQLAlchemy 2.0 (ORM), Alembic (migrations)
- **Data Engine:** DuckDB (OLAP) + Polars (DataFrames)
- **Storage:** DuckDB (Silver + Gold warehouses), Apache Parquet (seeds/curated)
- **Architecture:** Hexagonal/Clean Architecture with medallion data lake (Bronze/Silver/Gold)
- **Design Decision:** Monolithic ETL -- YAGNI; single service doesn't justify microservices

---

## Directory Layout

```
etl-core/
├── src_etl/                       # ETL application code
│   ├── main.py                    # CLI entry point (Typer)
│   ├── core/                      # Configuration and infrastructure
│   │   ├── config.py              # App settings (pydantic-settings)
│   │   ├── download_config.py     # Download settings (retries, timeouts)
│   │   └── logging.py             # Structured logging (structlog)
│   ├── db/                        # Database layer
│   │   ├── models.py              # SQLAlchemy ORM models (5 tables, 38 cols)
│   │   ├── database.py            # Engine/session management
│   │   ├── enums.py               # DENUE enums (TipoVial, PersonalOcupado, etc.)
│   │   └── seed_loader.py         # Seed orchestrator (delegates to loaders/)
│   ├── extractors/                # Domain-specific data extraction
│   │   └── denue.py               # DENUE orchestrator (Selenium + parallel)
│   ├── transformers/              # Transformation layer
│   │   ├── denue.py               # Unified orchestrator (DenueTransformer)
│   │   ├── processor.py           # TransformStage: ZIP -> Silver DuckDB
│   │   ├── harmonizer.py          # HarmonizerStage: Entity resolution across time
│   │   └── _legacy/               # Archived old processor/harmonizer
│   ├── loaders/                   # Data loading layer
│   │   ├── loader.py              # LoaderStage: Silver -> Gold (file copy)
│   │   ├── denue.py               # DataStoreManager (DuckDB upsert)
│   │   ├── geography.py           # Geography seeds (entidades, municipios, localidades)
│   │   └── scian.py               # SCIAN economic activity seeds
│   └── utils/                     # Shared utilities
│       ├── browser.py             # Selenium Chrome driver setup
│       ├── denue_schema.py        # DENUE column mappings, enums, aliases
│       ├── denue_scraping.py      # DENUE web scraping + filename sanitization
│       ├── downloader.py          # Generic HTTP downloader with retry
│       ├── entity_resolution.py   # Cascade matching algorithm engine
│       ├── file_validator.py      # SHA256, size, ZIP validation
│       ├── harmonizer_config.py   # Harmonizer regions/schema config
│       ├── matching.py            # Spatial + fuzzy matching utilities
│       └── warehouse.py           # DuckDB BaseWarehouse class
│
├── src_api/                       # FastAPI REST API (separated)
│   ├── __init__.py
│   ├── main.py                    # FastAPI app with lifespan, CORS
│   └── schemas.py                 # Pydantic request/response models
│
├── src_metadata/                  # Metadata tracking service (separated)
│   ├── __init__.py
│   ├── models.py                  # Pydantic models (JobRun, tasks, lineage)
│   ├── schema.py                  # DDL for metadata.duckdb
│   └── tracker.py                 # MetadataTracker class
│
├── data/
│   ├── bronze/                    # Raw downloaded ZIPs (not committed)
│   ├── silver/                    # Processed intermediate (not committed)
│   ├── gold/                      # Final DuckDB warehouse (not committed)
│   ├── seeds/                     # Parquet seed files (committed)
│   │   ├── seed_entidades.parquet       # 32 states
│   │   ├── seed_municipios.parquet      # 2,478 municipalities
│   │   ├── seed_localidades.parquet     # 39,140 localities
│   │   └── seed_actividades.parquet     # 1,127 economic activities
│   ├── processed/                 # ZIP -> Warehouse transformed data
│   └── curated/                   # Harmonized Gold layer data
│
├── alembic/                       # Database migrations
├── tests/
│   ├── conftest.py                # Fixtures (manual + real DB)
│   ├── unit/                      # Unit tests
│   └── integration/               # Integration tests
├── AGENTS.md                      # This file
├── Makefile                       # Task runner (30+ targets)
├── Dockerfile.etl                 # Container with Chrome for scraping
├── docker-compose.yml             # Docker services
├── pyproject.toml                 # Dependencies and config
└── uv.lock                        # Locked dependencies
```

---

## Build Commands

```bash
# Environment
uv sync                                    # Install all dependencies

# Run CLI
uv run python -m src_etl.main             # Main CLI entry point
uv run fastapi dev src_api/main.py        # Dev server (FastAPI)

# Linting
uv run ruff check src_etl/ src_metadata/ src_api/ tests/        # Lint
uv run ruff check src_etl/ src_metadata/ src_api/ tests/ --fix   # Auto-fix
uv run ruff format src_etl/ src_metadata/ src_api/ tests/        # Format

# Testing
uv run pytest                              # All tests
uv run pytest -m unit                      # Unit only
uv run pytest -m "not slow"                # Skip slow
uv run pytest -m "db and not slow"         # DB tests, skip slow
uv run pytest -vv                          # Verbose
```

---

## Makefile Targets

```bash
# ETL Pipeline
make extract                     # Extract data from INEGI (Bronze)
make extract ARGS="--force"      # Force re-download all files
make transform                   # Transform raw data (Bronze -> Silver)
make transform-force             # Force reprocess all
make load                        # Load Silver -> Gold
make harmonize                   # Entity resolution
make pipeline                    # Full: Extract -> Transform -> Load
make pipeline-force              # Full pipeline with force mode

# Database
make db-migrate                  # Run pending Alembic migrations
make db-downgrade                # Rollback last migration
make db-seed                     # Reload seed data (idempotent)
make db-reset                    # Full reset (destructive, asks confirmation)
make db-status                   # Show migration status

# Metadata
make metadata-query              # Query recent job runs
make metadata-stats              # Show pipeline statistics

# Testing
make test                        # All tests
make test-unit / test-integration / test-db / test-fast
make test-cov                    # Tests with coverage report
make lint / lint-fix / format    # Ruff linting and formatting
```

---

## Architecture

### ETL Pipeline Flow

```
┌──────────────────────────────────────────────────────────────────────┐
│                   DENUE TRANSFORMER (Orchestrator)                    │
│  ┌────────────────┬─────────────────┬──────────────┬──────────────┐ │
│  │ TransformStage │ HarmonizerStage │ LoaderStage  │ Metadata     │ │
│  │ (ZIP->Silver)  │ (Entity Resoln) │ (Silver->Gold│ Tracker      │ │
│  └────────────────┴─────────────────┴──────────────┴──────────────┘ │
└──────────────────────────────────────────────────────────────────────┘

Data Flow:
  EXTRACT   -> download_tasks  (lineage: WEB -> FILE)
  TRANSFORM -> process_tasks   (lineage: FILE -> TABLE in Silver)
  LOAD      -> process_tasks   (lineage: Silver -> Gold file copy)
  HARMONIZE -> harmonize_tasks (lineage: TABLE -> FILE)
```

### Metadata Layer

```
┌─────────────────────────────────────────────────────────────┐
│                 data/metadata.duckdb                         │
│  ┌────────────┬────────────────┬──────────────┐             │
│  │ job_runs   │ download_tasks │ data_lineage │             │
│  │ (run-level)│ (file-level)   │ (provenance) │             │
│  ├────────────┼────────────────┼──────────────┤             │
│  │ process_   │ harmonize_     │              │             │
│  │ tasks      │ tasks          │              │             │
│  └────────────┴────────────────┴──────────────┘             │
└─────────────────────────────────────────────────────────────┘
```

### Storage Layer

```
data/bronze/denue/{period}/*.zip      # Raw downloads
data/silver/denue.duckdb              # Silver DuckDB warehouse
data/gold/denue.duckdb                # Gold DuckDB warehouse (production)
data/curated/denue/snapshots/         # Harmonized snapshots
data/curated/denue/master_catalog.parquet
```

### Logging Layer

```
logs/downloads/YYYY-MM-DD/run_TIMESTAMP/
├── structured.jsonl     (machine-readable)
├── application.log      (human-readable)
├── errors.log           (errors only)
└── summary.json         (run statistics)
```

---

## Database

### ORM Models (5 tables)

| Model | Table | Primary Key | Rows |
|-------|-------|------------|------|
| `Establecimiento` | `establecimientos` | `id` (hash) | 100M+ |
| `ActividadEconomica` | `denue_actividades_economicas` | `(codigo_act, nombre_act)` | 1,127 |
| `Entidad` | `denue_entidades` | `cve_ent` | 32 |
| `Municipio` | `denue_municipios` | `(cve_ent, cve_mun)` | 2,478 |
| `Localidad` | `denue_localidades` | `(cve_ent, cve_mun, cve_loc)` | 39,140 |

### Establecimiento (38 columns)

| Category | Columns |
|----------|---------|
| PK | `id` |
| Identification | `clee`, `nom_estab`, `raz_social` |
| Economic | `codigo_act`, `tipounieco`, `per_ocu` |
| Address | `tipo_vial`, `nom_vial`, `numero_ext`, `letra_ext`, `numero_int`, `letra_int`, `edificio`, `edificio_e` |
| Cross Streets | `tipo_v_e_1/2/3`, `nom_v_e_1/2/3` |
| Settlement | `tipo_asent`, `nomb_asent`, `nom_cencom`, `num_local`, `cod_postal` |
| Geographic | `cve_ent`, `cve_mun`, `cve_loc`, `ageb`, `manzana` |
| Coordinates | `latitud`, `longitud` |
| Contact | `telefono`, `correoelec`, `www` |
| Temporal | `fecha_alta`, `period` |

### Performance Indexes (100M+ rows)

| Index | Columns | Speed Gain |
|-------|---------|-----------|
| `ix_estab_geo` | `cve_ent, cve_mun, cve_loc` | 100-600x |
| `ix_estab_activity_period` | `codigo_act, period` | 90-180x |
| `ix_estab_composite` | `cve_ent, codigo_act, per_ocu` | 300-600x |
| `ix_estab_postal` | `cod_postal` | 50-100x |
| `ix_estab_coords` | `latitud, longitud` | 80-200x |
| `ix_estab_clee` | `clee` | 200-500x |

### Backend Configuration

```bash
DATABASE_BACKEND=duckdb          # or postgresql
SILVER_DUCKDB_PATH=data/silver/denue.duckdb
GOLD_DUCKDB_PATH=data/gold/denue.duckdb
DUCKDB_THREADS=8
DUCKDB_MEMORY_LIMIT=16GB
```

| Backend | Migrations | Use Case |
|---------|-----------|----------|
| DuckDB | `init_db()` (create_all) | Development, Analytics |
| PostgreSQL | Alembic | Production, Multi-user |

**DuckDB limitation:** Does not support Alembic autogenerate or foreign keys in ETL patterns. Use application-level integrity.

### Seed Loading

Loading order (FK hierarchy): `entidades` -> `municipios` -> `localidades` -> `actividades_economicas`

```bash
make db-migrate          # Initial setup (creates schema + loads seeds)
make db-seed             # Reload seeds (truncate-and-reload, idempotent)
```

Verification:
```bash
uv run python -c "
import duckdb
from src_etl.core.config import settings
conn = duckdb.connect(str(settings.GOLD_DUCKDB_PATH))
for t in ['denue_entidades','denue_municipios','denue_localidades','denue_actividades_economicas']:
    print(f'{t}: {conn.execute(f\"SELECT COUNT(*) FROM {t}\").fetchone()[0]}')
conn.close()
"
# Expected: 32, 2478, 39140, 1127
```

### API Endpoints

```
GET /health              # Basic health check
GET /api/v1/health/db    # Database health check
GET /api/v1/info         # API information
GET /api/v1/docs         # Swagger UI
GET /api/v1/redoc        # ReDoc
```

---

## Environment Variables

```bash
# Database
DATABASE_BACKEND=duckdb
SILVER_DUCKDB_PATH=data/silver/denue.duckdb
GOLD_DUCKDB_PATH=data/gold/denue.duckdb
DUCKDB_THREADS=8
DUCKDB_MEMORY_LIMIT=16GB
METADATA_DUCKDB_PATH=data/metadata.duckdb

# PostgreSQL (when DATABASE_BACKEND=postgresql)
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=denue
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_password

# SQLAlchemy
SQLALCHEMY_ECHO=false
SQLALCHEMY_POOL_SIZE=5
SQLALCHEMY_MAX_OVERFLOW=10
SQLALCHEMY_POOL_PRE_PING=true

# Application
LOG_LEVEL=INFO
API_TITLE=DENUE ETL API
API_VERSION=0.1.0
API_PREFIX=/api/v1

# Processing
PROCESSED_OUTPUT_DIR=data/processed
CURATED_OUTPUT_DIR=data/curated

# Testing
TEST_DB_PATH=data/denue_warehouse_most_recent.db
```

---

## Metadata Tracking (src_metadata)

### MetadataTracker Usage

```python
from src_metadata.tracker import MetadataTracker
from src_metadata.models import JobStatus, TaskStatus

tracker = MetadataTracker(Path("data/metadata.duckdb"))

# Job run lifecycle
run_id = tracker.create_run("denue_downloader", config={...})
tracker.complete_run(run_id, JobStatus.SUCCESS)

# Download tasks
task_id = tracker.create_task(run_id, "download_comercio_2024_11", url, path)
tracker.start_task(task_id)
tracker.complete_task(task_id, TaskStatus.SUCCESS, result)

# Process tasks
task_id = tracker.create_process_task(run_id, task_name, source_path, period)
tracker.complete_process_task(task_id, status, rows_loaded=1000)

# Harmonize tasks
task_id = tracker.create_harmonize_task(run_id, task_name, region, states)
tracker.complete_harmonize_task(task_id, status, entities_matched=8000)

# Queries
failed = tracker.get_failed_tasks(retryable_only=True)
summary = tracker.get_run_summary(run_id)
metrics = tracker.get_process_metrics()
```

### Monitoring Queries

```sql
-- Failed retryable downloads
SELECT * FROM download_tasks
WHERE status = 'FAILED' AND is_retryable = TRUE AND attempt_number < max_retries;

-- Performance by period
SELECT period, COUNT(*), AVG(file_size_bytes)/1024/1024 as avg_mb,
       AVG(download_speed_mbps), AVG(duration_seconds)
FROM download_tasks WHERE status = 'SUCCESS' GROUP BY period;

-- Job success rate
SELECT DATE(run_date), COUNT(*),
  ROUND(100.0 * SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END) / COUNT(*), 2) as pct
FROM job_runs GROUP BY DATE(run_date) ORDER BY date DESC;

-- Data lineage audit
SELECT dl.*, dt.status, jr.job_name
FROM data_lineage dl
JOIN download_tasks dt ON dl.task_id = dt.task_id
JOIN job_runs jr ON dl.run_id = jr.run_id
ORDER BY dl.created_at;
```

### Error Classification

| Error | Retryable | Description |
|-------|-----------|-------------|
| NETWORK | Yes | Connection issues |
| HTTP_500 | Yes | Server errors |
| TIMEOUT | Yes | Request timeout |
| HTTP_404 | No | Not found (permanent) |
| VALIDATION | No | Corrupt file (permanent) |
| ZIP_CORRUPT / HASH_MISMATCH / SIZE_MISMATCH | No | File integrity failure |

---

## Testing

### Markers

```python
@pytest.mark.unit          # Fast, isolated
@pytest.mark.integration   # Real database
@pytest.mark.db            # Database-related
@pytest.mark.slow          # 100M+ row queries
@pytest.mark.catalog       # Catalog table tests
@pytest.mark.quality       # Data quality validation
```

### Fixtures (conftest.py)

**Manual:** `sample_establecimiento()`, `sample_entidad()`, `sample_municipio()`, `sample_localidad()`
**Real DB:** `production_db()` (read-only DuckDB), `real_sample_establecimiento()`, `db_schema_columns()`

---

## Code Style

### Principles
- **Polars** for DataFrames (never pandas)
- **Type hints** on all public functions
- **pydantic-settings** for configuration (never hardcode paths)
- **Structured logging** (never `print()`)
- **Context managers** for database connections

### Import Organization

```python
# Standard library
from pathlib import Path
from typing import Optional

# Third-party
import polars as pl
from pydantic import BaseModel

# Local - ETL
from src_etl.core.config import settings
from src_etl.transformers.denue import DenueTransformer

# Local - Metadata
from src_metadata.tracker import MetadataTracker
from src_metadata.models import JobStatus
```

---

## Alembic Migrations

**Golden rule:** NEVER call `commit()` or `rollback()` in migration code. Let Alembic manage transactions.

```
Alembic Transaction Lifecycle:
  1. BEGIN TRANSACTION
  2. Execute migration upgrade()
  3. Update alembic_version table
  4. COMMIT (or ROLLBACK on error)
```

Anti-patterns:
- Calling `connection.commit()` inside migrations
- Not re-raising exceptions in helper functions
- Mixing transaction levels between migrations and helpers

Verify cycle:
```bash
alembic upgrade head      # Apply all
alembic downgrade -1      # Rollback seeds (data: 0 rows)
alembic upgrade head      # Reapply (data: 42,777 rows)
```

---

## Git Rules

**Committed:** `data/seeds/*.parquet`, `uv.lock`, `*.py`, `*.md`, config files
**Ignored:** `data/bronze/*`, `data/silver/*`, `data/gold/*`, `*.duckdb`, `*.db`, `.env`, `logs/`, `.ruff_cache/`, `data/archive/`

---

## Docker

```bash
# Build
docker build -f Dockerfile.etl -t tesis-etl:local .

# Run (downloads data into local data/ volume)
docker compose run etl extract --force

# Run pipeline
docker compose run etl pipeline
```

---

## Quick Reference

```bash
# Setup from scratch
uv sync && make db-migrate

# Daily development
make test                        # Run tests (180 should pass)
make lint                        # Check code quality

# Run ETL
make extract                     # Step 1: Download from INEGI
make transform                   # Step 2: ZIP -> Silver DuckDB
make load                        # Step 3: Silver -> Gold DuckDB
# OR
make pipeline                    # All three steps

# Check status
make metadata-stats              # Pipeline statistics
make db-status                   # Migration status
duckdb data/metadata.duckdb "SHOW TABLES"
```
