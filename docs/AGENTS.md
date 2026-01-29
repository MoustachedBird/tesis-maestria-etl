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
uv run fastapi dev src/tesis_core/interface/api/main.py

# Run ETL pipeline
uv run python -m src.tesis_core.interface.cli.etl --input data/raw --output data/processed
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

# Format code (if configured)
uv run black src/ tests/

# Type checking (if configured)  
uv run mypy src/

# Linting (if configured)
uv run flake8 src/
uv run ruff check src/
```

### Makefile Commands
```bash
# General
make help           # Show all available commands
make setup          # Install dependencies
make clean          # Clean cache and compiled files

# ETL Pipeline
make download       # Download INEGI data
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
├── loaders/        # Data loading modules (Warehouse, S3)
├── reports/        # Reporting and monitoring utilities
├── utils/          # Shared utilities (loggers, connections)
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
from src.transformers.denue_processor import DenueProcessor
from src.loaders.denue_stores import DataStoreManager
from src.reports.reporter import ReportManager
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