"""
Database module exports.

This module exports all database-related components for easy importing.
"""

# Base and metadata
from src_etl.db.base import Base

# Database engine and session management
from src_etl.db.database import (
    get_engine,
    get_session_factory,
    get_db,
    init_db,
    drop_db,
)

# Models
from src_etl.db.models import (
    Establecimiento,
    ActividadEconomica,
    Entidad,
    Municipio,
    Localidad,
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
