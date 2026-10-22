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


@pytest.mark.unit
class TestLargoDeTextoDeOrigen:
    """El texto libre de INEGI no lleva tope de largo.

    El primer ingest contra Postgres fallo en los 20 cortes porque INEGI
    publica frases enteras en campos que su diccionario describe como
    numero y letra. Un tope sobre un dato que no controlamos no valida
    nada: convierte un valor sucio en el fallo de un COPY de 6M filas.
    """

    #: Campos que llegan como texto libre desde los CSV.
    LIBRES = (
        "numero_ext",
        "letra_ext",
        "numero_int",
        "letra_int",
        "num_local",
        "cod_postal",
        "ageb",
        "manzana",
        "fecha_alta_str",
    )

    #: Campos donde el tope SI es validacion: codigos que cruzan con los
    #: catalogos de geografia y de actividad economica.
    ACOTADOS = {"cve_ent": 2, "cve_mun": 3, "cve_loc": 4, "codigo_act": 10}

    def test_el_texto_libre_no_tiene_tope(self):
        from src.shared.db.models.silver import Establecimiento

        tabla = Establecimiento.__table__
        con_tope = {c: tabla.c[c].type.length for c in self.LIBRES if tabla.c[c].type.length}
        assert not con_tope, f"texto libre de INEGI con tope de largo: {con_tope}"

    def test_los_codigos_conservan_su_tope(self):
        """Ahi el largo es parte del formato, no una suposicion."""
        from src.shared.db.models.silver import Establecimiento

        tabla = Establecimiento.__table__
        assert {c: tabla.c[c].type.length for c in self.ACOTADOS} == self.ACOTADOS
