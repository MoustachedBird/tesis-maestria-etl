"""Tests for seed loader validation functions."""

import polars as pl
import pytest

from src_etl.loaders import geography, scian


@pytest.mark.unit
class TestGeographyValidateSeeds:
    """Test geography.validate_seeds()."""

    def test_valid_seeds_pass(self, tmp_path):
        # Create valid seed files
        pl.DataFrame({"cve_ent": ["01"], "entidad": ["Aguascalientes"]}).write_parquet(
            tmp_path / "seed_entidades.parquet"
        )
        pl.DataFrame(
            {"cve_ent": ["01"], "cve_mun": ["001"], "municipio": ["Aguascalientes"]}
        ).write_parquet(tmp_path / "seed_municipios.parquet")
        pl.DataFrame(
            {
                "cve_ent": ["01"],
                "cve_mun": ["001"],
                "cve_loc": ["0001"],
                "localidad": ["Aguascalientes"],
            }
        ).write_parquet(tmp_path / "seed_localidades.parquet")

        assert geography.validate_seeds(tmp_path) is True

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Missing seed file"):
            geography.validate_seeds(tmp_path)

    def test_empty_file_raises(self, tmp_path):
        pl.DataFrame({"cve_ent": [], "entidad": []}).write_parquet(
            tmp_path / "seed_entidades.parquet"
        )
        with pytest.raises(ValueError, match="is empty"):
            geography.validate_seeds(tmp_path)

    def test_wrong_columns_raises(self, tmp_path):
        # Missing 'entidad' column
        pl.DataFrame({"cve_ent": ["01"], "wrong_col": ["x"]}).write_parquet(
            tmp_path / "seed_entidades.parquet"
        )
        with pytest.raises(ValueError, match="missing columns"):
            geography.validate_seeds(tmp_path)


@pytest.mark.unit
class TestScianValidateSeeds:
    """Test scian.validate_seeds()."""

    def test_valid_seeds_pass(self, tmp_path):
        pl.DataFrame({"codigo_act": ["111110"], "nombre_act": ["Cultivo de soya"]}).write_parquet(
            tmp_path / "seed_actividades.parquet"
        )

        assert scian.validate_seeds(tmp_path) is True

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Missing seed file"):
            scian.validate_seeds(tmp_path)

    def test_empty_file_raises(self, tmp_path):
        pl.DataFrame({"codigo_act": [], "nombre_act": []}).write_parquet(
            tmp_path / "seed_actividades.parquet"
        )
        with pytest.raises(ValueError, match="is empty"):
            scian.validate_seeds(tmp_path)

    def test_wrong_columns_raises(self, tmp_path):
        pl.DataFrame({"codigo_act": ["111110"], "bad_col": ["x"]}).write_parquet(
            tmp_path / "seed_actividades.parquet"
        )
        with pytest.raises(ValueError, match="missing columns"):
            scian.validate_seeds(tmp_path)


@pytest.mark.unit
class TestGeographyConstants:
    """Test geography module constants."""

    def test_seed_mappings_order(self):
        """FK order: entidades -> municipios -> localidades."""
        tables = [table for _, table, _ in geography.SEED_MAPPINGS]
        assert tables == [
            "denue_entidades",
            "denue_municipios",
            "denue_localidades",
        ]

    def test_expected_columns_defined(self):
        for file_name, _, _ in geography.SEED_MAPPINGS:
            assert file_name in geography.EXPECTED_COLUMNS

    def test_ddl_defined_for_all_tables(self):
        for _, table, _ in geography.SEED_MAPPINGS:
            assert table in geography.TABLE_DDL


@pytest.mark.unit
class TestScianConstants:
    """Test SCIAN module constants."""

    def test_table_name(self):
        assert scian.TABLE_NAME == "denue_actividades_economicas"

    def test_expected_columns(self):
        assert "codigo_act" in scian.EXPECTED_COLUMNS
        assert "nombre_act" in scian.EXPECTED_COLUMNS

    def test_ddl_contains_primary_key(self):
        assert "PRIMARY KEY" in scian.TABLE_DDL
