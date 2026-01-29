"""
SQLAlchemy ORM Models for DENUE Database.

This module contains all database table definitions using SQLAlchemy 2.0 ORM.
All models inherit from the Base class defined in base.py.
"""

from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Date,
    DateTime,
    Boolean,
    Text,
    ForeignKey,
    Index,
    CheckConstraint,
)
from sqlalchemy.orm import mapped_column, Mapped, relationship
from sqlalchemy.sql import func
from datetime import datetime
from typing import Optional

from src.db.base import Base


class Establecimiento(Base):
    """
    Establecimientos (Business Establishments) from DENUE.

    Represents individual business establishments with their location,
    economic activity, and operational details.
    """

    __tablename__ = "establecimientos"

    # Primary Key
    clave: Mapped[str] = mapped_column(
        String(50), primary_key=True, comment="Unique establishment key"
    )

    # Identification
    nombre_establecimiento: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="Establishment name"
    )

    # Economic Activity
    codigo_actividad: Mapped[str] = mapped_column(
        String(10), nullable=False, index=True, comment="SCIAN economic activity code"
    )
    personal_ocupado: Mapped[int] = mapped_column(
        Integer, default=0, comment="Number of employees"
    )
    tamano_empresa: Mapped[str] = mapped_column(
        String(20), nullable=False, index=True, comment="Company size category"
    )

    # Location
    cve_entidad: Mapped[str] = mapped_column(
        String(2), nullable=False, index=True, comment="State code (entidad federativa)"
    )
    entidad: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="State name"
    )
    municipio: Mapped[Optional[str]] = mapped_column(
        String(100), comment="Municipality name"
    )

    # Geographic Coordinates
    latitud: Mapped[Optional[float]] = mapped_column(Float, comment="Latitude")
    longitud: Mapped[Optional[float]] = mapped_column(Float, comment="Longitude")
    coords_validas: Mapped[bool] = mapped_column(
        Boolean, default=False, comment="Coordinates validation flag"
    )

    # Metadata
    year_carga: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True, comment="Year of data load"
    )
    fecha_procesamiento: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="Processing timestamp"
    )

    # Indexes for common queries
    __table_args__ = (
        Index("ix_establecimiento_location", "cve_entidad", "municipio"),
        Index("ix_establecimiento_activity_year", "codigo_actividad", "year_carga"),
        CheckConstraint("personal_ocupado >= 0", name="ck_personal_ocupado_positive"),
    )

    def __repr__(self) -> str:
        return f"<Establecimiento(clave={self.clave}, nombre={self.nombre_establecimiento[:30]})>"


class ActividadEconomica(Base):
    """
    SCIAN Economic Activity Catalog.

    Stores the catalog of economic activities based on SCIAN classification.
    """

    __tablename__ = "actividades_economicas"

    # Composite Primary Key
    codigo_act: Mapped[str] = mapped_column(
        String(10), primary_key=True, comment="SCIAN activity code"
    )
    nombre_act: Mapped[str] = mapped_column(
        String(500), primary_key=True, comment="Activity description"
    )

    def __repr__(self) -> str:
        return f"<ActividadEconomica(codigo={self.codigo_act}, nombre={self.nombre_act[:30]})>"


class Entidad(Base):
    """
    Mexican States (Entidades Federativas) Catalog.

    Stores the catalog of Mexican states.
    """

    __tablename__ = "entidades"

    cve_ent: Mapped[str] = mapped_column(
        String(2), primary_key=True, comment="State code"
    )
    entidad: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="State name"
    )

    def __repr__(self) -> str:
        return f"<Entidad(cve={self.cve_ent}, nombre={self.entidad})>"


class Municipio(Base):
    """
    Mexican Municipalities Catalog.

    Stores the catalog of municipalities with hierarchical relationship to states.
    """

    __tablename__ = "municipios"

    # Composite Primary Key
    cve_ent: Mapped[str] = mapped_column(
        String(2), primary_key=True, comment="State code"
    )
    cve_mun: Mapped[str] = mapped_column(
        String(3), primary_key=True, comment="Municipality code"
    )

    municipio: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="Municipality name"
    )

    __table_args__ = (Index("ix_municipio_nombre", "municipio"),)

    def __repr__(self) -> str:
        return f"<Municipio(cve_ent={self.cve_ent}, cve_mun={self.cve_mun}, nombre={self.municipio})>"


class Localidad(Base):
    """
    Mexican Localities Catalog.

    Stores the catalog of localities with full geographic hierarchy.
    """

    __tablename__ = "localidades"

    # Composite Primary Key
    cve_ent: Mapped[str] = mapped_column(
        String(2), primary_key=True, comment="State code"
    )
    cve_mun: Mapped[str] = mapped_column(
        String(3), primary_key=True, comment="Municipality code"
    )
    cve_loc: Mapped[str] = mapped_column(
        String(4), primary_key=True, comment="Locality code"
    )

    localidad: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="Locality name"
    )

    __table_args__ = (Index("ix_localidad_nombre", "localidad"),)

    def __repr__(self) -> str:
        return f"<Localidad(cve_ent={self.cve_ent}, cve_mun={self.cve_mun}, cve_loc={self.cve_loc}, nombre={self.localidad})>"
