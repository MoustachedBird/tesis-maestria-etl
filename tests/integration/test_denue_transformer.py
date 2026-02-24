"""
Integration tests for DenueTransformer.

Tests the unified transformer with real components (but mocked data).
"""

import pytest
import shutil
from pathlib import Path
from src.transformers.denue_transformer import DenueTransformer

# Path to the real seed files in the project
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SEEDS_DIR = PROJECT_ROOT / "data" / "seeds"


@pytest.fixture
def temp_transformer(tmp_path):
    """Create temporary transformer for testing."""
    data_root = tmp_path / "data"
    data_root.mkdir()

    # Create required subdirectories
    (data_root / "raw" / "denue").mkdir(parents=True)
    seeds_dir = data_root / "seeds"
    seeds_dir.mkdir(parents=True)
    (data_root / "processed").mkdir(parents=True)
    (data_root / "curated").mkdir(parents=True)

    # Copy real seed parquet files into temp directory
    for seed_file in SEEDS_DIR.glob("*.parquet"):
        shutil.copy2(seed_file, seeds_dir / seed_file.name)

    metadata_db = data_root / "metadata.duckdb"

    transformer = DenueTransformer(
        data_root=data_root,
        metadata_db=metadata_db,
        processed_output_dir=data_root / "processed",
        curated_output_dir=data_root / "curated",
    )

    return transformer


def test_transformer_initialization(temp_transformer):
    """Test that transformer initializes correctly."""
    assert temp_transformer is not None
    assert temp_transformer.tracker is not None
    assert temp_transformer.processor is not None
    assert temp_transformer.harmonizer is not None


def test_get_pipeline_status(temp_transformer):
    """Test getting pipeline status."""
    status = temp_transformer.get_pipeline_status()

    assert "latest_process_run" in status
    assert "latest_harmonize_run" in status
    assert "process_metrics" in status
    assert "harmonize_metrics" in status
    assert "metadata_db" in status


def test_transformer_repr(temp_transformer):
    """Test string representation."""
    repr_str = repr(temp_transformer)
    assert "DenueTransformer" in repr_str
    assert "data_root" in repr_str
