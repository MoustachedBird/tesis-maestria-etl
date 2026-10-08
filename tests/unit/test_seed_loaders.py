"""Cada loader siembra su tabla desde el parquet, via el camino central.

La logica de carga/validacion vive en `loaders/seeds.py` (probada en
test_seeds.py); aqui se verifica que cada loader apunta a la tabla y el
archivo correctos y que la siembra es idempotente y append-only sobre el
esquema real del modelo.
"""

import duckdb
import pytest
from sqlalchemy import create_engine

from src.etl.loaders import dominios, estratos, geography, scian
from src.shared.db.base import DEFAULT_SCHEMA, Base, get_table
from src.shared.db.models import Establecimiento  # noqa: F401  (puebla el metadata)


def _db_with_tables(tmp_path, *table_names) -> duckdb.DuckDBPyConnection:
    """Crea las tablas dadas con el esquema del modelo (como la migracion)."""
    path = tmp_path / "seeds.duckdb"
    engine = create_engine(f"duckdb:///{path}")
    Base.metadata.create_all(engine, tables=[get_table(n) for n in table_names])
    engine.dispose()
    conn = duckdb.connect(str(path))
    conn.execute(f"SET schema='{DEFAULT_SCHEMA}'")
    return conn


@pytest.mark.unit
class TestSiembraCadaCatalogo:
    """load_seeds nativo puebla la tabla desde el parquet del repo."""

    def test_geografia(self, tmp_path):
        conn = _db_with_tables(
            tmp_path, "denue_entidades", "denue_municipios", "denue_localidades"
        )
        stats = geography.load_seeds(conn)
        assert stats["denue_entidades"] == 32
        assert conn.execute("SELECT COUNT(*) FROM denue_entidades").fetchone()[0] == 32

    def test_scian_deriva_jerarquia(self, tmp_path):
        conn = _db_with_tables(tmp_path, "denue_actividades_economicas")
        scian.load_seeds(conn)
        row = conn.execute(
            "SELECT sector, subsector, rama FROM denue_actividades_economicas "
            "WHERE codigo_act = '461110'"
        ).fetchone()
        assert row == ("46", "461", "4611")

    def test_dominios(self, tmp_path):
        conn = _db_with_tables(
            tmp_path,
            "denue_tipos_vialidad",
            "denue_tipos_asentamiento",
            "denue_tipos_unidad_economica",
        )
        stats = dominios.load_seeds(conn)
        assert stats["denue_tipos_asentamiento"] == 45

    def test_estratos(self, tmp_path):
        conn = _db_with_tables(tmp_path, "denue_estratos_personal")
        stats = estratos.load_seeds(conn)
        assert stats["denue_estratos_personal"] == 8


@pytest.mark.unit
class TestIdempotenteYAppendOnly:
    """Re-sembrar no duplica ni borra; una fila referenciada sobrevive."""

    def test_reseed_no_duplica(self, tmp_path):
        conn = _db_with_tables(
            tmp_path, "denue_entidades", "denue_municipios", "denue_localidades"
        )
        geography.load_seeds(conn)
        geography.load_seeds(conn)  # segunda vez
        assert conn.execute("SELECT COUNT(*) FROM denue_entidades").fetchone()[0] == 32

    def test_no_dropea_catalogo_referenciado(self, tmp_path):
        conn = _db_with_tables(
            tmp_path, "denue_entidades", "denue_municipios", "denue_localidades"
        )
        geography.load_seeds(conn)
        conn.execute(
            "CREATE TABLE fact_ref (cve_ent VARCHAR REFERENCES denue_entidades(cve_ent))"
        )
        conn.execute("INSERT INTO fact_ref VALUES ('01')")
        geography.load_seeds(conn)  # no debe dropear ni truncar
        assert conn.execute("SELECT COUNT(*) FROM fact_ref").fetchone()[0] == 1
