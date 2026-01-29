"""db schema to match denue csv files

This migration creates the complete database schema for DENUE (Directorio Estadístico
Nacional de Unidades Económicas) data from INEGI.

The schema includes:
- Main establishments table with all 38 columns from DENUE CSV files
- Geographic catalog tables (states, municipalities, localities)
- Economic activity catalog table (SCIAN classification)

Design considerations:
- All catalog tables use composite primary keys matching INEGI's hierarchical codes
- Indexes optimized for common query patterns (geographic, activity, temporal)
- VARCHAR types used instead of ENUM for database portability (DuckDB/PostgreSQL)
- Schema is compatible with both DuckDB (development) and PostgreSQL (production)

Revision ID: cbc954c59583
Revises:
Create Date: 2026-01-29 01:28:57.388700

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "cbc954c59583"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Create complete DENUE database schema.

    Creates tables for:
    1. Economic establishments (establecimientos) - main data table
    2. Economic activities catalog (denue_actividades_economicas)
    3. Geographic catalogs (states, municipalities, localities)
    """

    # =========================================================================
    # TABLE 1: establecimientos (Main Business Establishments Data)
    # =========================================================================
    # This table contains all business establishment records from DENUE with
    # complete details including identification, location, economic activity,
    # and contact information.
    #
    # Total columns: 38 (matching DENUE CSV schema)
    # Primary key: id (hash generated from establishment data)
    # =========================================================================

    op.create_table(
        "establecimientos",
        # === PRIMARY KEY ===
        # Unique identifier generated as hash of establishment data
        sa.Column(
            "id",
            sa.String(),
            nullable=False,
            comment="Unique establishment identifier (hash)",
        ),
        # === IDENTIFICATION ===
        # CLEE: Clave Larga de Establecimiento Económico (Long Economic Establishment Key)
        # This is INEGI's official unique identifier for establishments
        sa.Column(
            "clee",
            sa.String(),
            nullable=True,
            comment="CLEE - Clave Larga de Establecimiento Económico",
        ),
        sa.Column(
            "nom_estab",
            sa.String(length=500),
            nullable=True,
            comment="Establishment name",
        ),
        sa.Column(
            "raz_social",
            sa.String(length=500),
            nullable=True,
            comment="Business legal name (razón social)",
        ),
        # === ECONOMIC ACTIVITY ===
        # SCIAN: Sistema de Clasificación Industrial de América del Norte
        # (North American Industry Classification System)
        # codigo_act contains 2-6 digit SCIAN codes
        sa.Column(
            "codigo_act",
            sa.String(length=10),
            nullable=True,
            comment="SCIAN economic activity code",
        ),
        # === ESTABLISHMENT TYPE & SIZE ===
        # tipounieco: Type of economic unit
        #   - FIJO: Fixed/permanent establishment
        #   - SEMIFIJO: Semi-fixed/temporary establishment (street vendors, etc.)
        #   - NINGUNO: None/not specified
        sa.Column(
            "tipounieco",
            sa.String(length=20),
            nullable=True,
            comment="Economic unit type: FIJO, SEMIFIJO, NINGUNO",
        ),
        # per_ocu: Personal ocupado (Employee count range)
        # Values like: "0 a 5", "6 a 10", "11 a 30", "31 a 50", etc.
        sa.Column(
            "per_ocu",
            sa.String(length=50),
            nullable=True,
            comment="Personnel occupied (employee count range)",
        ),
        # === ADDRESS COMPONENTS ===
        # Mexican addresses follow a specific structure with multiple components
        # Main street/road information
        # tipo_vial: Road type (CALLE, AVENIDA, BOULEVARD, CARRETERA, etc.)
        sa.Column(
            "tipo_vial",
            sa.String(length=50),
            nullable=True,
            comment="Road type (CALLE, AVENIDA, BOULEVARD, etc.)",
        ),
        sa.Column(
            "nom_vial", sa.String(length=200), nullable=True, comment="Road/street name"
        ),
        # Intersecting streets (up to 3)
        # Used to provide precise location: "between street X and street Y"
        sa.Column(
            "tipo_v_e_1",
            sa.String(length=50),
            nullable=True,
            comment="Type of 1st intersecting street",
        ),
        sa.Column(
            "nom_v_e_1",
            sa.String(length=200),
            nullable=True,
            comment="Name of 1st intersecting street",
        ),
        sa.Column(
            "tipo_v_e_2",
            sa.String(length=50),
            nullable=True,
            comment="Type of 2nd intersecting street",
        ),
        sa.Column(
            "nom_v_e_2",
            sa.String(length=200),
            nullable=True,
            comment="Name of 2nd intersecting street",
        ),
        sa.Column(
            "tipo_v_e_3",
            sa.String(length=50),
            nullable=True,
            comment="Type of 3rd intersecting street",
        ),
        sa.Column(
            "nom_v_e_3",
            sa.String(length=200),
            nullable=True,
            comment="Name of 3rd intersecting street",
        ),
        # Building/location details
        sa.Column(
            "numero_ext", sa.String(length=20), nullable=True, comment="External number"
        ),
        sa.Column(
            "letra_ext",
            sa.String(length=10),
            nullable=True,
            comment="External number letter/suffix",
        ),
        sa.Column(
            "edificio", sa.String(length=100), nullable=True, comment="Building name"
        ),
        sa.Column(
            "edificio_e",
            sa.String(length=100),
            nullable=True,
            comment="Building name (extended)",
        ),
        sa.Column(
            "numero_int",
            sa.String(length=20),
            nullable=True,
            comment="Internal/apartment number",
        ),
        sa.Column(
            "letra_int",
            sa.String(length=10),
            nullable=True,
            comment="Internal number letter/suffix",
        ),
        # Settlement/neighborhood information
        # tipo_asent: Settlement type (COLONIA, FRACCIONAMIENTO, BARRIO, etc.)
        sa.Column(
            "tipo_asent",
            sa.String(length=50),
            nullable=True,
            comment="Settlement type (COLONIA, FRACCIONAMIENTO, etc.)",
        ),
        sa.Column(
            "nomb_asent",
            sa.String(length=200),
            nullable=True,
            comment="Settlement/neighborhood name",
        ),
        # Shopping center information
        # For establishments within malls/shopping centers
        sa.Column(
            "nom_cencom",
            sa.String(length=200),
            nullable=True,
            comment="Shopping center name",
        ),
        sa.Column(
            "num_local",
            sa.String(length=20),
            nullable=True,
            comment="Local/store number within shopping center",
        ),
        # Postal code (5 digits)
        sa.Column(
            "cod_postal",
            sa.String(length=5),
            nullable=True,
            comment="5-digit postal code",
        ),
        # === GEOGRAPHIC CODES (ADMINISTRATIVE DIVISIONS) ===
        # Mexico's hierarchical geographic division system:
        # State (Entidad) → Municipality (Municipio) → Locality (Localidad) → AGEB → Block (Manzana)
        # cve_ent: 2-digit state code (01-32)
        # Examples: 01=Aguascalientes, 09=CDMX, 19=Nuevo León
        sa.Column(
            "cve_ent",
            sa.String(length=2),
            nullable=True,
            comment="State code (entidad federativa)",
        ),
        # cve_mun: 3-digit municipality code (unique within state)
        sa.Column(
            "cve_mun", sa.String(length=3), nullable=True, comment="Municipality code"
        ),
        # cve_loc: 4-digit locality code (unique within municipality)
        sa.Column(
            "cve_loc", sa.String(length=4), nullable=True, comment="Locality code"
        ),
        # ageb: Área Geoestadística Básica (Basic Geostatistical Area)
        # AGEB is INEGI's fundamental geographic unit for census data
        sa.Column(
            "ageb",
            sa.String(length=4),
            nullable=True,
            comment="AGEB - Área Geoestadística Básica",
        ),
        # manzana: City block code
        sa.Column(
            "manzana",
            sa.String(length=3),
            nullable=True,
            comment="Block (manzana) code",
        ),
        # === GEOGRAPHIC COORDINATES ===
        # WGS84 decimal degrees format
        # Mexico bounds: ~14.5°N to 32.7°N, ~86.7°W to 118.4°W
        sa.Column("latitud", sa.Float(), nullable=True, comment="Latitude (WGS84)"),
        sa.Column("longitud", sa.Float(), nullable=True, comment="Longitude (WGS84)"),
        # === CONTACT INFORMATION ===
        sa.Column(
            "telefono", sa.String(length=50), nullable=True, comment="Phone number"
        ),
        sa.Column(
            "correoelec", sa.String(length=200), nullable=True, comment="Email address"
        ),
        sa.Column("www", sa.String(length=200), nullable=True, comment="Website URL"),
        # === TEMPORAL TRACKING ===
        # fecha_alta: Registration date in DENUE (format: "YYYY-MM")
        # When the establishment was first added to DENUE
        sa.Column(
            "fecha_alta",
            sa.String(length=10),
            nullable=True,
            comment="Registration date in DENUE (YYYY-MM format)",
        ),
        # period: Data period/snapshot date
        # The date when this data was collected/published (format: DATE)
        sa.Column(
            "period", sa.Date(), nullable=True, comment="Data period/snapshot date"
        ),
        # === PRIMARY KEY CONSTRAINT ===
        sa.PrimaryKeyConstraint("id", name=op.f("pk_establecimientos")),
    )

    # === INDEXES FOR PERFORMANCE ===
    # These indexes optimize common query patterns in DENUE data analysis

    # Geographic queries (most common pattern)
    # Query pattern: "Find all establishments in state X, municipality Y"
    op.create_index(
        op.f("ix_estab_geo"),
        "establecimientos",
        ["cve_ent", "cve_mun", "cve_loc"],
        unique=False,
    )
    op.create_index(
        op.f("ix_estab_geo_simple"),
        "establecimientos",
        ["cve_ent", "cve_mun"],
        unique=False,
    )

    # Economic activity analysis
    # Query pattern: "Analyze activity X over time"
    op.create_index(
        op.f("ix_estab_activity_period"),
        "establecimientos",
        ["codigo_act", "period"],
        unique=False,
    )

    # Composite searches
    # Query pattern: "Find large businesses in sector X in state Y"
    op.create_index(
        op.f("ix_estab_composite"),
        "establecimientos",
        ["cve_ent", "codigo_act", "per_ocu"],
        unique=False,
    )

    # Postal code lookups
    op.create_index(
        op.f("ix_estab_postal"), "establecimientos", ["cod_postal"], unique=False
    )

    # Spatial queries (GIS analysis)
    # Query pattern: "Find establishments near coordinates (lat, lon)"
    op.create_index(
        op.f("ix_estab_coords"),
        "establecimientos",
        ["latitud", "longitud"],
        unique=False,
    )

    # Business type and size filtering
    op.create_index(
        op.f("ix_estab_type_size"),
        "establecimientos",
        ["tipounieco", "per_ocu"],
        unique=False,
    )

    # CLEE lookups (official identifier)
    op.create_index(op.f("ix_estab_clee"), "establecimientos", ["clee"], unique=False)

    # Single-column indexes (already defined via SQLAlchemy model)
    op.create_index(
        op.f("ix_establecimientos_codigo_act"),
        "establecimientos",
        ["codigo_act"],
        unique=False,
    )
    op.create_index(
        op.f("ix_establecimientos_cve_ent"),
        "establecimientos",
        ["cve_ent"],
        unique=False,
    )
    op.create_index(
        op.f("ix_establecimientos_cve_mun"),
        "establecimientos",
        ["cve_mun"],
        unique=False,
    )
    op.create_index(
        op.f("ix_establecimientos_period"), "establecimientos", ["period"], unique=False
    )

    # =========================================================================
    # TABLE 2: denue_actividades_economicas (Economic Activities Catalog)
    # =========================================================================
    # SCIAN catalog: Sistema de Clasificación Industrial de América del Norte
    # Contains the hierarchical classification of economic activities
    #
    # Structure:
    # - 2 digits: Sector (e.g., 72 = Accommodation and food services)
    # - 3 digits: Subsector (e.g., 722 = Food services and drinking places)
    # - 4 digits: Industry group (e.g., 7225 = Restaurants and other eating places)
    # - 5 digits: Industry (e.g., 72251 = Full-service restaurants)
    # - 6 digits: Activity class (e.g., 722511 = Full-service restaurants with waiter service)
    # =========================================================================

    op.create_table(
        "denue_actividades_economicas",
        sa.Column(
            "codigo_act",
            sa.String(length=10),
            nullable=False,
            comment="SCIAN activity code",
        ),
        sa.Column(
            "nombre_act",
            sa.String(length=500),
            nullable=False,
            comment="Activity description",
        ),
        # Composite primary key: both code and name
        # This allows the same code to have different descriptions if needed
        sa.PrimaryKeyConstraint(
            "codigo_act", "nombre_act", name=op.f("pk_denue_actividades_economicas")
        ),
    )

    # =========================================================================
    # TABLE 3: denue_entidades (States Catalog)
    # =========================================================================
    # Mexican states (Entidades Federativas)
    # Total: 32 states including Mexico City (CDMX)
    # =========================================================================

    op.create_table(
        "denue_entidades",
        sa.Column("cve_ent", sa.String(length=2), nullable=False, comment="State code"),
        sa.Column(
            "entidad", sa.String(length=100), nullable=False, comment="State name"
        ),
        sa.PrimaryKeyConstraint("cve_ent", name=op.f("pk_denue_entidades")),
    )

    # =========================================================================
    # TABLE 4: denue_municipios (Municipalities Catalog)
    # =========================================================================
    # Mexican municipalities (Municipios)
    # Total: ~2,469 municipalities across all states
    # Hierarchical relationship: State → Municipality
    # =========================================================================

    op.create_table(
        "denue_municipios",
        sa.Column("cve_ent", sa.String(length=2), nullable=False, comment="State code"),
        sa.Column(
            "cve_mun", sa.String(length=3), nullable=False, comment="Municipality code"
        ),
        sa.Column(
            "municipio",
            sa.String(length=100),
            nullable=False,
            comment="Municipality name",
        ),
        # Composite primary key: state + municipality code
        sa.PrimaryKeyConstraint("cve_ent", "cve_mun", name=op.f("pk_denue_municipios")),
    )

    # Index for name-based lookups
    # Query pattern: "Find municipality by name"
    op.create_index(
        op.f("ix_municipio_nombre"), "denue_municipios", ["municipio"], unique=False
    )

    # =========================================================================
    # TABLE 5: denue_localidades (Localities Catalog)
    # =========================================================================
    # Mexican localities (Localidades)
    # Localities are settlements within municipalities (cities, towns, villages)
    # Hierarchical relationship: State → Municipality → Locality
    # =========================================================================

    op.create_table(
        "denue_localidades",
        sa.Column("cve_ent", sa.String(length=2), nullable=False, comment="State code"),
        sa.Column(
            "cve_mun", sa.String(length=3), nullable=False, comment="Municipality code"
        ),
        sa.Column(
            "cve_loc", sa.String(length=4), nullable=False, comment="Locality code"
        ),
        sa.Column(
            "localidad", sa.String(length=100), nullable=False, comment="Locality name"
        ),
        # Composite primary key: state + municipality + locality code
        sa.PrimaryKeyConstraint(
            "cve_ent", "cve_mun", "cve_loc", name=op.f("pk_denue_localidades")
        ),
    )

    # Index for name-based lookups
    # Query pattern: "Find locality by name"
    op.create_index(
        op.f("ix_localidad_nombre"), "denue_localidades", ["localidad"], unique=False
    )


def downgrade() -> None:
    """
    Drop all DENUE tables in reverse order.

    Order is important to avoid foreign key issues in the future if we add them.
    """

    # Drop in reverse order of creation
    op.drop_index(op.f("ix_localidad_nombre"), table_name="denue_localidades")
    op.drop_table("denue_localidades")

    op.drop_index(op.f("ix_municipio_nombre"), table_name="denue_municipios")
    op.drop_table("denue_municipios")

    op.drop_table("denue_entidades")

    op.drop_table("denue_actividades_economicas")

    # Drop all establecimientos indexes
    op.drop_index(op.f("ix_establecimientos_period"), table_name="establecimientos")
    op.drop_index(op.f("ix_establecimientos_cve_mun"), table_name="establecimientos")
    op.drop_index(op.f("ix_establecimientos_cve_ent"), table_name="establecimientos")
    op.drop_index(op.f("ix_establecimientos_codigo_act"), table_name="establecimientos")
    op.drop_index(op.f("ix_estab_clee"), table_name="establecimientos")
    op.drop_index(op.f("ix_estab_type_size"), table_name="establecimientos")
    op.drop_index(op.f("ix_estab_coords"), table_name="establecimientos")
    op.drop_index(op.f("ix_estab_postal"), table_name="establecimientos")
    op.drop_index(op.f("ix_estab_composite"), table_name="establecimientos")
    op.drop_index(op.f("ix_estab_activity_period"), table_name="establecimientos")
    op.drop_index(op.f("ix_estab_geo_simple"), table_name="establecimientos")
    op.drop_index(op.f("ix_estab_geo"), table_name="establecimientos")
    op.drop_table("establecimientos")
