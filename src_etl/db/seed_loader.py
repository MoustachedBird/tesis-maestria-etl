"""
Seed Data Loader for Catalog Tables.

This module provides utilities to load seed data from Parquet files into database
catalog tables during Alembic migrations.

The seed loader is designed to:
- Load seed data from data/seeds/ directory
- Support both DuckDB and PostgreSQL backends
- Handle truncate-and-reload strategy for idempotency
- Provide atomic transaction support (all-or-nothing)
- Log detailed progress and statistics

Usage:
    from src_etl.db.seed_loader import seed_catalog_tables

    # In Alembic migration upgrade() function
    connection = op.get_bind()
    stats = seed_catalog_tables(connection, settings.SEEDS_DIR)
"""

from pathlib import Path
from typing import Dict, Optional
import polars as pl
from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError


class SeedLoadError(Exception):
    """Exception raised when seed loading fails."""

    pass


def load_seed_from_parquet(
    connection: Connection,
    seed_file: Path,
    table_name: str,
    truncate: bool = True,
) -> int:
    """
    Load seed data from a Parquet file into a database table.

    This function reads a Parquet file using Polars and inserts the data
    into the specified database table. It supports both DuckDB and PostgreSQL.

    Args:
        connection: SQLAlchemy connection (from Alembic op.get_bind())
        seed_file: Path to the Parquet seed file
        table_name: Target database table name
        truncate: If True, delete existing data before inserting

    Returns:
        int: Number of rows inserted

    Raises:
        SeedLoadError: If the seed file is missing, corrupted, or insert fails

    Examples:
        >>> connection = op.get_bind()
        >>> count = load_seed_from_parquet(
        ...     connection,
        ...     Path("data/seeds/seed_entidades.parquet"),
        ...     "denue_entidades"
        ... )
        >>> print(f"Loaded {count} states")
    """
    # Validate seed file exists
    if not seed_file.exists():
        raise SeedLoadError(f"Seed file not found: {seed_file}")

    try:
        # Read Parquet file using Polars (fast and memory efficient)
        df = pl.read_parquet(seed_file)

        if len(df) == 0:
            print(f"⚠️  Warning: Seed file {seed_file.name} is empty")
            return 0

        # Truncate table if requested
        if truncate:
            connection.execute(text(f"DELETE FROM {table_name}"))
            # NOTE: No commit() here - let Alembic manage the transaction
            # This ensures atomic operations and proper version tracking

        # Convert to list of dictionaries for SQLAlchemy insertion
        records = df.to_dicts()

        # Get column names from DataFrame
        columns = df.columns

        # Build INSERT statement
        # Format: INSERT INTO table (col1, col2) VALUES (:col1, :col2)
        columns_str = ", ".join(columns)
        values_str = ", ".join([f":{col}" for col in columns])
        insert_sql = f"INSERT INTO {table_name} ({columns_str}) VALUES ({values_str})"

        # Execute batch insert
        connection.execute(text(insert_sql), records)
        # NOTE: No commit() here - let Alembic manage the transaction
        # This allows proper rollback if subsequent operations fail

        return len(records)

    except pl.exceptions.ComputeError as e:
        raise SeedLoadError(f"Failed to read Parquet file {seed_file.name}: {e}")
    except SQLAlchemyError as e:
        # NOTE: No rollback() here - let Alembic handle transaction rollback
        # This ensures the entire migration rolls back atomically
        raise SeedLoadError(f"Failed to insert data into {table_name}: {e}")
    except Exception as e:
        # NOTE: No rollback() here - let Alembic handle transaction rollback
        raise SeedLoadError(f"Unexpected error loading {seed_file.name}: {e}")


def seed_catalog_tables(
    connection: Connection,
    seeds_dir: Path,
) -> Dict[str, int]:
    """
    Load all catalog seed files into their respective tables.

    This function orchestrates the loading of all seed files in the correct order
    to respect hierarchical relationships:
    1. States (entidades) - no dependencies
    2. Municipalities (municipios) - depends on states
    3. Localities (localidades) - depends on municipalities
    4. Economic activities (actividades) - no dependencies

    The function uses a truncate-and-reload strategy, clearing existing data
    before inserting fresh seed data.

    Args:
        connection: SQLAlchemy connection (from Alembic op.get_bind())
        seeds_dir: Path to directory containing seed Parquet files

    Returns:
        dict: Statistics with row counts for each table

    Raises:
        SeedLoadError: If any seed file fails to load

    Examples:
        >>> from src_etl.core.config import settings
        >>> connection = op.get_bind()
        >>> stats = seed_catalog_tables(connection, settings.SEEDS_DIR)
        >>> print(f"Loaded {stats['entidades']} states")
    """
    # Validate seeds directory exists
    if not seeds_dir.exists():
        raise SeedLoadError(f"Seeds directory not found: {seeds_dir}")

    # Define seed files and their target tables in correct loading order
    seed_mappings = [
        # (seed_filename, table_name, display_name)
        ("seed_entidades.parquet", "denue_entidades", "states"),
        ("seed_municipios.parquet", "denue_municipios", "municipalities"),
        ("seed_localidades.parquet", "denue_localidades", "localities"),
        (
            "seed_actividades.parquet",
            "denue_actividades_economicas",
            "economic activities",
        ),
    ]

    stats = {}

    print("\n" + "=" * 70)
    print("📦 Loading Seed Data into Catalog Tables")
    print("=" * 70)

    try:
        for seed_filename, table_name, display_name in seed_mappings:
            seed_file = seeds_dir / seed_filename

            print(f"\n🔄 Loading {display_name}...")
            print(f"   Source: {seed_file.name}")
            print(f"   Target: {table_name}")

            # Load seed data
            count = load_seed_from_parquet(
                connection=connection,
                seed_file=seed_file,
                table_name=table_name,
                truncate=True,
            )

            # Store statistics
            key = display_name.replace(" ", "_")
            stats[key] = count

            print(f"   ✅ Loaded {count:,} rows")

        print("\n" + "=" * 70)
        print("✅ All Seed Data Loaded Successfully")
        print("=" * 70)

        # Print summary
        print("\n📊 Summary:")
        total_rows = sum(stats.values())
        for display_name, count in stats.items():
            print(f"   • {display_name.replace('_', ' ').title()}: {count:,} rows")
        print(f"   • Total: {total_rows:,} rows")
        print()

        return stats

    except SeedLoadError:
        # Re-raise seed load errors
        raise
    except Exception as e:
        # Wrap unexpected errors
        raise SeedLoadError(f"Unexpected error during seed loading: {e}")


def validate_seed_data(seeds_dir: Path) -> bool:
    """
    Validate that all required seed files exist and are readable.

    This function performs pre-flight checks before attempting to load seed data:
    - Checks that seeds directory exists
    - Verifies all required seed files are present
    - Tests that Parquet files are readable
    - Validates basic structure (non-empty, has expected columns)

    Args:
        seeds_dir: Path to directory containing seed Parquet files

    Returns:
        bool: True if all validations pass

    Raises:
        SeedLoadError: If any validation fails

    Examples:
        >>> from src_etl.core.config import settings
        >>> validate_seed_data(settings.SEEDS_DIR)
        True
    """
    if not seeds_dir.exists():
        raise SeedLoadError(f"Seeds directory not found: {seeds_dir}")

    # Required seed files and their expected columns
    required_seeds = {
        "seed_entidades.parquet": ["cve_ent", "entidad"],
        "seed_municipios.parquet": ["cve_ent", "cve_mun", "municipio"],
        "seed_localidades.parquet": ["cve_ent", "cve_mun", "cve_loc", "localidad"],
        "seed_actividades.parquet": ["codigo_act", "nombre_act"],
    }

    print("\n🔍 Validating Seed Files...")

    for filename, expected_columns in required_seeds.items():
        seed_file = seeds_dir / filename

        # Check file exists
        if not seed_file.exists():
            raise SeedLoadError(f"Missing required seed file: {filename}")

        # Check file is readable
        try:
            df = pl.read_parquet(seed_file)
        except Exception as e:
            raise SeedLoadError(f"Cannot read seed file {filename}: {e}")

        # Check not empty
        if len(df) == 0:
            raise SeedLoadError(f"Seed file {filename} is empty")

        # Check has expected columns
        actual_columns = set(df.columns)
        expected_columns_set = set(expected_columns)

        if not expected_columns_set.issubset(actual_columns):
            missing = expected_columns_set - actual_columns
            raise SeedLoadError(f"Seed file {filename} missing columns: {missing}")

        print(f"   ✅ {filename}: {len(df):,} rows, {len(df.columns)} columns")

    print("   ✅ All seed files validated\n")
    return True
