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
make help           # Show all available commands
make setup          # Install dependencies
make download       # Download INEGI data
make process        # Process RAW → Warehouse
make process-parallel # Process in parallel mode
make normalize      # Normalize dates
make harmonize      # Run harmonization
make validate       # Validate warehouse
make pipeline       # Run complete pipeline
make clean          # Clean cache and compiled files
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
├── db/            # Database models and Alembic migrations
└── main.py        # CLI entry point using Typer

data/
├── bronze/        # Raw data (never commit)
├── silver/        # Processed data (never commit)  
└── gold/          # Final data (never commit)
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
- **SQLAlchemy for migrations:** Use Alembic with SQLAlchemy for schema management
- **Connection management:** Use context managers for database connections
- **Query optimization:** Write SQL that works well with DuckDB's columnar storage

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

## Development Workflow

1. **Setup:** `make setup` or `uv sync`
2. **Development:** Write code in `src/` following the patterns above
3. **Testing:** `uv run pytest` to run tests
4. **Code Quality:** Run linting and type checking if configured
5. **Documentation:** Update docstrings and comments as needed

## Important Notes

- **Data files** in `data/` directories are never committed (see `.gitignore`)
- **Local databases** (`*.duckdb`, `*.db`) are excluded from version control
- **Environment variables** should be managed through `.env` files
- **Dependencies** are managed through `uv` with `pyproject.toml`
- **Lock file** (`uv.lock`) should be committed for reproducibility

## Common Pitfalls to Avoid

- Don't use pandas - prefer Polars for performance
- Don't hardcode paths - use the settings configuration
- Don't ignore type hints - they're required for maintainability
- Don't commit data files or local databases
- Don't use print() for logging - use proper logging utilities
- Don't forget to handle exceptions properly throughout the pipeline