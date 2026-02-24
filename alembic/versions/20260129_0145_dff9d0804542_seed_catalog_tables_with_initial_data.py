"""seed catalog tables with initial data

This migration loads initial seed data into catalog tables from Parquet files:
- denue_entidades (32 states)
- denue_municipios (2,478 municipalities)
- denue_localidades (localities)
- denue_actividades_economicas (1,127 economic activities)

The seed files are located in data/seeds/ directory and are loaded using
the seed_loader module which provides:
- Truncate-and-reload strategy for idempotency
- Atomic transaction support (all-or-nothing)
- Validation of seed files before loading
- Detailed progress logging

Revision ID: dff9d0804542
Revises: cbc954c59583
Create Date: 2026-01-29 01:45:18.559444

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = "dff9d0804542"
down_revision: Union[str, Sequence[str], None] = "cbc954c59583"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Load seed data into catalog tables.

    This migration populates the catalog tables with reference data from
    Parquet seed files. The data is loaded in the correct order to respect
    hierarchical relationships:

    1. States (entidades) - 32 rows
    2. Municipalities (municipios) - 2,478 rows
    3. Localities (localidades) - variable rows
    4. Economic activities (actividades) - 1,127 rows

    The migration will fail and rollback if:
    - Seed files are missing
    - Parquet files are corrupted
    - Insert operations fail

    On success, all catalog tables will be populated with fresh data.
    """
    # Import here to avoid module loading issues with Alembic
    from src_etl.db.seed_loader import (
        seed_catalog_tables,
        validate_seed_data,
        SeedLoadError,
    )
    from src_etl.core.config import get_settings

    settings = get_settings()
    connection = op.get_bind()

    try:
        # Step 1: Validate seed files exist and are readable
        print("\n" + "=" * 70)
        print("🔍 Validating Seed Files")
        print("=" * 70)
        validate_seed_data(settings.SEEDS_DIR)

        # Step 2: Clear existing catalog data (truncate in reverse dependency order)
        print("\n" + "=" * 70)
        print("🗑️  Clearing Existing Catalog Data")
        print("=" * 70)
        print("   Truncating tables in reverse dependency order...")

        # Order matters: clear child tables before parent tables
        connection.execute(text("DELETE FROM denue_localidades"))
        print("   ✅ Cleared denue_localidades")

        connection.execute(text("DELETE FROM denue_municipios"))
        print("   ✅ Cleared denue_municipios")

        connection.execute(text("DELETE FROM denue_entidades"))
        print("   ✅ Cleared denue_entidades")

        connection.execute(text("DELETE FROM denue_actividades_economicas"))
        print("   ✅ Cleared denue_actividades_economicas")

        # NOTE: No commit() here - Alembic manages the transaction
        print("   ✅ All catalog tables cleared\n")

        # Step 3: Load seed data
        stats = seed_catalog_tables(connection, settings.SEEDS_DIR)

        # Step 4: Verify data was loaded
        print("=" * 70)
        print("✅ Migration Completed Successfully")
        print("=" * 70)
        print(f"   Catalog tables populated with {sum(stats.values()):,} total rows\n")
        # NOTE: Alembic will commit the entire transaction and update alembic_version

    except SeedLoadError as e:
        print("\n" + "=" * 70)
        print("❌ Seed Loading Failed")
        print("=" * 70)
        print(f"   Error: {e}\n")
        print("   Alembic will rollback the migration automatically...\n")
        # NOTE: No rollback() here - Alembic handles transaction rollback
        raise
    except Exception as e:
        print("\n" + "=" * 70)
        print("❌ Unexpected Error During Migration")
        print("=" * 70)
        print(f"   Error: {e}\n")
        print("   Alembic will rollback the migration automatically...\n")
        # NOTE: No rollback() here - Alembic handles transaction rollback
        raise


def downgrade() -> None:
    """
    Clear all seed data from catalog tables.

    This downgrade function removes all data from catalog tables but keeps
    the table structures intact. This allows the migration to be reversed
    if needed.

    Note: This is a destructive operation that will delete:
    - All state records
    - All municipality records
    - All locality records
    - All economic activity records

    The establecimientos table is NOT affected by this downgrade.
    """
    connection = op.get_bind()

    print("\n" + "=" * 70)
    print("⏮️  Rolling Back Seed Data")
    print("=" * 70)
    print("   Removing all catalog data...\n")

    try:
        # Clear in reverse dependency order
        connection.execute(text("DELETE FROM denue_localidades"))
        print("   ✅ Cleared denue_localidades")

        connection.execute(text("DELETE FROM denue_municipios"))
        print("   ✅ Cleared denue_municipios")

        connection.execute(text("DELETE FROM denue_entidades"))
        print("   ✅ Cleared denue_entidades")

        connection.execute(text("DELETE FROM denue_actividades_economicas"))
        print("   ✅ Cleared denue_actividades_economicas")

        # NOTE: No commit() here - Alembic manages the transaction and version update

        print("\n" + "=" * 70)
        print("✅ Downgrade Completed Successfully")
        print("=" * 70)
        print("   All catalog data removed\n")
        # NOTE: Alembic will commit and update alembic_version to cbc954c59583

    except Exception as e:
        print("\n" + "=" * 70)
        print("❌ Downgrade Failed")
        print("=" * 70)
        print(f"   Error: {e}\n")
        print("   Alembic will rollback automatically...\n")
        # NOTE: No rollback() here - Alembic handles transaction rollback
        raise
