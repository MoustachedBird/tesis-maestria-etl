"""
Database model tests.

Tests for all ORM models including:
- Establecimiento helper methods (unit tests with manual fixtures)
- Schema alignment validation (integration tests with real DB)
- Catalog table models (Entidad, Municipio, Localidad)
- Data quality validation (integration tests)
- Query performance (integration tests on 100M+ rows)
"""

import pytest

# ============================================================================
# ESTABLECIMIENTO HELPER METHODS (UNIT TESTS - MANUAL FIXTURES)
# ============================================================================


@pytest.mark.unit
class TestEstablecimientoHelpers:
    """Test all 11 helper methods using manual fixtures (fast, isolated)."""

    def test_get_full_address_complete(self, sample_establecimiento):
        """Test full address formatting with all components."""
        address = sample_establecimiento.get_full_address()

        assert "CALLE INSURGENTES SUR" in address
        assert "#1234A" in address
        assert "EDIFICIO TORRE EMPRESARIAL" in address
        assert "INT. 10B" in address
        assert "COLONIA DEL VALLE" in address
        assert "CENTRO COMERCIAL PLAZA INSURGENTES" in address
        assert "LOCAL L-42" in address
        assert "C.P. 03100" in address

    def test_get_full_address_without_postal(self, sample_establecimiento):
        """Test address formatting without postal code."""
        address = sample_establecimiento.get_full_address(include_postal=False)

        assert "CALLE INSURGENTES SUR" in address
        assert "C.P." not in address

    def test_get_full_address_minimal(self, sample_establecimiento_minimal):
        """Test address formatting with minimal data."""
        address = sample_establecimiento_minimal.get_full_address()

        # Should return fallback message
        assert "Dirección no disponible" in address or address == "Dirección no disponible"

    def test_get_cross_streets(self, sample_establecimiento):
        """Test extracting intersecting streets."""
        cross_streets = sample_establecimiento.get_cross_streets()

        assert len(cross_streets) == 3
        assert "AVENIDA REFORMA" in cross_streets
        assert "CALLE CHAPULTEPEC" in cross_streets
        assert "BOULEVARD CONSTITUYENTES" in cross_streets

    def test_get_cross_streets_empty(self, sample_establecimiento_minimal):
        """Test cross streets when none are available."""
        cross_streets = sample_establecimiento_minimal.get_cross_streets()

        assert cross_streets == []

    def test_is_valid_location_mexico(self, sample_establecimiento):
        """Test coordinate validation for location within Mexico."""
        assert sample_establecimiento.is_valid_location is True

    def test_is_valid_location_outside_mexico(self, sample_establecimiento_invalid_coords):
        """Test coordinate validation for location outside Mexico."""
        assert sample_establecimiento_invalid_coords.is_valid_location is False

    def test_is_valid_location_no_coords(self, sample_establecimiento_minimal):
        """Test coordinate validation when coordinates are None."""
        assert sample_establecimiento_minimal.is_valid_location is False

    # NOTA: los tests de get_employee_range/get_business_size/
    # get_scian_sector/get_scian_subsector se eliminaron junto con esos
    # helpers: su semantica vive en los catalogos denue_estratos_personal y
    # denue_actividades_economicas (ver tests/unit/test_dominio_catalogs.py).

    def test_is_fixed_establishment_true(self, sample_establecimiento):
        """Test fixed establishment check (permanent)."""
        assert sample_establecimiento.is_fixed_establishment() is True

    def test_has_contact_info_true(self, sample_establecimiento):
        """Test contact info availability check."""
        assert sample_establecimiento.has_contact_info() is True

    def test_has_contact_info_false(self, sample_establecimiento_minimal):
        """Test contact info check when no contact available."""
        assert sample_establecimiento_minimal.has_contact_info() is False

    def test_get_registration_year(self, sample_establecimiento):
        """Test registration year extraction from fecha_alta_str."""
        year = sample_establecimiento.get_registration_year()

        assert year == 2020

    def test_get_geo_hierarchy(self, sample_establecimiento):
        """Test geographic administrative hierarchy extraction."""
        geo = sample_establecimiento.get_geo_hierarchy()

        assert geo["estado"] == "09"
        assert geo["municipio"] == "015"
        assert geo["localidad"] == "0001"
        assert geo["ageb"] == "0123"
        assert geo["manzana"] == "045"


# ============================================================================
# SCHEMA ALIGNMENT (INTEGRATION TESTS - REAL DB)
# ============================================================================


@pytest.mark.integration
@pytest.mark.db
class TestSchemaAlignment:
    """Validate model schema matches production database."""

    def test_column_count(self, db_schema_columns, model_columns):
        """Verify model has 39 columns (fecha_alta split into str + date)."""
        assert len(model_columns) == 39

    def test_all_db_columns_mapped(self, db_schema_columns, model_columns):
        """Check all database columns are in the model.

        The old test DB may still have ``fecha_alta`` while the model now has
        ``fecha_alta_str`` + ``fecha_alta_date``.  We normalise by mapping the
        legacy name before comparing.
        """
        # Normalise legacy column name so both old and new DBs pass
        normalised_db = {"fecha_alta_str" if c == "fecha_alta" else c for c in db_schema_columns}
        missing = normalised_db - model_columns

        assert missing == set(), f"DB columns missing from model: {missing}"

    def test_no_extra_model_columns(self, db_schema_columns, model_columns):
        """Check model has no unexpected columns beyond the known schema change."""
        normalised_db = {"fecha_alta_str" if c == "fecha_alta" else c for c in db_schema_columns}
        # fecha_alta_date is a new column that the old DB doesn't have
        extra = model_columns - normalised_db - {"fecha_alta_date"}

        assert extra == set(), f"Model has extra columns not in DB: {extra}"

    def test_establecimiento_table_exists(self, production_db):
        """Verify establecimientos table exists in production DB."""
        tables = production_db.execute("SHOW TABLES").fetchall()
        table_names = [t[0] for t in tables]

        assert "establecimientos" in table_names


# ============================================================================
# DATABASE CONNECTION (INTEGRATION TESTS - REAL DB)
# ============================================================================


@pytest.mark.integration
@pytest.mark.db
class TestDatabaseConnection:
    """Test database connectivity and basic queries."""

    def test_production_db_connection(self, production_db):
        """Test connection to production DuckDB."""
        result = production_db.execute("SELECT 1").fetchone()

        assert result[0] == 1

    def test_establecimientos_table_count(self, query_helper):
        """Verify establecimientos table has data (100M+ rows expected)."""
        count = query_helper.count_establecimientos()

        assert count > 0
        assert count > 1_000_000  # At least 1 million records

    def test_real_sample_fixture(self, real_sample_establecimiento):
        """Verify real sample fixture loads actual data."""
        assert real_sample_establecimiento.id is not None
        assert real_sample_establecimiento.nom_estab is not None
        assert real_sample_establecimiento.codigo_act is not None


# ============================================================================
# CATALOG TABLE MODELS (INTEGRATION TESTS - REAL DB)
# ============================================================================


@pytest.mark.integration
@pytest.mark.catalog
@pytest.mark.db
class TestCatalogTables:
    """Test catalog table models (Entidad, Municipio, Localidad, ActividadEconomica)."""

    def test_entidad_table_exists(self, production_db):
        """Verify denue_entidades table exists."""
        tables = production_db.execute("SHOW TABLES").fetchall()
        table_names = [t[0] for t in tables]

        assert "denue_entidades" in table_names

    def test_entidad_count(self, production_db):
        """Verify entidades table has 32 states."""
        count = production_db.execute("SELECT COUNT(*) FROM denue_entidades").fetchone()[0]

        assert count == 32

    def test_entidad_model_match(self, real_sample_entidad):
        """Test Entidad model can load real data."""
        assert real_sample_entidad.cve_ent is not None
        assert real_sample_entidad.entidad is not None
        assert len(real_sample_entidad.cve_ent) == 2

    def test_municipio_table_exists(self, production_db):
        """Verify denue_municipios table exists."""
        count = production_db.execute("SELECT COUNT(*) FROM denue_municipios").fetchone()[0]

        assert count > 0
        assert count > 2000  # Mexico has 2400+ municipalities

    def test_municipio_model_match(self, real_sample_municipio):
        """Test Municipio model can load real data."""
        assert real_sample_municipio.cve_ent is not None
        assert real_sample_municipio.cve_mun is not None
        assert real_sample_municipio.municipio is not None

    def test_localidad_table_exists(self, production_db):
        """Verify denue_localidades table exists."""
        count = production_db.execute("SELECT COUNT(*) FROM denue_localidades").fetchone()[0]

        assert count > 0
        assert count > 10000  # Mexico has tens of thousands of localities

    def test_actividad_economica_table_exists(self, production_db):
        """Verify denue_actividades_economicas table exists."""
        count = production_db.execute(
            "SELECT COUNT(*) FROM denue_actividades_economicas"
        ).fetchone()[0]

        assert count > 0
        assert count > 1000  # SCIAN has 1000+ activity codes


# ============================================================================
# DATA QUALITY VALIDATION (INTEGRATION TESTS - REAL DB)
# ============================================================================


@pytest.mark.integration
@pytest.mark.quality
@pytest.mark.db
class TestDataQuality:
    """Validate data quality and integrity in production database."""

    def test_primary_key_not_null(self, production_db):
        """Verify all establishments have non-null IDs."""
        count_null = production_db.execute(
            "SELECT COUNT(*) FROM establecimientos WHERE id IS NULL"
        ).fetchone()[0]

        assert count_null == 0

    def test_coordinates_in_valid_range(self, production_db):
        """Test that coordinates are within valid ranges when present."""
        query = """
        SELECT COUNT(*)
        FROM establecimientos
        WHERE latitud IS NOT NULL
          AND longitud IS NOT NULL
          AND (
              latitud < -90 OR latitud > 90
              OR longitud < -180 OR longitud > 180
          )
        """
        count_invalid = production_db.execute(query).fetchone()[0]

        # Should be very few or zero invalid coordinates
        total_with_coords = production_db.execute(
            "SELECT COUNT(*) FROM establecimientos WHERE latitud IS NOT NULL"
        ).fetchone()[0]

        if total_with_coords > 0:
            invalid_ratio = count_invalid / total_with_coords
            assert invalid_ratio < 0.01  # Less than 1% invalid

    def test_codigo_act_format(self, production_db):
        """Test SCIAN codes have valid format when present."""
        query = """
        SELECT COUNT(*)
        FROM establecimientos
        WHERE codigo_act IS NOT NULL
          AND LENGTH(codigo_act) < 2
        """
        count_invalid = production_db.execute(query).fetchone()[0]

        assert count_invalid == 0

    def test_postal_code_format(self, production_db):
        """Test postal codes are 5 digits when present."""
        # Get sample of postal codes
        query_total = """
        SELECT COUNT(*)
        FROM establecimientos
        WHERE cod_postal IS NOT NULL
        """
        total_with_postal = production_db.execute(query_total).fetchone()[0]

        query_invalid = """
        SELECT COUNT(*)
        FROM establecimientos
        WHERE cod_postal IS NOT NULL
          AND (LENGTH(cod_postal) != 5 OR cod_postal NOT SIMILAR TO '[0-9]{5}')
        """
        count_invalid = production_db.execute(query_invalid).fetchone()[0]

        # Check that most postal codes are valid (allow up to 10% invalid due to data quality)
        if total_with_postal > 0:
            invalid_ratio = count_invalid / total_with_postal
            assert invalid_ratio < 0.10  # Less than 10% invalid


# ============================================================================
# QUERY TESTS (INTEGRATION TESTS - REAL DB)
# ============================================================================


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.db
class TestQueries:
    """Test actual queries against production database (100M+ rows)."""

    def test_filter_by_state(self, query_helper):
        """Test filtering establishments by state (should use index)."""
        # CDMX (09) should have many establishments
        count = query_helper.count_by_state("09")

        assert count > 0
        assert count > 100000  # CDMX has many businesses

    def test_filter_by_activity_code(self, query_helper):
        """Test filtering by SCIAN activity code."""
        # Restaurants (722) should be common
        count = query_helper.count_by_activity("722")

        assert count > 0
        assert count > 50000  # Restaurants are common

    def test_sample_query_performance(self, production_db):
        """Test that sample queries complete in reasonable time."""
        import time

        query = """
        SELECT COUNT(*)
        FROM establecimientos
        WHERE cve_ent = '09'
          AND codigo_act LIKE '722%'
        """

        start = time.time()
        result = production_db.execute(query).fetchone()[0]
        elapsed = time.time() - start

        assert result > 0
        # Query should complete in under 5 seconds with proper indexes
        assert elapsed < 5.0

    def test_geographic_hierarchy_query(self, production_db):
        """Test query using geographic hierarchy."""
        query = """
        SELECT COUNT(*)
        FROM establecimientos
        WHERE cve_ent = '19'  -- Nuevo León
          AND cve_mun = '039'  -- Monterrey
        """

        count = production_db.execute(query).fetchone()[0]

        assert count > 0
        assert count > 10000  # Monterrey is a major city

    def test_helper_method_on_real_data(self, real_sample_establecimiento):
        """Test helper methods work correctly on real production data."""
        # Test get_full_address
        address = real_sample_establecimiento.get_full_address()
        assert isinstance(address, str)
        assert len(address) > 0

        # Test coordinate validation
        if real_sample_establecimiento.latitud and real_sample_establecimiento.longitud:
            is_valid = real_sample_establecimiento.is_valid_location
            assert isinstance(is_valid, bool)

        # Test domain values (semantics now live in the catalog tables)
        if real_sample_establecimiento.per_ocu_id is not None:
            assert isinstance(real_sample_establecimiento.per_ocu_id, int)

        if real_sample_establecimiento.codigo_act:
            assert len(real_sample_establecimiento.codigo_act) >= 2
