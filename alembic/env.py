"""
Alembic environment configuration.

Migraciones de Alembic contra la base unica de PostgreSQL.
It uses the settings from src/core/config.py to determine the database backend.
"""

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
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
    Limita la reflexion a nuestros tres schemas.

    Con `include_schemas=True`, Alembic refleja TODOS los schemas de la base
    y marcaria como sobrantes los que no conoce (los de extensiones, por
    ejemplo), proponiendo DROP en el autogenerate. `None` es el schema por
    defecto de la conexion.

    Args:
        name: Nombre del objeto.
        type_: Tipo de objeto (schema, table, index...).
        parent_names: Nombres de los objetos padre.

    Returns:
        bool: True si el objeto debe considerarse.
    """
    if type_ == "schema":
        return name is None or name in SCHEMAS
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

    config_dict = {
        "url": url,
        "target_metadata": target_metadata,
        "literal_binds": True,
        "include_object": include_object,
        "include_name": include_name,
        "dialect_opts": {"paramstyle": "named"},
    }

    config_dict["render_as_batch"] = False
    config_dict["compare_type"] = True
    config_dict["compare_server_default"] = True
    config_dict["include_schemas"] = True
    config_dict["version_table_schema"] = VERSION_TABLE_SCHEMA

    context.configure(**config_dict)

    with context.begin_transaction():
        context.run_migrations()


# ------------------------------------------------------------------------------
# Migration Logic (Online Mode)
# ------------------------------------------------------------------------------
def _get_engine() -> Engine:
    """
    Engine de migraciones: la base unica con sus tres schemas.

    Una sola conexion cubre silver, gold y meta -- la unificacion que motiva
    el rediseno (arquitectura.md §1.3). NullPool porque una migracion abre
    una conexion, la usa y termina: no hay nada que agrupar.
    """
    from sqlalchemy import create_engine as sa_create_engine
    from sqlalchemy.pool import NullPool

    return sa_create_engine(settings.DATABASE_URL, poolclass=NullPool, future=True)


def _ensure_schemas(connection) -> None:
    """Crea los tres schemas antes de que Alembic toque nada.

    Huevo y gallina, encontrado en la primera corrida contra una base virgen
    (2026-10-20): `version_table_schema="meta"` hace que Alembic cree
    `meta.alembic_version` **antes** de ejecutar migracion alguna, pero el
    schema `meta` lo creaba el propio baseline en su primer paso. Resultado:
    `InvalidSchemaName: schema "meta" does not exist` sobre base limpia.

    Los schemas no son contenido de una migracion sino el continente donde
    viven todas --incluida la tabla de versiones--, asi que su creacion va
    aqui, antes del `run_migrations`. El `CREATE SCHEMA IF NOT EXISTS` que
    el baseline todavia hace queda redundante pero inofensivo.

    Args:
        connection: conexion abierta sobre la que correran las migraciones.
    """
    from sqlalchemy import text

    for schema in SCHEMAS:
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    connection.commit()


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode.

    In this scenario we create an Engine and associate a connection with the context.
    This is the normal mode for running migrations against a live database.

    Nota: en PostgreSQL una sola conexion cubre los tres schemas.
    """
    connectable = _get_engine()

    with connectable.connect() as connection:
        _ensure_schemas(connection)

        config_dict = {
            "connection": connection,
            "target_metadata": target_metadata,
            "include_object": include_object,
            "include_name": include_name,
        }

        config_dict["render_as_batch"] = False
        config_dict["compare_type"] = True
        config_dict["compare_server_default"] = True
        # Sin include_schemas, autogenerate solo ve el schema por defecto
        # de la conexion y propondria DROP de lo que vive en los otros dos.
        config_dict["include_schemas"] = True
        config_dict["version_table_schema"] = VERSION_TABLE_SCHEMA

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
