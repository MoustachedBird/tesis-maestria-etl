"""
Pytest configuration and shared fixtures.

This module provides test fixtures using a HYBRID approach:
- Unit tests use MANUAL fixtures (fast, isolated)
- Integration tests use REAL production DuckDB data (realistic)

Environment Variables:
    TEST_DB_PATH: Path to production DuckDB (default: data/denue_warehouse_most_recent.db)
"""

import os
from datetime import date

import duckdb
import pytest
from sqlalchemy import inspect

from src_etl.db.enums import (
    PersonalOcupado,
    TipoAsentamiento,
    TipoUnidadEconomica,
    TipoVial,
    domain_id,
)
from src_etl.db.models import (
    ActividadEconomica,
    Entidad,
    Establecimiento,
    Localidad,
    Municipio,
)
from src_etl.db.models import (
    TipoAsentamiento as TipoAsentamientoCat,
)
from src_etl.db.models import (
    TipoVialidad as TipoVialidadCat,
)

# ============================================================================
# CONFIGURATION
# ============================================================================


def get_test_db_path() -> str:
    """
    Get production database path from environment or use default.

    Returns:
        Path to production DuckDB file
    """
    return os.getenv("TEST_DB_PATH", "data/denue_warehouse_most_recent.db")


# ============================================================================
# DATABASE FIXTURES (INTEGRATION TESTS - REAL DATA)
# ============================================================================


@pytest.fixture(scope="session")
def production_db():
    """
    Read-only connection to production DuckDB warehouse.

    Used for integration tests that validate against real data.
    Scope: session (shared across all tests for performance).

    Yields:
        DuckDB connection (read-only)
    """
    db_path = get_test_db_path()

    if not os.path.exists(db_path):
        pytest.skip(f"Production database not found at {db_path}")

    conn = duckdb.connect(db_path, read_only=True)
    yield conn
    conn.close()


@pytest.fixture
def real_sample_establecimiento(production_db) -> Establecimiento:
    """
    Pull a real establishment from production database.

    Retrieves actual record with complete data for integration testing.

    Args:
        production_db: Production DuckDB connection fixture

    Returns:
        Establecimiento instance populated with real data
    """
    # Get column names
    col_query = production_db.execute("DESCRIBE establecimientos")
    all_columns = [row[0] for row in col_query.fetchall()]

    # Get sample establishment with good data coverage
    query = """
    SELECT *
    FROM establecimientos
    WHERE nom_estab IS NOT NULL
      AND nom_vial IS NOT NULL
      AND latitud IS NOT NULL
      AND per_ocu IS NOT NULL
      AND codigo_act IS NOT NULL
      AND telefono IS NOT NULL
    LIMIT 1
    """

    result = production_db.execute(query).fetchone()

    if not result:
        pytest.skip("No suitable sample data found in production database")

    # Create Establecimiento instance from real data
    estab = Establecimiento()
    for i, col in enumerate(all_columns):
        if hasattr(estab, col):
            setattr(estab, col, result[i])

    return estab


@pytest.fixture
def real_sample_entidad(production_db) -> Entidad:
    """
    Pull a real state (entidad) from production database.

    Args:
        production_db: Production DuckDB connection fixture

    Returns:
        Entidad instance with real data
    """
    query = "SELECT cve_ent, entidad FROM denue_entidades LIMIT 1"
    result = production_db.execute(query).fetchone()

    if not result:
        pytest.skip("No entidad data found")

    return Entidad(cve_ent=result[0], entidad=result[1])


@pytest.fixture
def real_sample_municipio(production_db) -> Municipio:
    """
    Pull a real municipality from production database.

    Args:
        production_db: Production DuckDB connection fixture

    Returns:
        Municipio instance with real data
    """
    query = "SELECT cve_ent, cve_mun, municipio FROM denue_municipios LIMIT 1"
    result = production_db.execute(query).fetchone()

    if not result:
        pytest.skip("No municipio data found")

    return Municipio(cve_ent=result[0], cve_mun=result[1], municipio=result[2])


# ============================================================================
# MANUAL FIXTURES (UNIT TESTS - FAST, ISOLATED)
# ============================================================================


@pytest.fixture
def sample_establecimiento() -> Establecimiento:
    """
    Manually crafted Establecimiento for unit tests.

    Fast, isolated fixture with controlled test data.
    Use for testing helper methods without database dependency.

    Returns:
        Establecimiento instance with manually set fields
    """
    estab = Establecimiento(
        id="test_123456789",
        clee="0512345678901",
        nom_estab="Test Restaurant & Bar",
        raz_social="TEST BUSINESS SA DE CV",
        codigo_act="722515",
        # Business type (FKs por id; el label vive en el catalogo)
        tipounieco_id=domain_id(TipoUnidadEconomica.FIJO),
        per_ocu_id=domain_id(PersonalOcupado.PERSONAS_11_30),
        # Address components
        tipo_vial_id=domain_id(TipoVial.CALLE),
        nom_vial="INSURGENTES SUR",
        numero_ext="1234",
        letra_ext="A",
        numero_int="10",
        letra_int="B",
        edificio="TORRE EMPRESARIAL",
        # Cross streets
        tipo_v_e_1=TipoVial.AVENIDA.value,
        nom_v_e_1="REFORMA",
        tipo_v_e_2=TipoVial.CALLE.value,
        nom_v_e_2="CHAPULTEPEC",
        tipo_v_e_3=TipoVial.BOULEVARD.value,
        nom_v_e_3="CONSTITUYENTES",
        # Settlement
        tipo_asent_id=domain_id(TipoAsentamiento.COLONIA),
        nomb_asent="DEL VALLE",
        # Shopping center
        nom_cencom="PLAZA INSURGENTES",
        num_local="L-42",
        # Postal & geographic
        cod_postal="03100",
        cve_ent="09",
        cve_mun="015",
        cve_loc="0001",
        ageb="0123",
        manzana="045",
        # Coordinates (Mexico City - valid)
        latitud=19.4326,
        longitud=-99.1332,
        # Contact
        telefono="5555551234",
        correoelec="test@example.com",
        www="https://www.testbusiness.com",
        # Temporal
        fecha_alta_str="2020-06",
        fecha_alta_date=date(2020, 6, 1),
        period=date(2023, 11, 1),
    )
    # Objeto transient (sin sesion): las relationships no cargan solas, se
    # asignan los objetos de catalogo para que los helpers de direccion
    # resuelvan los labels igual que con una fila persistida + JOIN.
    estab.tipo_vialidad = TipoVialidadCat(
        id=domain_id(TipoVial.CALLE), tipo_vial=TipoVial.CALLE.value, descripcion="test"
    )
    estab.tipo_asentamiento = TipoAsentamientoCat(
        id=domain_id(TipoAsentamiento.COLONIA),
        tipo_asent=TipoAsentamiento.COLONIA.value,
        descripcion="test",
    )
    return estab


@pytest.fixture
def sample_establecimiento_minimal() -> Establecimiento:
    """
    Minimal Establecimiento with only required fields.

    Use for testing edge cases and validation logic.

    Returns:
        Establecimiento with minimal data
    """
    return Establecimiento(
        id="test_minimal_001",
        nom_estab="Minimal Business",
        codigo_act="722515",
        per_ocu_id=domain_id(PersonalOcupado.PERSONAS_0_5),
        cve_ent="09",
    )


@pytest.fixture
def sample_establecimiento_invalid_coords() -> Establecimiento:
    """
    Establecimiento with invalid coordinates (outside Mexico).

    Use for testing coordinate validation.

    Returns:
        Establecimiento with invalid coordinates
    """
    estab = Establecimiento(
        id="test_invalid_coords",
        nom_estab="Invalid Location Business",
        codigo_act="722515",
        latitud=51.5074,  # London, UK
        longitud=-0.1278,
        cve_ent="09",
    )
    return estab


@pytest.fixture
def sample_entidad() -> Entidad:
    """
    Manually crafted Entidad for unit tests.

    Returns:
        Entidad instance (Mexico City)
    """
    return Entidad(cve_ent="09", entidad="CIUDAD DE MÉXICO")


@pytest.fixture
def sample_municipio() -> Municipio:
    """
    Manually crafted Municipio for unit tests.

    Returns:
        Municipio instance (Benito Juárez, CDMX)
    """
    return Municipio(cve_ent="09", cve_mun="015", municipio="BENITO JUÁREZ")


@pytest.fixture
def sample_localidad() -> Localidad:
    """
    Manually crafted Localidad for unit tests.

    Returns:
        Localidad instance
    """
    return Localidad(cve_ent="09", cve_mun="015", cve_loc="0001", localidad="CIUDAD DE MÉXICO")


@pytest.fixture
def sample_actividad_economica() -> ActividadEconomica:
    """
    Manually crafted ActividadEconomica for unit tests.

    Returns:
        ActividadEconomica instance (Restaurants)
    """
    return ActividadEconomica(
        codigo_act="722515",
        nombre_act="Restaurantes con servicio de preparación de alimentos a la carta o de comida corrida",
        sector="72",
        subsector="722",
        rama="7225",
    )


# ============================================================================
# HELPER FIXTURES
# ============================================================================


@pytest.fixture
def db_schema_columns(production_db) -> set[str]:
    """
    Get set of column names from production database schema.

    Useful for schema validation tests.

    Args:
        production_db: Production DuckDB connection

    Returns:
        Set of column names from establecimientos table
    """
    columns = production_db.execute("DESCRIBE establecimientos").fetchall()
    return {col[0] for col in columns}


@pytest.fixture
def model_columns() -> set[str]:
    """
    Get set of column names from Establecimiento model.

    Useful for schema validation tests.

    Returns:
        Set of column names from SQLAlchemy model
    """
    mapper = inspect(Establecimiento)
    return {col.key for col in mapper.columns}


# ============================================================================
# DATA QUERY HELPERS
# ============================================================================


@pytest.fixture
def query_helper(production_db):
    """
    Helper fixture for executing queries in tests.

    Provides convenient methods for common query patterns.

    Args:
        production_db: Production DuckDB connection

    Returns:
        QueryHelper instance
    """

    class QueryHelper:
        def __init__(self, conn):
            self.conn = conn

        def count_establecimientos(self) -> int:
            """Get total count of establishments."""
            return self.conn.execute("SELECT COUNT(*) FROM establecimientos").fetchone()[0]

        def count_by_state(self, cve_ent: str) -> int:
            """Get count of establishments in specific state."""
            return self.conn.execute(
                "SELECT COUNT(*) FROM establecimientos WHERE cve_ent = ?", [cve_ent]
            ).fetchone()[0]

        def count_by_activity(self, codigo_act: str) -> int:
            """Get count of establishments by activity code."""
            return self.conn.execute(
                "SELECT COUNT(*) FROM establecimientos WHERE codigo_act LIKE ?",
                [f"{codigo_act}%"],
            ).fetchone()[0]

        def sample_by_state(self, cve_ent: str, limit: int = 10) -> list:
            """Get sample establishments from specific state."""
            return self.conn.execute(
                "SELECT * FROM establecimientos WHERE cve_ent = ? LIMIT ?",
                [cve_ent, limit],
            ).fetchall()

    return QueryHelper(production_db)
