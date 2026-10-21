"""
Generic downloader with retry logic and validation.

Provides reusable download functionality with:
- Exponential backoff retry logic
- Streaming downloads for memory efficiency
- Progress callback support (Rich progress bars)
- Automatic validation
- Error classification
"""

import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
import structlog

from src.etl.core.download_config import DownloadConfig
from src.etl.utils.file_validator import FileValidator, ValidationResult
from src.shared.enums import ErrorType

logger = structlog.get_logger(__name__)


class DownloadResult:
    """
    Result of a download operation.

    Attributes:
        success: Whether download succeeded
        file_path: Path to downloaded file
        size_bytes: File size in bytes
        duration_seconds: Download duration
        download_speed_mbps: Download speed in Mbps
        validation_result: File validation result
        error_type: Error classification (if failed)
        error_message: Error details (if failed)
        attempt_number: Number of attempts made
    """

    def __init__(
        self,
        success: bool,
        file_path: Path | None = None,
        size_bytes: int | None = None,
        duration_seconds: float | None = None,
        download_speed_mbps: float | None = None,
        validation_result: ValidationResult | None = None,
        error_type: ErrorType | None = None,
        error_message: str | None = None,
        attempt_number: int = 1,
    ):
        self.success = success
        self.file_path = file_path
        self.size_bytes = size_bytes
        self.duration_seconds = duration_seconds
        self.download_speed_mbps = download_speed_mbps
        self.validation_result = validation_result
        self.error_type = error_type
        self.error_message = error_message
        self.attempt_number = attempt_number

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "file_path": str(self.file_path) if self.file_path else None,
            "size_bytes": self.size_bytes,
            "duration_seconds": self.duration_seconds,
            "download_speed_mbps": self.download_speed_mbps,
            "validation_passed": self.validation_result.is_valid
            if self.validation_result
            else None,
            "error_type": self.error_type.value if self.error_type else None,
            "error_message": self.error_message,
            "attempt_number": self.attempt_number,
        }

    def __repr__(self) -> str:
        status = "SUCCESS" if self.success else "FAILED"
        return f"DownloadResult({status}, {self.file_path}, {self.size_bytes} bytes)"


class Downloader:
    """
    Generic downloader with retry logic and validation.

    Features:
    - Automatic retry with exponential backoff
    - Streaming downloads for large files
    - Progress callbacks for UI updates
    - File validation (hash, size, ZIP integrity)
    - Error classification for retry logic

    Example:
        >>> config = DownloadConfig(max_retries=3)
        >>> downloader = Downloader(config)
        >>> result = downloader.download(
        ...     url="https://example.com/data.zip",
        ...     target_path=Path("data.zip"),
        ... )
        >>> if result.success:
        ...     print(f"Downloaded {result.size_bytes} bytes")
    """

    def __init__(
        self,
        config: DownloadConfig | None = None,
        validator: FileValidator | None = None,
    ):
        """
        Initialize downloader.

        Args:
            config: Download configuration (uses defaults if not provided)
            validator: File validator (creates default if not provided)
        """
        self.config = config or DownloadConfig()
        self.validator = validator or FileValidator(
            hash_algorithm=self.config.hash_algorithm,
            size_tolerance_percent=self.config.size_tolerance_percent,
        )

        # Setup session with headers
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.config.user_agent,
                **self.config.headers,
            }
        )

    def download(
        self,
        url: str,
        target_path: Path,
        expected_size_bytes: int | None = None,
        expected_hash: str | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> DownloadResult:
        """
        Download file with retry logic and validation.

        Args:
            url: URL to download from
            target_path: Where to save the file
            expected_size_bytes: Expected file size for validation
            expected_hash: Expected hash for validation
            progress_callback: Callback function(downloaded_bytes, total_bytes)

        Returns:
            DownloadResult with success status and metadata

        Example:
            >>> def progress(downloaded, total):
            ...     print(f"Progress: {downloaded}/{total}")
            >>> result = downloader.download(
            ...     url="https://example.com/data.zip",
            ...     target_path=Path("data.zip"),
            ...     progress_callback=progress,
            ... )
        """
        log = logger.bind(url=url, target_path=str(target_path))
        log.info("download_started")

        start_time = datetime.now()
        attempt = 0

        try:
            # Download with retry logic
            file_path, size_bytes, attempts = self._download_with_retry(
                url=url,
                target_path=target_path,
                progress_callback=progress_callback,
            )
            attempt = attempts

            # Calculate metrics
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            speed_mbps = (size_bytes * 8 / 1_000_000) / duration if duration > 0 else 0

            # Validate if enabled
            validation_result = None
            if self.config.validate_downloads:
                log.info("validation_started")
                validation_result = self.validator.validate(
                    file_path=file_path,
                    expected_size_bytes=expected_size_bytes,
                    expected_hash=expected_hash,
                    validate_zip=True,
                )

                if not validation_result.is_valid:
                    log.error(
                        "validation_failed",
                        checks_failed=validation_result.checks_failed,
                        error_message=validation_result.error_message,
                    )

                    # Delete invalid file
                    if file_path.exists():
                        file_path.unlink()

                    return DownloadResult(
                        success=False,
                        file_path=file_path,
                        size_bytes=size_bytes,
                        duration_seconds=duration,
                        download_speed_mbps=speed_mbps,
                        validation_result=validation_result,
                        error_type=validation_result.error_type,
                        error_message=validation_result.error_message,
                        attempt_number=attempt,
                    )

                log.info("validation_passed")

            log.info(
                "download_completed",
                size_bytes=size_bytes,
                duration_seconds=duration,
                speed_mbps=speed_mbps,
                attempts=attempt,
            )

            return DownloadResult(
                success=True,
                file_path=file_path,
                size_bytes=size_bytes,
                duration_seconds=duration,
                download_speed_mbps=speed_mbps,
                validation_result=validation_result,
                attempt_number=attempt,
            )

        except Exception as e:
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()

            error_type, error_message = self._classify_error(e)

            log.error(
                "download_failed",
                error_type=error_type.value,
                error_message=error_message,
                duration_seconds=duration,
                attempts=attempt,
            )

            return DownloadResult(
                success=False,
                file_path=target_path,
                duration_seconds=duration,
                error_type=error_type,
                error_message=error_message,
                attempt_number=attempt,
            )

    def _download_with_retry(
        self,
        url: str,
        target_path: Path,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> tuple[Path, int, int]:
        """
        Download file with exponential backoff retry.

        Args:
            url: URL to download
            target_path: Target file path
            progress_callback: Progress callback function

        Returns:
            Tuple of (file_path, size_bytes, attempt_number)

        Raises:
            Exception: If download fails after all retries
        """
        attempt = 1
        last_exception = None

        for attempt in range(1, self.config.max_retries + 2):
            try:
                result = self._download_file(url, target_path, progress_callback)
                return (*result, attempt)
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError,
            ) as e:
                last_exception = e
                if attempt >= self.config.max_retries + 1:
                    raise
                # Wait before retry
                wait_time = self.config.get_retry_delay(attempt - 1)
                time.sleep(wait_time)
            except Exception:
                # Non-retryable errors
                raise

        # Should never reach here, but satisfy type checker
        if last_exception:
            raise last_exception
        raise RuntimeError("Download failed with unknown error")

    def _download_file(
        self,
        url: str,
        target_path: Path,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> tuple[Path, int]:
        """
        Download file to target path (single attempt).

        Args:
            url: URL to download
            target_path: Target file path
            progress_callback: Progress callback

        Returns:
            Tuple of (file_path, size_bytes)
        """
        # Ensure parent directory exists
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # Download to temp file first
        assert self.config.temp_directory is not None, "temp_directory should be set in config"
        temp_path = self.config.temp_directory / f"{target_path.name}.tmp"
        temp_path.parent.mkdir(parents=True, exist_ok=True)

        # Stream download
        response = self.session.get(
            url,
            stream=True,
            timeout=(
                self.config.connect_timeout_seconds,
                self.config.timeout_seconds,
            ),
        )
        response.raise_for_status()

        # Get total size from headers
        total_size = int(response.headers.get("content-length", 0))

        # Download in chunks
        downloaded = 0
        with open(temp_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=self.config.chunk_size_bytes):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)

                    # Call progress callback
                    if progress_callback:
                        progress_callback(downloaded, total_size)

        # Move to final location
        if target_path.exists():
            target_path.unlink()
        temp_path.rename(target_path)

        return (target_path, downloaded)

    def _classify_error(self, exception: Exception) -> tuple[ErrorType, str]:
        """
        Classify exception into ErrorType for retry logic.

        Args:
            exception: Exception that occurred

        Returns:
            Tuple of (ErrorType, error_message)
        """
        error_message = str(exception)

        # Network errors
        if isinstance(exception, requests.exceptions.ConnectionError):
            return (ErrorType.NETWORK_ERROR, f"Connection error: {error_message}")

        # Timeout errors
        if isinstance(exception, requests.exceptions.Timeout):
            return (ErrorType.HTTP_TIMEOUT, f"Request timeout: {error_message}")

        # HTTP errors
        if isinstance(exception, requests.exceptions.HTTPError):
            if hasattr(exception, "response") and exception.response is not None:
                status_code = exception.response.status_code
                if status_code == 404:
                    return (
                        ErrorType.HTTP_404,
                        f"File not found (404): {error_message}",
                    )
                elif 500 <= status_code < 600:
                    return (
                        ErrorType.HTTP_500,
                        f"Server error ({status_code}): {error_message}",
                    )
            return (ErrorType.NETWORK_ERROR, f"HTTP error: {error_message}")

        # Disk errors
        if isinstance(exception, OSError):
            if "No space left" in error_message or "Disk full" in error_message:
                return (ErrorType.DISK_FULL, f"Disk full: {error_message}")

        # Unknown errors
        return (ErrorType.UNKNOWN, f"Unexpected error: {error_message}")

    def close(self):
        """Close HTTP session."""
        self.session.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()

    def __repr__(self) -> str:
        """String representation."""
        return f"Downloader(max_retries={self.config.max_retries})"
