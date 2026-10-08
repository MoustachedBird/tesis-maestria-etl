"""
Database module exports.

This module exports all database-related components for easy importing.
"""

# Base and metadata
from src.shared.db.base import Base

# Models
from src.shared.db.models import (
    ActividadEconomica,
    Entidad,
    Establecimiento,
    Localidad,
    Municipio,
)

# Database engine and session management
from src.shared.db.session import (
    drop_db,
    get_db,
    get_engine,
    get_session_factory,
    init_db,
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
