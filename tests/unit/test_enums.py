"""
ENUM class tests.

Tests for all 4 ENUM classes defined in src/db/enums.py:
- TipoVial (39 values)
- TipoAsentamiento (45 values)
- TipoUnidadEconomica (3 values)
- PersonalOcupado (7 values + helper methods)

These are pure unit tests with no database dependency.
"""

import pytest

from src_etl.db.enums import (
    PersonalOcupado,
    TipoAsentamiento,
    TipoUnidadEconomica,
    TipoVial,
)

# ============================================================================
# PERSONAL OCUPADO (EMPLOYEE COUNT ENUM)
# ============================================================================


@pytest.mark.unit
class TestPersonalOcupado:
    """Test PersonalOcupado enum and its helper methods."""

    def test_enum_values_count(self):
        """Verify PersonalOcupado has 8 values (including NINGUNO)."""
        assert len(PersonalOcupado) == 8

    def test_enum_values_exist(self):
        """Test all expected values exist."""
        assert PersonalOcupado.NINGUNO.value == "NINGUNO"
        assert PersonalOcupado.PERSONAS_0_5.value == "0 A 5 PERSONAS"
        assert PersonalOcupado.PERSONAS_6_10.value == "6 A 10 PERSONAS"
        assert PersonalOcupado.PERSONAS_11_30.value == "11 A 30 PERSONAS"
        assert PersonalOcupado.PERSONAS_31_50.value == "31 A 50 PERSONAS"
        assert PersonalOcupado.PERSONAS_51_100.value == "51 A 100 PERSONAS"
        assert PersonalOcupado.PERSONAS_101_250.value == "101 A 250 PERSONAS"
        assert PersonalOcupado.PERSONAS_251_MAS.value == "251 Y MAS PERSONAS"

    # get_range() method tests

    def test_get_range_micro_0_5(self):
        """Test get_range for 0-5 employees."""
        min_emp, max_emp = PersonalOcupado.PERSONAS_0_5.get_range()

        assert min_emp == 0
        assert max_emp == 5

    def test_get_range_micro_6_10(self):
        """Test get_range for 6-10 employees."""
        min_emp, max_emp = PersonalOcupado.PERSONAS_6_10.get_range()

        assert min_emp == 6
        assert max_emp == 10

    def test_get_range_small_11_30(self):
        """Test get_range for 11-30 employees."""
        min_emp, max_emp = PersonalOcupado.PERSONAS_11_30.get_range()

        assert min_emp == 11
        assert max_emp == 30

    def test_get_range_small_31_50(self):
        """Test get_range for 31-50 employees."""
        min_emp, max_emp = PersonalOcupado.PERSONAS_31_50.get_range()

        assert min_emp == 31
        assert max_emp == 50

    def test_get_range_medium_51_100(self):
        """Test get_range for 51-100 employees."""
        min_emp, max_emp = PersonalOcupado.PERSONAS_51_100.get_range()

        assert min_emp == 51
        assert max_emp == 100

    def test_get_range_medium_101_250(self):
        """Test get_range for 101-250 employees."""
        min_emp, max_emp = PersonalOcupado.PERSONAS_101_250.get_range()

        assert min_emp == 101
        assert max_emp == 250

    def test_get_range_large_251_mas(self):
        """Test get_range for 251+ employees."""
        min_emp, max_emp = PersonalOcupado.PERSONAS_251_MAS.get_range()

        assert min_emp == 251
        assert max_emp == 999999  # Large upper bound

    def test_get_range_ninguno(self):
        """Test get_range for NINGUNO."""
        min_emp, max_emp = PersonalOcupado.NINGUNO.get_range()

        assert min_emp == 0
        assert max_emp == 0

    # get_size_category() method tests

    def test_get_size_category_micro_0_5(self):
        """Test size category for 0-5 employees is Micro."""
        category = PersonalOcupado.PERSONAS_0_5.get_size_category()

        assert category == "Micro"

    def test_get_size_category_micro_6_10(self):
        """Test size category for 6-10 employees is Micro."""
        category = PersonalOcupado.PERSONAS_6_10.get_size_category()

        assert category == "Micro"

    def test_get_size_category_small_11_30(self):
        """Test size category for 11-30 employees is Small."""
        category = PersonalOcupado.PERSONAS_11_30.get_size_category()

        assert category == "Small"

    def test_get_size_category_small_31_50(self):
        """Test size category for 31-50 employees is Small."""
        category = PersonalOcupado.PERSONAS_31_50.get_size_category()

        assert category == "Small"

    def test_get_size_category_medium_51_100(self):
        """Test size category for 51-100 employees is Medium."""
        category = PersonalOcupado.PERSONAS_51_100.get_size_category()

        assert category == "Medium"

    def test_get_size_category_medium_101_250(self):
        """Test size category for 101-250 employees is Medium."""
        category = PersonalOcupado.PERSONAS_101_250.get_size_category()

        assert category == "Medium"

    def test_get_size_category_large(self):
        """Test size category for 251+ employees is Large."""
        category = PersonalOcupado.PERSONAS_251_MAS.get_size_category()

        assert category == "Large"

    def test_get_size_category_ninguno(self):
        """Test size category for NINGUNO is Unknown."""
        category = PersonalOcupado.NINGUNO.get_size_category()

        assert category == "Unknown"


# ============================================================================
# TIPO VIAL (ROAD/STREET TYPE ENUM)
# ============================================================================


@pytest.mark.unit
class TestTipoVial:
    """Test TipoVial enum (39 road/street types, aligned with DOMAIN_CATALOGS)."""

    def test_enum_values_count(self):
        """Verify TipoVial has 39 values (the full INEGI domain)."""
        assert len(TipoVial) == 39

    def test_infrastructure_road_types_exist(self):
        """Values added when syncing with the ingest domain (DOMAIN_CATALOGS)."""
        assert TipoVial.CALLE_DE_RODAJE.value == "CALLE DE RODAJE"
        assert TipoVial.PUENTE.value == "PUENTE"
        assert TipoVial.TUNEL.value == "TUNEL"
        assert TipoVial.PASO_A_DESNIVEL.value == "PASO A DESNIVEL"
        assert TipoVial.GLORIETA.value == "GLORIETA"
        assert TipoVial.RAMPA.value == "RAMPA"

    def test_common_road_types_exist(self):
        """Test most common road types exist."""
        assert TipoVial.NINGUNO.value == "NINGUNO"
        assert TipoVial.CALLE.value == "CALLE"
        assert TipoVial.AVENIDA.value == "AVENIDA"
        assert TipoVial.BOULEVARD.value == "BOULEVARD"
        assert TipoVial.CARRETERA.value == "CARRETERA"
        assert TipoVial.CALZADA.value == "CALZADA"
        assert TipoVial.PRIVADA.value == "PRIVADA"

    def test_all_values_are_uppercase(self):
        """Verify all enum values are uppercase strings."""
        for tipo in TipoVial:
            assert tipo.value == tipo.value.upper()
            assert isinstance(tipo.value, str)

    def test_specific_road_types(self):
        """Test specific road type values."""
        assert TipoVial.AUTOPISTA.value == "AUTOPISTA"
        assert TipoVial.CIRCUITO.value == "CIRCUITO"
        assert TipoVial.PERIFERICO.value == "PERIFERICO"
        assert TipoVial.VIADUCTO.value == "VIADUCTO"
        assert TipoVial.EJE_VIAL.value == "EJE VIAL"

    def test_enum_by_value(self):
        """Test enum can be accessed by value."""
        tipo = TipoVial("CALLE")

        assert tipo == TipoVial.CALLE

    def test_enum_invalid_value_raises_error(self):
        """Test that invalid value raises ValueError."""
        with pytest.raises(ValueError):
            TipoVial("INVALID_ROAD_TYPE")


# ============================================================================
# TIPO ASENTAMIENTO (SETTLEMENT TYPE ENUM)
# ============================================================================


@pytest.mark.unit
class TestTipoAsentamiento:
    """Test TipoAsentamiento enum (45 settlement types)."""

    def test_enum_values_count(self):
        """Verify TipoAsentamiento has 45 values."""
        assert len(TipoAsentamiento) == 45

    def test_common_settlement_types_exist(self):
        """Test most common settlement types exist."""
        assert TipoAsentamiento.NINGUNO.value == "NINGUNO"
        assert TipoAsentamiento.COLONIA.value == "COLONIA"
        assert TipoAsentamiento.FRACCIONAMIENTO.value == "FRACCIONAMIENTO"
        assert TipoAsentamiento.PUEBLO.value == "PUEBLO"
        assert TipoAsentamiento.CIUDAD.value == "CIUDAD"
        assert TipoAsentamiento.BARRIO.value == "BARRIO"
        assert TipoAsentamiento.EJIDO.value == "EJIDO"

    def test_industrial_zones_exist(self):
        """Test industrial zone types exist."""
        assert TipoAsentamiento.PARQUE_INDUSTRIAL.value == "PARQUE INDUSTRIAL"
        assert TipoAsentamiento.CIUDAD_INDUSTRIAL.value == "CIUDAD INDUSTRIAL"
        assert TipoAsentamiento.CORREDOR_INDUSTRIAL.value == "CORREDOR INDUSTRIAL"
        assert TipoAsentamiento.ZONA_INDUSTRIAL.value == "ZONA INDUSTRIAL"

    def test_residential_types_exist(self):
        """Test residential settlement types exist."""
        assert TipoAsentamiento.RESIDENCIAL.value == "RESIDENCIAL"
        assert TipoAsentamiento.UNIDAD_HABITACIONAL.value == "UNIDAD HABITACIONAL"
        assert TipoAsentamiento.CONJUNTO_HABITACIONAL.value == "CONJUNTO HABITACIONAL"
        assert TipoAsentamiento.CONDOMINIO.value == "CONDOMINIO"

    def test_rural_types_exist(self):
        """Test rural settlement types exist."""
        assert TipoAsentamiento.RANCHO.value == "RANCHO"
        assert TipoAsentamiento.RANCHERIA.value == "RANCHERIA"
        assert TipoAsentamiento.GRANJA.value == "GRANJA"
        assert TipoAsentamiento.HACIENDA.value == "HACIENDA"

    def test_all_values_are_uppercase(self):
        """Verify all enum values are uppercase strings."""
        for tipo in TipoAsentamiento:
            assert tipo.value == tipo.value.upper()
            assert isinstance(tipo.value, str)

    def test_enum_by_value(self):
        """Test enum can be accessed by value."""
        tipo = TipoAsentamiento("COLONIA")

        assert tipo == TipoAsentamiento.COLONIA


# ============================================================================
# TIPO UNIDAD ECONOMICA (ECONOMIC UNIT TYPE ENUM)
# ============================================================================


@pytest.mark.unit
class TestTipoUnidadEconomica:
    """Test TipoUnidadEconomica enum (3 establishment types)."""

    def test_enum_values_count(self):
        """Verify TipoUnidadEconomica has 3 values."""
        assert len(TipoUnidadEconomica) == 3

    def test_all_values_exist(self):
        """Test all three values exist."""
        assert TipoUnidadEconomica.NINGUNO.value == "NINGUNO"
        assert TipoUnidadEconomica.FIJO.value == "FIJO"
        assert TipoUnidadEconomica.SEMIFIJO.value == "SEMIFIJO"

    def test_fijo_is_permanent(self):
        """Test FIJO represents permanent establishments."""
        assert TipoUnidadEconomica.FIJO.value == "FIJO"
        # FIJO means fixed/permanent establishment

    def test_semifijo_is_temporary(self):
        """Test SEMIFIJO represents semi-fixed/temporary establishments."""
        assert TipoUnidadEconomica.SEMIFIJO.value == "SEMIFIJO"
        # SEMIFIJO means semi-fixed/temporary establishment (e.g., market stalls)

    def test_enum_by_value(self):
        """Test enum can be accessed by value."""
        tipo = TipoUnidadEconomica("FIJO")

        assert tipo == TipoUnidadEconomica.FIJO

    def test_all_values_are_uppercase(self):
        """Verify all enum values are uppercase strings."""
        for tipo in TipoUnidadEconomica:
            assert tipo.value == tipo.value.upper()
            assert isinstance(tipo.value, str)


# ============================================================================
# CROSS-ENUM TESTS
# ============================================================================


@pytest.mark.unit
class TestEnumIntegration:
    """Test interactions between different ENUMs."""

    def test_all_enums_are_string_enums(self):
        """Verify all ENUMs inherit from str (for SQLAlchemy compatibility)."""
        assert issubclass(TipoVial, str)
        assert issubclass(TipoAsentamiento, str)
        assert issubclass(TipoUnidadEconomica, str)
        assert issubclass(PersonalOcupado, str)

    def test_enums_can_be_compared_to_strings(self):
        """Test ENUMs can be compared directly to strings."""
        assert TipoVial.CALLE == "CALLE"
        assert PersonalOcupado.PERSONAS_11_30 == "11 A 30 PERSONAS"
        assert TipoUnidadEconomica.FIJO == "FIJO"

    def test_enum_values_are_unique_within_type(self):
        """Verify no duplicate values within each ENUM."""
        # TipoVial
        vial_values = [t.value for t in TipoVial]
        assert len(vial_values) == len(set(vial_values))

        # TipoAsentamiento
        asent_values = [t.value for t in TipoAsentamiento]
        assert len(asent_values) == len(set(asent_values))

        # TipoUnidadEconomica
        uni_values = [t.value for t in TipoUnidadEconomica]
        assert len(uni_values) == len(set(uni_values))

        # PersonalOcupado
        per_values = [t.value for t in PersonalOcupado]
        assert len(per_values) == len(set(per_values))
