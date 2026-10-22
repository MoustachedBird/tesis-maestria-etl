"""Los seeds deben cumplir las restricciones reales de su tabla.

Un parquet mal formado (id repetido, etiqueta duplicada, NULL donde la tabla
exige NOT NULL) tiene que detenerse EN LA MIGRACION con un mensaje que diga
archivo, columna y valores — nunca colarse ni desaparecer en silencio por el
INSERT OR IGNORE de la siembra.
"""

import polars as pl
import pytest

from src.etl.loaders import dominios
from src.etl.loaders.dominios import TABLES, build_rows
from src.etl.loaders.estratos import build_rows as build_estrato_rows
from src.etl.loaders.seeds import validate_seed

TABLA = "denue_tipos_asentamiento"


@pytest.fixture()
def seed() -> pl.DataFrame:
    """Seed real del repo (fuente de verdad del catalogo)."""
    return pl.read_parquet(dominios.seed_path(TABLA))


@pytest.mark.unit
class TestSeedsDelRepoSonValidos:
    """Los seeds versionados deben pasar la validacion siempre."""

    def test_dominios_validos(self):
        assert all(rows for rows in build_rows().values())

    def test_estratos_validos(self):
        assert build_estrato_rows()

    def test_todos_los_catalogos_de_dominio_tienen_seed(self):
        for table, *_ in TABLES:
            assert dominios.seed_path(table).exists()


@pytest.mark.unit
class TestSeedMalFormado:
    """Cada violacion posible se detecta con mensaje accionable."""

    def test_pk_duplicada(self, seed):
        with pytest.raises(ValueError, match="repetido"):
            validate_seed(pl.concat([seed, seed.head(1)]), TABLA, "x.parquet")

    def test_unique_duplicado(self, seed):
        otro_id = seed.head(1).with_columns(pl.lit(999, dtype=seed["id"].dtype).alias("id"))
        with pytest.raises(ValueError, match="repetido"):
            validate_seed(pl.concat([seed, otro_id]), TABLA, "x.parquet")

    def test_null_en_columna_not_null(self, seed):
        roto = seed.with_columns(
            pl.when(pl.col("id") == 1)
            .then(None)
            .otherwise(pl.col("descripcion"))
            .alias("descripcion")
        )
        with pytest.raises(ValueError, match="NULL"):
            validate_seed(roto, TABLA, "x.parquet")

    def test_falta_columna_obligatoria(self, seed):
        with pytest.raises(ValueError, match="faltan columnas"):
            validate_seed(seed.drop("descripcion"), TABLA, "x.parquet")

    def test_columna_inexistente_en_la_tabla(self, seed):
        with pytest.raises(ValueError, match="no existen"):
            validate_seed(seed.with_columns(pl.lit(1).alias("fantasma")), TABLA, "x.parquet")

    def test_seed_vacio(self, seed):
        with pytest.raises(ValueError, match="no tiene filas"):
            validate_seed(seed.head(0), TABLA, "x.parquet")

    def test_pk_compuesta_duplicada(self):
        mun = pl.read_parquet("data/seeds/seed_municipios.parquet")
        with pytest.raises(ValueError, match="cve_ent, cve_mun"):
            validate_seed(pl.concat([mun, mun.head(1)]), "denue_municipios", "x.parquet")

    def test_unique_compuesto_de_tabla(self):
        """sesnsp_delitos declara UNIQUE(bien_juridico, tipo, subtipo,
        modalidad): repetir la clave natural con otro id debe detenerse."""
        from src.etl.loaders.sesnsp import delitos_seed_path

        delitos = pl.read_parquet(delitos_seed_path())
        otro_id = delitos.head(1).with_columns(pl.lit(9999, dtype=delitos["id"].dtype).alias("id"))
        with pytest.raises(ValueError, match="bien_juridico, tipo, subtipo, modalidad"):
            validate_seed(pl.concat([delitos, otro_id]), "sesnsp_delitos", "x.parquet")

    def test_seeds_de_sesnsp_del_repo_son_validos(self):
        from src.etl.loaders.sesnsp import build_delitos_rows

        assert len(build_delitos_rows()) == 98
