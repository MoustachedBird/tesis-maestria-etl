"""
Alembic environment configuration.

This module configures Alembic migrations to work with both PostgreSQL and DuckDB.
It uses the settings from src/core/config.py to determine the database backend.
"""

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from alembic.ddl.impl import DefaultImpl
from sqlalchemy import Engine

# Add project root to Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import configuration and models
from src.shared.config import get_settings

# Import all models to ensure they're registered with Base.metadata
from src.shared.db import models  # noqa: F401
from src.shared.db.base import SCHEMAS, Base
from src.shared.enums import Layer


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

VERSION_TABLE_SCHEMA = Layer.META.value
"""Schema donde Alembic guarda `alembic_version`.

En `meta` y no en el default (`silver`) porque es estado de control, no un
dato de negocio: asi `pg_dump -n silver` trae solo datos y el versionado
viaja con el resto de la observabilidad.
"""


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


def include_name(name, type_, parent_names):
    """
    Filter function to prevent DuckDB from querying problematic system catalogs.

    DuckDB has limited PostgreSQL compatibility and doesn't support all pg_catalog tables.
    This filter prevents Alembic from attempting queries that would fail.

    Args:
        name: Name of the object
        type_: Type of object (table, column, index, etc.)
        parent_names: Dictionary of parent object names

    Returns:
        bool: True if object should be included, False otherwise
    """
    # For DuckDB, skip collation introspection (causes pg_collation errors)
    if settings.DATABASE_BACKEND == "duckdb":
        # Skip any system catalog queries
        if type_ == "schema" and name in ("information_schema", "pg_catalog"):
            return False
        return True

    # En PostgreSQL con include_schemas=True, Alembic reflejaria TODOS los
    # schemas de la base y marcaria como "sobrantes" los que no conoce. Se
    # limita a los nuestros: `None` es el schema por defecto de la conexion.
    if type_ == "schema":
        return name in SCHEMAS or name is None
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
    url = (
        settings.DATABASE_URL
        if settings.DATABASE_BACKEND == "postgresql"
        else settings.SILVER_DATABASE_URL
    )

    # Configure context based on backend
    config_dict = {
        "url": url,
        "target_metadata": target_metadata,
        "literal_binds": True,
        "include_object": include_object,
        "include_name": include_name,
        "dialect_opts": {"paramstyle": "named"},
    }

    # Backend-specific configuration
    if settings.DATABASE_BACKEND == "postgresql":
        config_dict["render_as_batch"] = False
        config_dict["compare_type"] = True
        config_dict["compare_server_default"] = True
        config_dict["include_schemas"] = True
        config_dict["version_table_schema"] = VERSION_TABLE_SCHEMA
    elif settings.DATABASE_BACKEND == "duckdb":
        # DuckDB has limited introspection capabilities
        # Disable comparisons that trigger problematic catalog queries
        config_dict["compare_type"] = False
        config_dict["compare_server_default"] = False

    context.configure(**config_dict)

    with context.begin_transaction():
        context.run_migrations()


# ------------------------------------------------------------------------------
# Migration Logic (Online Mode)
# ------------------------------------------------------------------------------
def _get_engine() -> Engine:
    """
    Engine de migraciones: la base unica con sus tres schemas.

    En PostgreSQL es una sola conexion para silver, gold y meta -- la
    unificacion que motiva todo el rediseno (arquitectura.md §1.3). En
    DuckDB apunta al archivo Silver, que es lo unico que Alembic gestionaba
    cuando Gold era una copia del archivo.
    """
    from sqlalchemy import create_engine as sa_create_engine
    from sqlalchemy.pool import NullPool

    if settings.DATABASE_BACKEND == "postgresql":
        return sa_create_engine(settings.DATABASE_URL, poolclass=NullPool, future=True)

    return sa_create_engine(
        settings.SILVER_DATABASE_URL,
        poolclass=NullPool,
        connect_args={"read_only": False},
        future=True,
    )


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode.

    In this scenario we create an Engine and associate a connection with the context.
    This is the normal mode for running migrations against a live database.

    Nota: en PostgreSQL una sola conexion cubre los tres schemas.
    """
    connectable = _get_engine()

    with connectable.connect() as connection:
        # Configure context based on backend
        config_dict = {
            "connection": connection,
            "target_metadata": target_metadata,
            "include_object": include_object,
            "include_name": include_name,
        }

        # Backend-specific configuration
        if settings.DATABASE_BACKEND == "postgresql":
            config_dict["render_as_batch"] = False
            config_dict["compare_type"] = True
            config_dict["compare_server_default"] = True
            # Sin include_schemas, autogenerate solo ve el schema por
            # defecto de la conexion y propondria DROP de todo lo que vive
            # en los otros dos.
            config_dict["include_schemas"] = True
            config_dict["version_table_schema"] = VERSION_TABLE_SCHEMA
        elif settings.DATABASE_BACKEND == "duckdb":
            # DuckDB has limited introspection capabilities
            # Disable comparisons that trigger problematic catalog queries
            config_dict["compare_type"] = False
            config_dict["compare_server_default"] = False

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
