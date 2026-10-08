"""Tests del respaldo rotativo del warehouse (src/etl/utils/backup.py)."""

import duckdb
import pytest

from src.etl.utils import backup as backup_mod
from src.etl.utils.backup import backup_path_for, backup_warehouse


@pytest.fixture(autouse=True)
def _reset_estado():
    """Limpia el registro de bases ya respaldadas entre tests."""
    backup_mod._ya_respaldadas.clear()
    yield
    backup_mod._ya_respaldadas.clear()


def _crear_db(path, filas: int):
    """Crea una DuckDB con una tabla `t` de `filas` renglones."""
    conn = duckdb.connect(str(path))
    conn.execute("CREATE OR REPLACE TABLE t AS SELECT range AS i FROM range(?)", [filas])
    conn.close()


def _contar(path) -> int:
    conn = duckdb.connect(str(path), read_only=True)
    try:
        return conn.execute("SELECT count(*) FROM t").fetchone()[0]
    finally:
        conn.close()


@pytest.mark.unit
def test_respalda_y_conserva_los_datos(tmp_path):
    db = tmp_path / "silver.duckdb"
    _crear_db(db, 100)

    destino = backup_warehouse(db, stage="ingest")

    assert destino == backup_path_for(db)
    assert destino.exists()
    assert _contar(destino) == 100


@pytest.mark.unit
def test_una_sola_vez_por_proceso(tmp_path):
    """Varias etapas del mismo comando comparten UN respaldo: el previo al comando."""
    db = tmp_path / "silver.duckdb"
    _crear_db(db, 10)

    destino = backup_warehouse(db, stage="ingest")
    assert _contar(destino) == 10

    # Una etapa posterior modifica la base y pide respaldo de nuevo.
    _crear_db(db, 999)
    backup_warehouse(db, stage="clee-radio")

    assert _contar(destino) == 10, "el respaldo debe seguir siendo el estado PREVIO al comando"


@pytest.mark.unit
def test_la_siguiente_corrida_sobreescribe_el_respaldo(tmp_path):
    """Es rotativo: cada comando nuevo reemplaza el respaldo anterior."""
    db = tmp_path / "silver.duckdb"
    _crear_db(db, 10)
    destino = backup_warehouse(db, stage="ingest")
    assert _contar(destino) == 10

    # Nuevo comando = nuevo proceso: se limpia el registro en memoria.
    backup_mod._ya_respaldadas.clear()
    _crear_db(db, 55)
    backup_warehouse(db, stage="ingest")

    assert _contar(destino) == 55


@pytest.mark.unit
def test_base_ilegible_no_destruye_el_respaldo_bueno(tmp_path):
    """La proteccion central: una base corrupta NO pisa la copia sana.

    Es el escenario del incidente 2026-07-24: si tras corromperse la base
    se corre otro comando, el respaldo rotativo no debe reemplazarse con
    el estado dañado -- seria perder justo lo que salva.
    """
    db = tmp_path / "silver.duckdb"
    _crear_db(db, 42)
    destino = backup_warehouse(db, stage="ingest")
    assert _contar(destino) == 42

    db.write_bytes(b"esto no es una base de datos")  # corrupcion
    backup_mod._ya_respaldadas.clear()

    assert backup_warehouse(db, stage="clee-radio") is None
    assert _contar(destino) == 42, "el respaldo bueno debe sobrevivir"


@pytest.mark.unit
def test_sin_base_no_hay_respaldo(tmp_path):
    """Primera corrida (base inexistente): no hay nada que respaldar."""
    assert backup_warehouse(tmp_path / "no_existe.duckdb", stage="ingest") is None
    assert not backup_path_for(tmp_path / "no_existe.duckdb").exists()


@pytest.mark.unit
def test_se_puede_deshabilitar(tmp_path, monkeypatch):
    db = tmp_path / "silver.duckdb"
    _crear_db(db, 5)
    monkeypatch.setattr(backup_mod.settings, "WAREHOUSE_BACKUP_ENABLED", False)

    assert backup_warehouse(db, stage="ingest") is None
    assert not backup_path_for(db).exists()
