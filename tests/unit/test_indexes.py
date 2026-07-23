"""Los indices se gestionan solos: nadie tiene que acordarse de nada.

Se suspenden durante las escrituras masivas (ART fila por fila las hace
ordenes de magnitud mas lentas) y se restituyen al terminar, tambien si la
etapa falla. La definicion sale del modelo ORM, no de SQL suelto.
"""

import duckdb
import pytest
from sqlalchemy import create_engine

from src_etl.db.base import Base
from src_etl.db.indexes import (
    MANAGED_TABLES,
    drop_indexes,
    ensure_indexes,
    existing_indexes,
    expected_indexes,
    missing_indexes,
    suspended_indexes,
)
from src_etl.db.models import Establecimiento  # noqa: F401


@pytest.fixture()
def db(tmp_path):
    """Base scratch con el esquema del modelo (igual que la migracion)."""
    path = tmp_path / "idx.duckdb"
    engine = create_engine(f"duckdb:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    conn = duckdb.connect(str(path))
    yield conn
    conn.close()


@pytest.mark.unit
class TestDefinicion:
    def test_se_derivan_del_modelo(self):
        esperados = expected_indexes()
        assert esperados
        declarados = {
            i.name for t in MANAGED_TABLES for i in Base.metadata.tables[t].indexes
        }
        assert set(esperados) == declarados

    def test_son_sentencias_create_index(self):
        for name, sql in expected_indexes().items():
            assert sql.upper().startswith("CREATE INDEX")
            assert name in sql


@pytest.mark.unit
class TestCicloDeVida:
    def test_la_base_migrada_nace_completa(self, db):
        assert missing_indexes(db) == []

    def test_drop_y_ensure_son_simetricos(self, db):
        soltados = drop_indexes(db)
        assert soltados and existing_indexes(db) == set()
        creados = ensure_indexes(db)
        assert sorted(creados) == sorted(soltados)
        assert missing_indexes(db) == []

    def test_ensure_es_idempotente(self, db):
        assert ensure_indexes(db) == []

    def test_suspendidos_dentro_restituidos_fuera(self, db):
        with suspended_indexes(db):
            assert existing_indexes(db) == set()
        assert missing_indexes(db) == []

    def test_se_restituyen_aunque_la_etapa_falle(self, db):
        with pytest.raises(RuntimeError):
            with suspended_indexes(db):
                raise RuntimeError("fallo a media carga")
        assert missing_indexes(db) == []

    def test_enabled_false_los_deja_intactos(self, db):
        antes = existing_indexes(db)
        with suspended_indexes(db, enabled=False):
            assert existing_indexes(db) == antes
        assert existing_indexes(db) == antes
