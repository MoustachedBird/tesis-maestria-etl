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
    BigInteger,
    Boolean,
    Date,
    Double,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.db.base import Base


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
    clee_propagado_radio: Mapped[str | None] = mapped_column(
        String,
        index=True,
        comment="Resolved CLEE: official INEGI CLEE or generated via entity resolution",
    )
    clee_propagado_h3: Mapped[str | None] = mapped_column(
        String,
        comment=(
            "Resolved CLEE via H3 cell blocking (comparison track vs clee_propagado_radio). "
            "Deliberately unindexed: ART maintenance makes massive resolution "
            "writes ~1000x slower; index after resolution if needed"
        ),
    )
    nom_estab: Mapped[str | None] = mapped_column(String(500), comment="Establishment name")
    raz_social: Mapped[str | None] = mapped_column(
        String(500), comment="Business legal name (razón social)"
    )

    # ===== ECONOMIC ACTIVITY =====
    codigo_act: Mapped[str | None] = mapped_column(
        String(10),
        ForeignKey("denue_actividades_economicas.codigo_act"),
        index=True,
        comment="SCIAN economic activity code (FK real al catalogo)",
    )

    # ===== ESTABLISHMENT TYPE & SIZE (FKs reales a catalogos de dominio) =====
    tipounieco_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("denue_tipos_unidad_economica.id"),
        comment="FK a denue_tipos_unidad_economica (antes label tipounieco)",
    )
    per_ocu_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("denue_estratos_personal.id"),
        comment="FK a denue_estratos_personal (antes label per_ocu)",
    )

    # ===== ADDRESS COMPONENTS =====
    # Main street/road
    tipo_vial_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("denue_tipos_vialidad.id"),
        comment="FK a denue_tipos_vialidad (antes label tipo_vial)",
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
    tipo_asent_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("denue_tipos_asentamiento.id"),
        comment="FK a denue_tipos_asentamiento (antes label tipo_asent)",
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
    # Double (no Float): el DDL canonico del writer usa DOUBLE y FLOAT de
    # precision simple redondea las coordenadas ~4 cm, ademas de dejar el
    # ORM desalineado con la tabla real.
    latitud: Mapped[float | None] = mapped_column(Double, comment="Latitude (WGS84)")
    longitud: Mapped[float | None] = mapped_column(Double, comment="Longitude (WGS84)")

    # ===== H3 SPATIAL INDEX (CLEE resolution blocking) =====
    h3_cell_9: Mapped[int | None] = mapped_column(
        BigInteger,
        comment="H3 cell index at resolution 9 (~174m edge) for spatial blocking",
    )
    h3_cell_5: Mapped[int | None] = mapped_column(
        BigInteger,
        comment="H3 cell index at resolution 5 (~8.5km edge) for spatial partitioning",
    )

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

    # ===== CONSTRAINTS & INDEXES =====
    # FKs de geografia como constraints REALES tambien en el ORM (mismo
    # esquema que el DDL del writer); los FKs de dominio van inline en sus
    # mapped_column de arriba. Decision del usuario (2026-07-17): relaciones
    # fisicas por id. Costo asumido y documentado en AGENTS.md: los FKs en
    # DuckDB implican mantenimiento de indice en escrituras masivas.
    __table_args__ = (
        ForeignKeyConstraint(["cve_ent"], ["denue_entidades.cve_ent"]),
        ForeignKeyConstraint(
            ["cve_ent", "cve_mun"],
            ["denue_municipios.cve_ent", "denue_municipios.cve_mun"],
        ),
        ForeignKeyConstraint(
            ["cve_ent", "cve_mun", "cve_loc"],
            [
                "denue_localidades.cve_ent",
                "denue_localidades.cve_mun",
                "denue_localidades.cve_loc",
            ],
        ),
        # Geographic queries (most common).
        # No ix_estab_geo_simple ("cve_ent", "cve_mun"): es PREFIJO de este
        # indice, asi que el planner resuelve con el compuesto y el corto
        # solo costaba un ART extra que mantener (ver migracion a4000idx).
        Index("ix_estab_geo", "cve_ent", "cve_mun", "cve_loc"),
        # Economic activity + time analysis
        Index("ix_estab_activity_period", "codigo_act", "period"),
        # Composite searches (state + activity + size)
        Index("ix_estab_composite", "cve_ent", "codigo_act", "per_ocu_id"),
        # Coordinates for spatial queries
        Index("ix_estab_coords", "latitud", "longitud"),
        # Business type filtering
        Index("ix_estab_type_size", "tipounieco_id", "per_ocu_id"),
        # H3 blocking key (CLEE resolution)
        Index("ix_estab_h3_9", "h3_cell_9"),
        # NO declarar aqui indices de UNA columna que ya trae `index=True`
        # en su mapped_column: SQLAlchemy genera ix_establecimientos_<col> y
        # este bloque generaria un SEGUNDO ART sobre el mismo campo. Asi
        # nacieron ix_estab_clee (clee) e ix_estab_postal (cod_postal),
        # eliminados en la migracion a4000idx.
    )

    # ===== RELATIONSHIPS (derivadas de los FKs reales) =====
    # Las de dominio/actividad se infieren de su ForeignKey. Las de
    # geografia son viewonly porque comparten columnas (cve_ent aparece en
    # los 3 FKs compuestos) y solo se navegan, no se asignan.

    actividad: Mapped["ActividadEconomica | None"] = relationship()
    estrato: Mapped["EstratoPersonal | None"] = relationship()
    tipo_vialidad: Mapped["TipoVialidad | None"] = relationship()
    tipo_asentamiento: Mapped["TipoAsentamiento | None"] = relationship()
    tipo_unidad: Mapped["TipoUnidadEconomica | None"] = relationship()
    entidad: Mapped["Entidad | None"] = relationship(viewonly=True)
    municipio: Mapped["Municipio | None"] = relationship(viewonly=True)
    localidad: Mapped["Localidad | None"] = relationship(viewonly=True)

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

        # Main street (el label del tipo viene del catalogo via relationship;
        # None si el objeto no esta ligado a una sesion o no tiene tipo)
        tipo_vial = self.tipo_vialidad.tipo_vial if self.tipo_vialidad else None
        if self.nom_vial:
            street = f"{tipo_vial} {self.nom_vial}" if tipo_vial else self.nom_vial
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
        tipo_asent = self.tipo_asentamiento.tipo_asent if self.tipo_asentamiento else None
        if self.nomb_asent:
            parts.append(f"{tipo_asent} {self.nomb_asent}" if tipo_asent else self.nomb_asent)

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

    # NOTA: los antiguos helpers get_employee_range / get_business_size /
    # get_scian_sector / get_scian_subsector se eliminaron: esa semantica
    # vive ahora en los catalogos (denue_estratos_personal.empleados_min/
    # empleados_max/categoria y denue_actividades_economicas.sector/
    # subsector/rama), navegables via los relationships `estrato` y
    # `actividad` o por JOIN en SQL.

    def is_fixed_establishment(self) -> bool:
        """
        Check if establishment is fixed (permanent) vs semi-fixed (temporary).

        Resuelve la etiqueta navegando el catalogo (`tipo_unidad`), que es la
        fuente de verdad del dominio: no hay una copia del dominio en Python
        contra la cual comparar. Requiere que el objeto este ligado a una
        sesion (o el catalogo precargado); si el FK es NULL devuelve False.

        Returns:
            True if fixed establishment, False otherwise
        """
        return self.tipo_unidad is not None and self.tipo_unidad.tipounieco == "FIJO"

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
    PK is `codigo_act` alone (nombre_act depends functionally on it).
    The SCIAN hierarchy (sector/subsector/rama) is materialized as columns
    derived from the 6-digit code prefix, so it is queryable in plain SQL.
    """

    __tablename__ = "denue_actividades_economicas"

    codigo_act: Mapped[str] = mapped_column(
        String(10), primary_key=True, comment="SCIAN activity code (6 digits)"
    )
    nombre_act: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="Activity description"
    )
    sector: Mapped[str] = mapped_column(
        String(2), nullable=False, index=True, comment="SCIAN sector (first 2 digits)"
    )
    subsector: Mapped[str] = mapped_column(
        String(3), nullable=False, comment="SCIAN subsector (first 3 digits)"
    )
    rama: Mapped[str] = mapped_column(
        String(4), nullable=False, comment="SCIAN rama (first 4 digits)"
    )

    def __repr__(self) -> str:
        return f"<ActividadEconomica(codigo={self.codigo_act}, nombre={self.nombre_act[:30]})>"


class EstratoPersonal(Base):
    """
    Personnel Size Strata Catalog.

    Materializes the INEGI employee-count ranges (per_ocu) with their numeric
    bounds and business-size category, making SME classification queryable in
    plain SQL. Sembrado desde `data/seeds/seed_estratos_personal.parquet`
    (fuente de verdad del catalogo).
    """

    __tablename__ = "denue_estratos_personal"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=False,  # ids explicitos (posicion en el enum), no secuencia
        comment="Surrogate id (del seed parquet; append-only: los hechos lo referencian)",
    )
    per_ocu: Mapped[str] = mapped_column(
        String(50), nullable=False, unique=True, comment="Employee count range label (INEGI)"
    )
    empleados_min: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Minimum employees in range"
    )
    empleados_max: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Maximum employees in range (999999 = open-ended)"
    )
    categoria: Mapped[str] = mapped_column(
        String(10), nullable=False, comment="Business size: Micro, Small, Medium, Large, Unknown"
    )

    def __repr__(self) -> str:
        return f"<EstratoPersonal(per_ocu={self.per_ocu}, categoria={self.categoria})>"


class TipoVialidad(Base):
    """
    Road/Street Type Domain Catalog.

    Normalizes the old `tipo_vial_type` DuckDB ENUM into a catalog table:
    `establecimientos.tipo_vial_id` references `id` with a real FK. Sembrado
    desde `data/seeds/seed_tipos_vialidad.parquet` (fuente de verdad).
    """

    __tablename__ = "denue_tipos_vialidad"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=False,  # ids explicitos (posicion en el enum), no secuencia
        comment="Surrogate id (del seed parquet; append-only)",
    )
    tipo_vial: Mapped[str] = mapped_column(
        String(50), nullable=False, unique=True, comment="Road type label (INEGI)"
    )
    descripcion: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="Human-readable meaning of the road type"
    )

    def __repr__(self) -> str:
        return f"<TipoVialidad({self.id}: {self.tipo_vial})>"


class TipoAsentamiento(Base):
    """
    Settlement Type Domain Catalog.

    Normalizes the old `tipo_asent_type` DuckDB ENUM into a catalog table:
    `establecimientos.tipo_asent_id` references `id` with a real FK. Sembrado
    desde `data/seeds/seed_tipos_asentamiento.parquet` (fuente de verdad).
    """

    __tablename__ = "denue_tipos_asentamiento"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=False,  # ids explicitos (posicion en el enum), no secuencia
        comment="Surrogate id (del seed parquet; append-only)",
    )
    tipo_asent: Mapped[str] = mapped_column(
        String(50), nullable=False, unique=True, comment="Settlement type label (INEGI)"
    )
    descripcion: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="Human-readable meaning of the settlement type"
    )

    def __repr__(self) -> str:
        return f"<TipoAsentamiento({self.id}: {self.tipo_asent})>"


class TipoUnidadEconomica(Base):
    """
    Economic Unit Type Domain Catalog.

    Normalizes the old `tipounieco_type` DuckDB ENUM into a catalog table:
    `establecimientos.tipounieco_id` references `id` with a real FK. The
    permanence semantics (FIJO/SEMIFIJO) live in the queryable `descripcion`
    column instead of Python-only comments. Sembrado desde
    `data/seeds/seed_tipos_unidad_economica.parquet` (fuente de verdad).
    """

    __tablename__ = "denue_tipos_unidad_economica"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=False,  # ids explicitos (posicion en el enum), no secuencia
        comment="Surrogate id (del seed parquet; append-only)",
    )
    tipounieco: Mapped[str] = mapped_column(
        String(20), nullable=False, unique=True, comment="Economic unit type label (INEGI)"
    )
    descripcion: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="Human-readable permanence semantics"
    )

    def __repr__(self) -> str:
        return f"<TipoUnidadEconomica({self.id}: {self.tipounieco})>"


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


class SesnspDelito(Base):
    """
    SESNSP Crime Catalog.

    Hierarchy bien_juridico -> tipo -> subtipo -> modalidad from the SESNSP
    municipal crime incidence dataset. Surrogate ids are append-only
    (materialized in data/seeds/seed_sesnsp_delitos.parquet) because the
    fact table references them by FK.
    """

    __tablename__ = "sesnsp_delitos"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=False,  # ids explicitos (orden del seed parquet), no secuencia
        comment="Surrogate id (seed order)",
    )
    bien_juridico: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="Affected legal good (bien juridico afectado)"
    )
    tipo: Mapped[str] = mapped_column(String(100), nullable=False, comment="Crime type")
    subtipo: Mapped[str] = mapped_column(String(150), nullable=False, comment="Crime subtype")
    modalidad: Mapped[str] = mapped_column(String(150), nullable=False, comment="Crime modality")

    __table_args__ = (
        UniqueConstraint("bien_juridico", "tipo", "subtipo", "modalidad", name="uq_sesnsp_delito"),
    )

    def __repr__(self) -> str:
        return f"<SesnspDelito(id={self.id}, subtipo={self.subtipo}, modalidad={self.modalidad})>"


class SesnspIncidenciaMunicipal(Base):
    """
    SESNSP Municipal Crime Incidence (monthly fact table).

    Investigation folders (carpetas de investigacion) per municipality x
    crime x month, unpivoted to long format from the SESNSP consolidated
    CSV. Shares the DENUE geographic keys (cve_ent/cve_mun) so business
    survival can be joined with crime incidence per municipality over time;
    `fecha` follows the project-wide DATE YYYY-MM-01 convention.
    """

    __tablename__ = "sesnsp_incidencia_municipal"

    cve_ent: Mapped[str] = mapped_column(String(2), primary_key=True, comment="State code")
    cve_mun: Mapped[str] = mapped_column(String(3), primary_key=True, comment="Municipality code")
    delito_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sesnsp_delitos.id"), primary_key=True, comment="Crime catalog FK"
    )
    fecha: Mapped[date] = mapped_column(Date, primary_key=True, comment="Month (YYYY-MM-01)")
    carpetas: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Investigation folders opened in the month"
    )

    __table_args__ = (
        ForeignKeyConstraint(["cve_ent"], ["denue_entidades.cve_ent"]),
        ForeignKeyConstraint(
            ["cve_ent", "cve_mun"],
            ["denue_municipios.cve_ent", "denue_municipios.cve_mun"],
        ),
    )

    # Relationships
    delito: Mapped["SesnspDelito"] = relationship()
    municipio: Mapped["Municipio"] = relationship()

    def __repr__(self) -> str:
        return (
            f"<SesnspIncidenciaMunicipal(mun={self.cve_ent}{self.cve_mun}, "
            f"delito_id={self.delito_id}, fecha={self.fecha}, carpetas={self.carpetas})>"
        )


class EntidadEconomica(Base):
    """
    Economic Entity Dimension (N3) -- one row per resolved business.

    Collapses per-period observations in `establecimientos` to one row per
    resolved identity (`clee_propagado_radio`): attributes from the most
    recent period plus the observed lifespan (primer/ultimo periodo). This
    is the survival/mortality unit of analysis for the thesis.

    Fully DERIVED artifact: rebuilt from scratch after each CLEE
    resolution run (NEW_* synthetic ids are not stable across --force
    runs). Deliberately carries no FKs to the catalogs -- the fact table
    already enforces those domains and the dimension is bulk-rebuilt.
    Built by `loaders/entidades.py:build_entidades_economicas()`.
    """

    __tablename__ = "entidades_economicas"

    clee_propagado_radio: Mapped[str] = mapped_column(
        String(50), primary_key=True, comment="Resolved identity (CLEE or NEW_* synthetic)"
    )
    es_sintetica: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="True if NEW_* synthetic id (no match found)"
    )
    nom_estab: Mapped[str | None] = mapped_column(
        String(200), comment="Business name (most recent period)"
    )
    raz_social: Mapped[str | None] = mapped_column(
        String(200), comment="Legal name (most recent period)"
    )
    codigo_act: Mapped[str | None] = mapped_column(
        String(10), comment="SCIAN activity (most recent period)"
    )
    cve_ent: Mapped[str | None] = mapped_column(
        String(2), comment="State code (most recent period)"
    )
    cve_mun: Mapped[str | None] = mapped_column(
        String(3), comment="Municipality code (most recent period)"
    )
    cod_postal: Mapped[str | None] = mapped_column(
        String(10), comment="Postal code (most recent period)"
    )
    latitud: Mapped[float | None] = mapped_column(Double, comment="Latitude (most recent period)")
    longitud: Mapped[float | None] = mapped_column(
        Double, comment="Longitude (most recent period)"
    )
    h3_cell_9: Mapped[int | None] = mapped_column(
        BigInteger, comment="H3 res-9 cell (most recent period)"
    )
    primer_periodo: Mapped[date] = mapped_column(
        Date, nullable=False, comment="First period the entity was observed"
    )
    ultimo_periodo: Mapped[date] = mapped_column(
        Date, nullable=False, comment="Last period the entity was observed"
    )
    n_periodos: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Number of distinct periods observed"
    )
    fecha_alta_date: Mapped[date | None] = mapped_column(
        Date, comment="Earliest known INEGI registration date"
    )

    def __repr__(self) -> str:
        return (
            f"<EntidadEconomica(clee={self.clee_propagado_radio}, "
            f"{self.primer_periodo}..{self.ultimo_periodo}, n={self.n_periodos})>"
        )
