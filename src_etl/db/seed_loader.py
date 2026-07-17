"""
Seed Data Loader Orchestrator.

Coordinates loading of all catalog seed data from Parquet files
into database tables during Alembic migrations.

Delegates to domain-specific loaders:
- src_etl.loaders.geography: states, municipalities, localities
- src_etl.loaders.scian: SCIAN economic activities

Usage:
    from src_etl.db.seed_loader import seed_catalog_tables

    # In Alembic migration upgrade() function
    connection = op.get_bind()
    stats = seed_catalog_tables(connection, settings.SEEDS_DIR)
"""

from pathlib import Path

from src_etl.loaders.dominios import (
    load_seeds_sqlalchemy as load_dominios,
)
from src_etl.loaders.estratos import (
    load_seeds_sqlalchemy as load_estratos,
)
from src_etl.loaders.geography import (
    load_seeds_sqlalchemy as load_geography,
)
from src_etl.loaders.geography import (
    validate_seeds as validate_geography,
)
from src_etl.loaders.scian import (
    load_seeds_sqlalchemy as load_scian,
)
from src_etl.loaders.scian import (
    validate_seeds as validate_scian,
)


class SeedLoadError(Exception):
    """Exception raised when seed loading fails."""

    pass


def seed_catalog_tables(
    connection,
    seeds_dir: Path,
) -> dict[str, int]:
    """
    Load all catalog seed files into their respective tables.

    Orchestrates loading in FK dependency order:
    1. Geography: entidades -> municipios -> localidades
    2. SCIAN: actividades_economicas (independent)

    Args:
        connection: SQLAlchemy connection (from Alembic op.get_bind())
        seeds_dir: Path to directory containing seed Parquet files

    Returns:
        dict: Statistics with row counts for each table

    Raises:
        SeedLoadError: If any seed file fails to load
    """
    if not seeds_dir.exists():
        raise SeedLoadError(f"Seeds directory not found: {seeds_dir}")

    stats = {}

    print("\n" + "=" * 70)
    print("Loading Seed Data into Catalog Tables")
    print("=" * 70)

    try:
        # 1. Geography seeds (FK order: entidades -> municipios -> localidades)
        print("\n  Geography catalogs...")
        geo_stats = load_geography(connection, seeds_dir)
        stats.update(geo_stats)

        # 2. SCIAN economic activities
        print("\n  SCIAN activity catalog...")
        scian_stats = load_scian(connection, seeds_dir)
        stats.update(scian_stats)

        # 3. Personnel strata (generated from PersonalOcupado enum, no parquet)
        print("\n  Personnel strata catalog...")
        estratos_stats = load_estratos(connection)
        stats.update(estratos_stats)

        # 4. Domain-type catalogs (generated from enums, no parquet)
        print("\n  Domain-type catalogs (vialidad/asentamiento/unidad)...")
        dominios_stats = load_dominios(connection)
        stats.update(dominios_stats)

        print("\n" + "=" * 70)
        print("All Seed Data Loaded Successfully")
        print("=" * 70)

        # Print summary
        print("\nSummary:")
        total_rows = sum(stats.values())
        for name, count in stats.items():
            print(f"   {name.replace('_', ' ').title()}: {count:,} rows")
        print(f"   Total: {total_rows:,} rows\n")

        return stats

    except Exception as e:
        raise SeedLoadError(f"Seed loading failed: {e}")


def validate_seed_data(seeds_dir: Path) -> bool:
    """
    Validate that all required seed files exist and are readable.

    Args:
        seeds_dir: Path to directory containing seed Parquet files

    Returns:
        bool: True if all validations pass

    Raises:
        SeedLoadError: If any validation fails
    """
    if not seeds_dir.exists():
        raise SeedLoadError(f"Seeds directory not found: {seeds_dir}")

    print("\nValidating Seed Files...")

    try:
        validate_geography(seeds_dir)
        validate_scian(seeds_dir)
    except (FileNotFoundError, ValueError) as e:
        raise SeedLoadError(str(e))

    print("   All seed files validated\n")
    return True
