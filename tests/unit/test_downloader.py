"""Tests for Downloader and DownloadResult."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from src.etl.core.download_config import DownloadConfig
from src.etl.utils.downloader import Downloader, DownloadResult
from src.shared.enums import ErrorType


@pytest.mark.unit
class TestDownloadResult:
    """Test DownloadResult data class."""

    def test_success_result(self):
        result = DownloadResult(
            success=True,
            file_path=Path("test.zip"),
            size_bytes=1024,
            duration_seconds=2.5,
            download_speed_mbps=0.003,
            attempt_number=1,
        )
        assert result.success
        assert result.size_bytes == 1024
        assert result.attempt_number == 1

    def test_failure_result(self):
        result = DownloadResult(
            success=False,
            error_type=ErrorType.NETWORK_ERROR,
            error_message="Connection refused",
            attempt_number=3,
        )
        assert not result.success
        assert result.error_type == ErrorType.NETWORK_ERROR

    def test_to_dict_serialization(self):
        result = DownloadResult(
            success=True,
            file_path=Path("data.zip"),
            size_bytes=2048,
            duration_seconds=1.0,
            download_speed_mbps=0.016,
            attempt_number=1,
        )
        d = result.to_dict()
        assert d["success"] is True
        assert d["file_path"] == "data.zip"
        assert d["size_bytes"] == 2048
        assert d["error_type"] is None

    def test_to_dict_with_error(self):
        result = DownloadResult(
            success=False,
            error_type=ErrorType.HTTP_404,
            error_message="Not found",
        )
        d = result.to_dict()
        assert d["success"] is False
        assert d["error_type"] == "HTTP_404"
        assert d["error_message"] == "Not found"

    def test_repr_success(self):
        result = DownloadResult(
            success=True,
            file_path=Path("test.zip"),
            size_bytes=100,
        )
        r = repr(result)
        assert "SUCCESS" in r
        assert "test.zip" in r

    def test_repr_failure(self):
        result = DownloadResult(success=False)
        r = repr(result)
        assert "FAILED" in r


@pytest.mark.unit
class TestDownloaderInit:
    """Test Downloader initialization."""

    def test_default_config(self, tmp_path):
        config = DownloadConfig(
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        downloader = Downloader(config=config)
        assert downloader.config is config
        assert downloader.validator is not None

    def test_custom_validator(self, tmp_path):
        from src.etl.utils.file_validator import FileValidator

        config = DownloadConfig(
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        validator = FileValidator(hash_algorithm="md5")
        downloader = Downloader(config=config, validator=validator)
        assert downloader.validator is validator

    def test_session_has_user_agent(self, tmp_path):
        config = DownloadConfig(
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
            user_agent="TestAgent/1.0",
        )
        downloader = Downloader(config=config)
        assert downloader.session.headers["User-Agent"] == "TestAgent/1.0"

    def test_repr(self, tmp_path):
        config = DownloadConfig(
            max_retries=5,
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        downloader = Downloader(config=config)
        r = repr(downloader)
        assert "max_retries=5" in r


@pytest.mark.unit
class TestDownloaderContextManager:
    """Test context manager protocol."""

    def test_context_manager(self, tmp_path):
        config = DownloadConfig(
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        with Downloader(config=config) as d:
            assert d is not None
        # Session should be closed after exiting context


@pytest.mark.unit
class TestDownloaderErrorClassification:
    """Test error classification."""

    def test_connection_error(self, tmp_path):
        config = DownloadConfig(
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        d = Downloader(config=config)
        error_type, msg = d._classify_error(requests.exceptions.ConnectionError("refused"))
        assert error_type == ErrorType.NETWORK_ERROR
        assert "Connection error" in msg

    def test_timeout_error(self, tmp_path):
        config = DownloadConfig(
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        d = Downloader(config=config)
        error_type, msg = d._classify_error(requests.exceptions.Timeout("timed out"))
        assert error_type == ErrorType.HTTP_TIMEOUT
        assert "timeout" in msg.lower()

    def test_http_404_error(self, tmp_path):
        config = DownloadConfig(
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        d = Downloader(config=config)
        response = MagicMock()
        response.status_code = 404
        exc = requests.exceptions.HTTPError(response=response)
        error_type, msg = d._classify_error(exc)
        assert error_type == ErrorType.HTTP_404

    def test_http_500_error(self, tmp_path):
        config = DownloadConfig(
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        d = Downloader(config=config)
        response = MagicMock()
        response.status_code = 503
        exc = requests.exceptions.HTTPError(response=response)
        error_type, msg = d._classify_error(exc)
        assert error_type == ErrorType.HTTP_500

    def test_disk_full_error(self, tmp_path):
        config = DownloadConfig(
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        d = Downloader(config=config)
        error_type, msg = d._classify_error(OSError("No space left on device"))
        assert error_type == ErrorType.DISK_FULL

    def test_unknown_error(self, tmp_path):
        config = DownloadConfig(
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        d = Downloader(config=config)
        error_type, msg = d._classify_error(RuntimeError("something weird"))
        assert error_type == ErrorType.UNKNOWN
        assert "Unexpected" in msg

    def test_http_error_without_response(self, tmp_path):
        config = DownloadConfig(
            download_directory=tmp_path / "downloads",
            log_directory=tmp_path / "logs",
        )
        d = Downloader(config=config)
        exc = requests.exceptions.HTTPError("generic http error")
        exc.response = None
        error_type, msg = d._classify_error(exc)
        assert error_type == ErrorType.NETWORK_ERROR
