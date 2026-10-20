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
    (data_root / "silver").mkdir(parents=True)
    (data_root / "gold").mkdir(parents=True)

    # Copy real seed parquet files into temp directory
    for seed_file in SEEDS_DIR.glob("*.parquet"):
        shutil.copy2(seed_file, seeds_dir / seed_file.name)

    silver_db = data_root / "silver" / "mipymes.duckdb"
    gold_db = data_root / "gold" / "mipymes.duckdb"

    transformer = DenuePipelineOrchestrator(
        data_root=data_root,
        silver_db_path=silver_db,
        gold_db_path=gold_db,
        tracker=tracker,
    )

    return transformer


def test_transformer_initialization(temp_transformer):
    """Test that transformer initializes correctly."""
    assert temp_transformer is not None
    assert temp_transformer.tracker is not None
    assert temp_transformer.transformer is not None
    assert temp_transformer.clee_resolver is not None
    assert temp_transformer.loader is not None


@pytest.mark.postgres
def test_get_pipeline_status(temp_transformer):
    """El estado del pipeline se lee del schema `meta`, asi que exige servidor."""
    status = temp_transformer.get_pipeline_status()

    assert "latest_transform_run" in status
    assert "latest_load_run" in status
    assert "latest_clee_resolution_run" in status
    assert "process_metrics" in status
    assert "clee_resolution_metrics" in status
    assert "silver_db" in status
    assert "gold_db" in status


def test_transformer_repr(temp_transformer):
    """Test string representation."""
    repr_str = repr(temp_transformer)
    assert "DenuePipelineOrchestrator" in repr_str
    assert "data_root" in repr_str
    assert "silver" in repr_str
    assert "gold" in repr_str
