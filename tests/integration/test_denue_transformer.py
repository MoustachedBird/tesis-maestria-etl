"""
Integration tests for DenuePipelineOrchestrator.

Tests the unified transformer with real components (but mocked data).
"""

import shutil
from pathlib import Path

import pytest

from src.etl.transformers.denue import DenuePipelineOrchestrator

# Path to the real seed files in the project
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SEEDS_DIR = PROJECT_ROOT / "data" / "seeds"


@pytest.fixture
def temp_transformer(tmp_path, tracker):
    """Create temporary transformer for testing."""
    data_root = tmp_path / "data"
    data_root.mkdir()

    # Create required subdirectories
    (data_root / "bronze" / "denue").mkdir(parents=True)
    seeds_dir = data_root / "seeds"
    seeds_dir.mkdir(parents=True)
    # Copy real seed parquet files into temp directory
    for seed_file in SEEDS_DIR.glob("*.parquet"):
        shutil.copy2(seed_file, seeds_dir / seed_file.name)

    transformer = DenuePipelineOrchestrator(data_root=data_root, tracker=tracker)

    return transformer


def test_transformer_initialization(temp_transformer):
    """El orquestador arma el ingest; los resolvers van bajo demanda.

    Cada resolver abre su archivo de log al nacer, así que construirlos
    todos en el `__init__` dejaba los logs de métodos que esa corrida no
    iba a usar.
    """
    assert temp_transformer.tracker is not None
    assert temp_transformer.transformer is not None
    assert temp_transformer._resolvers == {}


def test_el_resolver_se_crea_una_sola_vez_por_metodo(temp_transformer):
    """Pedirlo dos veces devuelve el mismo objeto, no uno nuevo."""
    primero = temp_transformer._resolver("radio")
    assert temp_transformer._resolver("radio") is primero
    assert temp_transformer._resolver("h3") is not primero
    assert set(temp_transformer._resolvers) == {"radio", "h3"}


def test_todo_metodo_del_registro_se_puede_construir(temp_transformer):
    """Agregar una entrada a METHODS no puede dejar el orquestador roto."""
    from src.etl.transformers.clee_resolver import CLEEResolver

    for metodo in CLEEResolver.METHODS:
        resolver = temp_transformer._resolver(metodo)
        assert resolver.method == metodo
        assert resolver.clee_table == CLEEResolver.METHODS[metodo]["table"]


@pytest.mark.postgres
def test_get_pipeline_status(temp_transformer):
    """El estado del pipeline se lee del schema `meta`, asi que exige servidor."""
    status = temp_transformer.get_pipeline_status()

    assert "latest_transform_run" in status
    assert "latest_clee_resolution_run" in status
    assert "process_metrics" in status
    assert "clee_resolution_metrics" in status


def test_transformer_repr(temp_transformer):
    """El repr dice de donde lee: su unica configuracion es la raiz de datos."""
    repr_str = repr(temp_transformer)
    assert "DenuePipelineOrchestrator" in repr_str
    assert str(temp_transformer.data_root) in repr_str
