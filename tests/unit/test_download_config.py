"""Tests for DownloadConfig."""

import json

import pytest
import yaml

from src_etl.core.download_config import DownloadConfig


@pytest.mark.unit
class TestDownloadConfigDefaults:
    """Test default configuration values."""

    def test_default_max_retries(self, tmp_path):
        config = DownloadConfig(
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        assert config.max_retries == 3

    def test_default_timeout(self, tmp_path):
        config = DownloadConfig(
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        assert config.timeout_seconds == 300

    def test_default_parallel_downloads(self, tmp_path):
        config = DownloadConfig(
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        assert config.max_parallel_downloads == 3

    def test_default_hash_algorithm(self, tmp_path):
        config = DownloadConfig(
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        assert config.hash_algorithm == "sha256"

    def test_default_validate_downloads_enabled(self, tmp_path):
        config = DownloadConfig(
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        assert config.validate_downloads is True

    def test_temp_directory_defaults_to_download_subdir(self, tmp_path):
        config = DownloadConfig(
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        assert config.temp_directory == tmp_path / "downloads" / ".tmp"


@pytest.mark.unit
class TestDownloadConfigValidation:
    """Test field validation."""

    def test_invalid_hash_algorithm_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="not supported"):
            DownloadConfig(
                hash_algorithm="blake2",
                download_directory=tmp_path / "downloads",
                log_directory=tmp_path / "logs",
            )

    def test_valid_hash_algorithms_accepted(self, tmp_path):
        for algo in ["md5", "sha1", "sha256", "sha512"]:
            config = DownloadConfig(
                hash_algorithm=algo,
                download_directory=tmp_path / "downloads",
                log_directory=tmp_path / "logs",
            )
            assert config.hash_algorithm == algo

    def test_invalid_log_level_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="Invalid log level"):
            DownloadConfig(
                log_level="VERBOSE",
                download_directory=tmp_path / "downloads",
                log_directory=tmp_path / "logs",
            )

    def test_valid_log_levels_accepted(self, tmp_path):
        for level in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
            config = DownloadConfig(
                log_level=level,
                download_directory=tmp_path / "downloads",
                log_directory=tmp_path / "logs",
            )
            assert config.log_level == level

    def test_log_level_normalized_to_uppercase(self, tmp_path):
        config = DownloadConfig(
            log_level="info",
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        assert config.log_level == "INFO"

    def test_max_retries_bounds(self, tmp_path):
        with pytest.raises(ValueError):
            DownloadConfig(
                max_retries=-1,
                download_directory=tmp_path / "downloads",
                log_directory=tmp_path / "logs",
            )
        with pytest.raises(ValueError):
            DownloadConfig(
                max_retries=11,
                download_directory=tmp_path / "downloads",
                log_directory=tmp_path / "logs",
            )


@pytest.mark.unit
class TestDownloadConfigRetryDelay:
    """Test exponential backoff calculation."""

    def test_first_retry_uses_base_delay(self, tmp_path):
        config = DownloadConfig(
            retry_delay_seconds=10,
            retry_backoff_multiplier=2.0,
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        assert config.get_retry_delay(0) == 10.0

    def test_second_retry_applies_multiplier(self, tmp_path):
        config = DownloadConfig(
            retry_delay_seconds=10,
            retry_backoff_multiplier=2.0,
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        assert config.get_retry_delay(1) == 20.0

    def test_third_retry_squares_multiplier(self, tmp_path):
        config = DownloadConfig(
            retry_delay_seconds=10,
            retry_backoff_multiplier=2.0,
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        assert config.get_retry_delay(2) == 40.0


@pytest.mark.unit
class TestDownloadConfigSerialization:
    """Test config serialization/deserialization."""

    def test_to_dict_returns_serializable(self, tmp_path):
        config = DownloadConfig(
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        data = config.to_dict()
        assert isinstance(data, dict)
        assert data["max_retries"] == 3
        # Paths should be strings
        assert isinstance(data["download_directory"], str)

    def test_from_dict_roundtrip(self, tmp_path):
        original = DownloadConfig(
            max_retries=5,
            timeout_seconds=120,
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        data = original.to_dict()
        restored = DownloadConfig.from_dict(data)
        assert restored.max_retries == 5
        assert restored.timeout_seconds == 120

    def test_to_yaml_creates_file(self, tmp_path):
        config = DownloadConfig(
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        yaml_path = tmp_path / "config.yaml"
        config.to_yaml(yaml_path)
        assert yaml_path.exists()

        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        assert data["max_retries"] == 3

    def test_from_yaml_roundtrip(self, tmp_path):
        config = DownloadConfig(
            max_retries=7,
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        yaml_path = tmp_path / "config.yaml"
        config.to_yaml(yaml_path)

        restored = DownloadConfig.from_yaml(yaml_path)
        assert restored.max_retries == 7

    def test_from_yaml_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            DownloadConfig.from_yaml(tmp_path / "nonexistent.yaml")

    def test_to_json_creates_file(self, tmp_path):
        config = DownloadConfig(
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        json_path = tmp_path / "config.json"
        config.to_json(json_path)
        assert json_path.exists()

        with open(json_path) as f:
            data = json.load(f)
        assert data["max_retries"] == 3

    def test_from_json_roundtrip(self, tmp_path):
        config = DownloadConfig(
            max_retries=4,
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        json_path = tmp_path / "config.json"
        config.to_json(json_path)

        restored = DownloadConfig.from_json(json_path)
        assert restored.max_retries == 4

    def test_from_json_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            DownloadConfig.from_json(tmp_path / "nonexistent.json")

    def test_repr(self, tmp_path):
        config = DownloadConfig(
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        r = repr(config)
        assert "DownloadConfig" in r
        assert "max_retries=3" in r


@pytest.mark.unit
class TestDownloadConfigDirectories:
    """Test directory creation behavior."""

    def test_creates_download_directory(self, tmp_path):
        dl_dir = tmp_path / "new_dir" / "downloads"
        DownloadConfig(
            download_directory=dl_dir,
            log_directory=tmp_path / "logs",
        )
        assert dl_dir.exists()

    def test_creates_log_directory(self, tmp_path):
        log_dir = tmp_path / "new_dir" / "logs"
        DownloadConfig(
            download_directory=tmp_path / "downloads",
            log_directory=log_dir,
        )
        assert log_dir.exists()

    def test_creates_temp_directory(self, tmp_path):
        config = DownloadConfig(
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        assert config.temp_directory.exists()

    def test_custom_temp_directory(self, tmp_path):
        custom_tmp = tmp_path / "custom_tmp"
        config = DownloadConfig(
            download_directory=tmp_path / "downloads",
            temp_directory=custom_tmp,
            log_directory=tmp_path / "logs",
        )
        assert config.temp_directory == custom_tmp
        assert custom_tmp.exists()
