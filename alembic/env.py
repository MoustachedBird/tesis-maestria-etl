"""
Alembic environment configuration.

This module configures Alembic migrations to work with both PostgreSQL and DuckDB.
It uses the settings from src/core/config.py to determine the database backend.
"""

from logging.config import fileConfig
from sqlalchemy import pool
from alembic import context
from alembic.ddl.impl import DefaultImpl
import sys
from pathlib import Path

# Add project root to Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import configuration and models
from src.core.config import get_settings
from src.db.database import get_engine
from src.db.base import Base

# Import all models to ensure they're registered with Base.metadata
from src.db import models  # noqa: F401


# ------------------------------------------------------------------------------
# DuckDB Support for Alembic
# ------------------------------------------------------------------------------
class DuckDBImpl(DefaultImpl):
    """
    Alembic DDL implementation for DuckDB dialect.

    DuckDB is not natively supported by Alembic, so we register it
    to use the default implementation for basic DDL operations.
    """

    __dialect__ = "duckdb"


# ------------------------------------------------------------------------------
# Alembic Configuration
# ------------------------------------------------------------------------------
config = context.config
settings = get_settings()

# Configure logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set target metadata for Alembic migrations
target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    """
    Filter function to exclude certain objects from autogenerate detection.

    This prevents Alembic from detecting its own version table as a change.

    Args:
        obj: The object being considered
        name: Name of the object
        type_: Type of object (table, column, index, etc.)
        reflected: Whether the object was reflected from the database
        compare_to: The object being compared to (if any)

    Returns:
        bool: True if object should be included, False otherwise
    """
    # Exclude alembic_version table from autogenerate
    if type_ == "table" and name == "alembic_version":
        return False
    return True


# ------------------------------------------------------------------------------
# Migration Logic (Offline Mode)
# ------------------------------------------------------------------------------
def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine.
    By skipping the Engine creation we don't even need a DBAPI to be available.

    This is useful for generating SQL scripts without a database connection.
    """
    url = settings.DATABASE_URL

    # Configure context based on backend
    config_dict = {
        "url": url,
        "target_metadata": target_metadata,
        "literal_binds": True,
        "include_object": include_object,
        "dialect_opts": {"paramstyle": "named"},
        "compare_type": True,
        "compare_server_default": True,
    }

    # Only add render_as_batch for PostgreSQL (DuckDB doesn't support it in Alembic)
    if settings.DATABASE_BACKEND == "postgresql":
        config_dict["render_as_batch"] = False

    context.configure(**config_dict)

    with context.begin_transaction():
        context.run_migrations()


# ------------------------------------------------------------------------------
# Migration Logic (Online Mode)
# ------------------------------------------------------------------------------
def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode.

    In this scenario we create an Engine and associate a connection with the context.
    This is the normal mode for running migrations against a live database.
    """
    connectable = get_engine()

    with connectable.connect() as connection:
        # Configure context based on backend
        config_dict = {
            "connection": connection,
            "target_metadata": target_metadata,
            "include_object": include_object,
            "compare_type": True,
            "compare_server_default": True,
        }

        # Only add render_as_batch for PostgreSQL (DuckDB doesn't support it in Alembic)
        if settings.DATABASE_BACKEND == "postgresql":
            config_dict["render_as_batch"] = False

        context.configure(**config_dict)

        with context.begin_transaction():
            context.run_migrations()


# ------------------------------------------------------------------------------
# Entry Point
# ------------------------------------------------------------------------------
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
