"""
Database module exports.

This module exports all database-related components for easy importing.
"""

# Base and metadata
from src_etl.db.base import Base

# Database engine and session management
from src_etl.db.database import (
    drop_db,
    get_db,
    get_engine,
    get_session_factory,
    init_db,
)

# Models
from src_etl.db.models import (
    ActividadEconomica,
    Entidad,
    Establecimiento,
    Localidad,
    Municipio,
)

__all__ = [
    # Base
    "Base",
    # Database management
    "get_engine",
    "get_session_factory",
    "get_db",
    "init_db",
    "drop_db",
    # Models
    "Establecimiento",
    "ActividadEconomica",
    "Entidad",
    "Municipio",
    "Localidad",
]
