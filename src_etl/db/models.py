"""
SQLAlchemy ORM Models for DENUE Database.

This module contains all database table definitions using SQLAlchemy 2.0 ORM.
All models inherit from the Base class defined in base.py.

The schema matches the DuckDB warehouse structure with all 38 columns for establecimientos
and proper catalog tables for geographic and economic classifications.
"""

import re
from datetime import date

from sqlalchemy import (
    Date,
    Float,
    Index,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from src_etl.db.base import Base
from src_etl.db.enums import (
    PersonalOcupado,
    TipoUnidadEconomica,
)


class Establecimiento(Base):
    """
    Establecimientos (Business Establishments) from DENUE.

    Represents individual business establishments with complete details including:
    - Identification and economic activity
    - Detailed address components
    - Geographic coordinates and administrative divisions
    - Contact information
    - Temporal tracking (registration date and data period)

    This model matches the actual DuckDB warehouse schema with all 38 columns.
    """

    __tablename__ = "establecimientos"

    # ===== PRIMARY KEY =====
    id: Mapped[str] = mapped_column(
        String, primary_key=True, comment="Unique establishment identifier (hash)"
    )

    # ===== IDENTIFICATION =====
    clee: Mapped[str | None] = mapped_column(
        String, index=True, comment="CLEE - Clave Larga de Establecimiento Económico"
    )
    nom_estab: Mapped[str | None] = mapped_column(String(500), comment="Establishment name")
    raz_social: Mapped[str | None] = mapped_column(
        String(500), comment="Business legal name (razón social)"
    )

    # ===== ECONOMIC ACTIVITY =====
    codigo_act: Mapped[str | None] = mapped_column(
        String(10), index=True, comment="SCIAN economic activity code"
    )

    # ===== ESTABLISHMENT TYPE & SIZE =====
    tipounieco: Mapped[str | None] = mapped_column(
        String(20), comment="Economic unit type: FIJO, SEMIFIJO, NINGUNO"
    )
    per_ocu: Mapped[str | None] = mapped_column(
        String(50), comment="Personnel occupied (employee count range)"
    )

    # ===== ADDRESS COMPONENTS =====
    # Main street/road
    tipo_vial: Mapped[str | None] = mapped_column(
        String(50), comment="Road type (CALLE, AVENIDA, BOULEVARD, etc.)"
    )
    nom_vial: Mapped[str | None] = mapped_column(String(200), comment="Road/street name")

    # Intersecting streets (up to 3)
    tipo_v_e_1: Mapped[str | None] = mapped_column(
        String(50), comment="Type of 1st intersecting street"
    )
    nom_v_e_1: Mapped[str | None] = mapped_column(
        String(200), comment="Name of 1st intersecting street"
    )
    tipo_v_e_2: Mapped[str | None] = mapped_column(
        String(50), comment="Type of 2nd intersecting street"
    )
    nom_v_e_2: Mapped[str | None] = mapped_column(
        String(200), comment="Name of 2nd intersecting street"
    )
    tipo_v_e_3: Mapped[str | None] = mapped_column(
        String(50), comment="Type of 3rd intersecting street"
    )
    nom_v_e_3: Mapped[str | None] = mapped_column(
        String(200), comment="Name of 3rd intersecting street"
    )

    # Building/location details
    numero_ext: Mapped[str | None] = mapped_column(String(20), comment="External number")
    letra_ext: Mapped[str | None] = mapped_column(
        String(10), comment="External number letter/suffix"
    )
    edificio: Mapped[str | None] = mapped_column(String(100), comment="Building name")
    edificio_e: Mapped[str | None] = mapped_column(String(100), comment="Building name (extended)")
    numero_int: Mapped[str | None] = mapped_column(String(20), comment="Internal/apartment number")
    letra_int: Mapped[str | None] = mapped_column(
        String(10), comment="Internal number letter/suffix"
    )

    # Settlement/neighborhood
    tipo_asent: Mapped[str | None] = mapped_column(
        String(50), comment="Settlement type (COLONIA, FRACCIONAMIENTO, etc.)"
    )
    nomb_asent: Mapped[str | None] = mapped_column(
        String(200), comment="Settlement/neighborhood name"
    )

    # Shopping center
    nom_cencom: Mapped[str | None] = mapped_column(String(200), comment="Shopping center name")
    num_local: Mapped[str | None] = mapped_column(
        String(20), comment="Local/store number within shopping center"
    )

    # Postal code
    cod_postal: Mapped[str | None] = mapped_column(
        String(5), index=True, comment="5-digit postal code"
    )

    # ===== GEOGRAPHIC CODES (ADMINISTRATIVE DIVISIONS) =====
    cve_ent: Mapped[str | None] = mapped_column(
        String(2), index=True, comment="State code (entidad federativa)"
    )
    cve_mun: Mapped[str | None] = mapped_column(String(3), index=True, comment="Municipality code")
    cve_loc: Mapped[str | None] = mapped_column(String(4), comment="Locality code")
    ageb: Mapped[str | None] = mapped_column(String(4), comment="AGEB - Área Geoestadística Básica")
    manzana: Mapped[str | None] = mapped_column(String(3), comment="Block (manzana) code")

    # ===== GEOGRAPHIC COORDINATES =====
    latitud: Mapped[float | None] = mapped_column(Float, comment="Latitude (WGS84)")
    longitud: Mapped[float | None] = mapped_column(Float, comment="Longitude (WGS84)")

    # ===== CONTACT INFORMATION =====
    telefono: Mapped[str | None] = mapped_column(String(50), comment="Phone number")
    correoelec: Mapped[str | None] = mapped_column(String(200), comment="Email address")
    www: Mapped[str | None] = mapped_column(String(200), comment="Website URL")

    # ===== TEMPORAL TRACKING =====
    fecha_alta_str: Mapped[str | None] = mapped_column(
        String(10), comment="Registration date in DENUE (any format) as string"
    )
    fecha_alta_date: Mapped[Date | None] = mapped_column(
        Date, comment="Registration date in DENUE (YYYY-MM-01) as Date"
    )
    period: Mapped[date] = mapped_column(Date, index=True, comment="Data period/snapshot date")

    # ===== INDEXES FOR PERFORMANCE =====
    __table_args__ = (
        # Geographic queries (most common)
        Index("ix_estab_geo", "cve_ent", "cve_mun", "cve_loc"),
        Index("ix_estab_geo_simple", "cve_ent", "cve_mun"),
        # Economic activity + time analysis
        Index("ix_estab_activity_period", "codigo_act", "period"),
        # Composite searches (state + activity + size)
        Index("ix_estab_composite", "cve_ent", "codigo_act", "per_ocu"),
        # Postal code lookups
        Index("ix_estab_postal", "cod_postal"),
        # Coordinates for spatial queries
        Index("ix_estab_coords", "latitud", "longitud"),
        # Business type filtering
        Index("ix_estab_type_size", "tipounieco", "per_ocu"),
        # CLEE lookups
        Index("ix_estab_clee", "clee"),
    )

    # ===== HELPER METHODS =====

    def get_full_address(self, include_postal: bool = True) -> str:
        """
        Format complete address from individual components.

        Args:
            include_postal: Include postal code in formatted address

        Returns:
            Formatted address string

        Examples:
            >>> estab.get_full_address()
            'CALLE JOSÉ MARÍA MORELOS #509, COLONIA ZONA CENTRO, C.P. 25950'
        """
        parts = []

        # Main street
        if self.tipo_vial and self.nom_vial:
            street = f"{self.tipo_vial} {self.nom_vial}"
            if self.numero_ext:
                street += f" #{self.numero_ext}"
                if self.letra_ext:
                    street += self.letra_ext
            parts.append(street)

        # Building/interior
        building_parts = []
        if self.edificio:
            building_parts.append(f"EDIFICIO {self.edificio}")
        if self.numero_int:
            int_num = f"INT. {self.numero_int}"
            if self.letra_int:
                int_num += self.letra_int
            building_parts.append(int_num)
        if building_parts:
            parts.append(", ".join(building_parts))

        # Settlement
        if self.tipo_asent and self.nomb_asent:
            parts.append(f"{self.tipo_asent} {self.nomb_asent}")

        # Shopping center
        if self.nom_cencom:
            center = f"CENTRO COMERCIAL {self.nom_cencom}"
            if self.num_local:
                center += f", LOCAL {self.num_local}"
            parts.append(center)

        # Postal code
        if include_postal and self.cod_postal:
            parts.append(f"C.P. {self.cod_postal}")

        return ", ".join(parts) if parts else "Dirección no disponible"

    def get_cross_streets(self) -> list[str]:
        """
        Get list of intersecting streets.

        Returns:
            List of formatted cross street names (up to 3)

        Examples:
            >>> estab.get_cross_streets()
            ['CALLE NICOLÁS BRAVO', 'CALLE GENERAL CEPEDA']
        """
        streets = []
        for i in range(1, 4):
            tipo = getattr(self, f"tipo_v_e_{i}")
            nombre = getattr(self, f"nom_v_e_{i}")
            if tipo and nombre and tipo != "NINGUNO":
                streets.append(f"{tipo} {nombre}")
        return streets

    @property
    def is_valid_location(self) -> bool:
        """
        Check if geographic coordinates are valid and within Mexico bounds.

        Returns:
            True if coordinates are valid, False otherwise

        Examples:
            >>> estab.is_valid_location
            True
        """
        if self.latitud is None or self.longitud is None:
            return False

        # Mexico geographical bounds (approximate)
        # Latitude: 14.5° N to 32.7° N
        # Longitude: 86.7° W to 118.4° W (negative values)
        return 14.5 <= self.latitud <= 32.7 and -118.4 <= self.longitud <= -86.7

    def get_employee_range(self) -> tuple[int, int]:
        """
        Parse employee count ENUM into numeric range.

        Returns:
            Tuple of (minimum, maximum) employee count

        Examples:
            >>> estab.get_employee_range()
            (11, 30)
        """
        if not self.per_ocu:
            return (0, 0)

        try:
            enum_val = PersonalOcupado(self.per_ocu)
            return enum_val.get_range()
        except ValueError:
            return (0, 0)

    def get_business_size(self) -> str:
        """
        Get business size category based on employee count.

        Returns:
            Business size: "Micro", "Small", "Medium", "Large", or "Unknown"

        Examples:
            >>> estab.get_business_size()
            'Small'
        """
        if not self.per_ocu:
            return "Unknown"

        try:
            enum_val = PersonalOcupado(self.per_ocu)
            return enum_val.get_size_category()
        except ValueError:
            return "Unknown"

    def get_scian_sector(self) -> str | None:
        """
        Extract SCIAN sector (2-digit code) from full activity code.

        Returns:
            2-digit SCIAN sector code or None

        Examples:
            >>> estab.get_scian_sector()  # codigo_act = "722515"
            '72'
        """
        if not self.codigo_act or len(self.codigo_act) < 2:
            return None
        return self.codigo_act[:2]

    def get_scian_subsector(self) -> str | None:
        """
        Extract SCIAN subsector (3-digit code) from full activity code.

        Returns:
            3-digit SCIAN subsector code or None

        Examples:
            >>> estab.get_scian_subsector()  # codigo_act = "722515"
            '722'
        """
        if not self.codigo_act or len(self.codigo_act) < 3:
            return None
        return self.codigo_act[:3]

    def is_fixed_establishment(self) -> bool:
        """
        Check if establishment is fixed (permanent) vs semi-fixed (temporary).

        Returns:
            True if fixed establishment, False otherwise
        """
        return self.tipounieco == TipoUnidadEconomica.FIJO.value

    def has_contact_info(self) -> bool:
        """
        Check if establishment has any contact information.

        Returns:
            True if phone, email, or website is available
        """
        return bool(self.telefono or self.correoelec or self.www)

    def get_registration_year(self) -> int | None:
        """
        Extract year from fecha_alta_str (registration date).

        Returns:
            Registration year as integer or None

        Examples:
            >>> estab.get_registration_year()  # fecha_alta_str = "2010-07"
            2010
        """
        if not self.fecha_alta_str:
            return None

        # fecha_alta_str format is typically "YYYY-MM"
        match = re.match(r"(\d{4})", self.fecha_alta_str)
        if match:
            return int(match.group(1))
        return None

    def get_geo_hierarchy(self) -> dict[str, str | None]:
        """
        Get complete geographic administrative hierarchy.

        Returns:
            Dictionary with state, municipality, locality, AGEB, and block codes

        Examples:
            >>> estab.get_geo_hierarchy()
            {'estado': '05', 'municipio': '011', 'localidad': '0001', 'ageb': '0469', 'manzana': '004'}
        """
        return {
            "estado": self.cve_ent,
            "municipio": self.cve_mun,
            "localidad": self.cve_loc,
            "ageb": self.ageb,
            "manzana": self.manzana,
        }

    def __repr__(self) -> str:
        return f"<Establecimiento(id={self.id[:16]}..., nom_estab={self.nom_estab[:30] if self.nom_estab else 'N/A'})>"


class ActividadEconomica(Base):
    """
    SCIAN Economic Activity Catalog.

    Stores the catalog of economic activities based on SCIAN classification.
    """

    __tablename__ = "denue_actividades_economicas"

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

    __tablename__ = "denue_entidades"

    cve_ent: Mapped[str] = mapped_column(String(2), primary_key=True, comment="State code")
    entidad: Mapped[str] = mapped_column(String(100), nullable=False, comment="State name")

    def __repr__(self) -> str:
        return f"<Entidad(cve={self.cve_ent}, nombre={self.entidad})>"


class Municipio(Base):
    """
    Mexican Municipalities Catalog.

    Stores the catalog of municipalities with hierarchical relationship to states.
    """

    __tablename__ = "denue_municipios"

    # Composite Primary Key
    cve_ent: Mapped[str] = mapped_column(String(2), primary_key=True, comment="State code")
    cve_mun: Mapped[str] = mapped_column(String(3), primary_key=True, comment="Municipality code")

    municipio: Mapped[str] = mapped_column(String(100), nullable=False, comment="Municipality name")

    __table_args__ = (Index("ix_municipio_nombre", "municipio"),)

    def __repr__(self) -> str:
        return (
            f"<Municipio(cve_ent={self.cve_ent}, cve_mun={self.cve_mun}, nombre={self.municipio})>"
        )


class Localidad(Base):
    """
    Mexican Localities Catalog.

    Stores the catalog of localities with full geographic hierarchy.
    """

    __tablename__ = "denue_localidades"

    # Composite Primary Key
    cve_ent: Mapped[str] = mapped_column(String(2), primary_key=True, comment="State code")
    cve_mun: Mapped[str] = mapped_column(String(3), primary_key=True, comment="Municipality code")
    cve_loc: Mapped[str] = mapped_column(String(4), primary_key=True, comment="Locality code")

    localidad: Mapped[str] = mapped_column(String(100), nullable=False, comment="Locality name")

    __table_args__ = (Index("ix_localidad_nombre", "localidad"),)

    def __repr__(self) -> str:
        return f"<Localidad(cve_ent={self.cve_ent}, cve_mun={self.cve_mun}, cve_loc={self.cve_loc}, nombre={self.localidad})>"
