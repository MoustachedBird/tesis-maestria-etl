"""
Download configuration management.

Provides externalized configuration for download operations with
validation, environment variable support, and sensible defaults.
"""

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class DownloadConfig(BaseSettings):
    """
    Configuration for download operations.

    Supports environment variables with DOWNLOAD_ prefix and
    configuration files (YAML/JSON).

    Example:
        >>> config = DownloadConfig()
        >>> config.max_retries
        3
        >>>
        >>> # From environment
        >>> import os
        >>> os.environ['DOWNLOAD_MAX_RETRIES'] = '5'
        >>> config = DownloadConfig()
        >>> config.max_retries
        5
        >>>
        >>> # From file
        >>> config = DownloadConfig.from_yaml('config.yaml')
    """

    # Retry configuration
    max_retries: int = Field(
        default=3,
        description="Maximum number of retry attempts for failed downloads",
        ge=0,
        le=10,
    )

    retry_delay_seconds: int = Field(
        default=60,
        description="Base delay in seconds before retrying (uses exponential backoff)",
        ge=1,
        le=3600,
    )

    retry_backoff_multiplier: float = Field(
        default=2.0,
        description="Multiplier for exponential backoff (delay = base * multiplier ^ attempt)",
        ge=1.0,
        le=10.0,
    )

    # Timeout configuration
    timeout_seconds: int = Field(
        default=300,
        description="Request timeout in seconds (5 minutes default)",
        ge=10,
        le=3600,
    )

    connect_timeout_seconds: int = Field(
        default=10,
        description="Connection timeout in seconds",
        ge=1,
        le=60,
    )

    # Performance configuration
    max_parallel_downloads: int = Field(
        default=3,
        description="Maximum number of concurrent downloads",
        ge=1,
        le=10,
    )

    chunk_size_bytes: int = Field(
        default=8192,
        description="Download chunk size in bytes (8KB default)",
        ge=1024,
        le=1048576,  # Max 1MB chunks
    )

    # Validation configuration
    validate_downloads: bool = Field(
        default=True,
        description="Enable file validation after download",
    )

    hash_algorithm: str = Field(
        default="sha256",
        description="Hash algorithm for file validation",
    )

    size_tolerance_percent: float = Field(
        default=5.0,
        description="Acceptable file size variation percentage",
        ge=0.0,
        le=50.0,
    )

    # Storage configuration
    download_directory: Path = Field(
        default=Path("data/bronze/denue"),
        description="Directory for downloaded files",
    )

    temp_directory: Path | None = Field(
        default=None,
        description="Directory for temporary downloads (defaults to download_directory/.tmp)",
    )

    # Logging configuration
    log_level: str = Field(
        default="INFO",
        description="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
    )

    log_directory: Path = Field(
        default=Path("logs/downloads"),
        description="Directory for log files",
    )

    # HTTP configuration
    user_agent: str = Field(
        default="ETL-Core/1.0 (Data Pipeline)",
        description="User-Agent header for HTTP requests",
    )

    headers: dict[str, str] = Field(
        default_factory=dict,
        description="Additional HTTP headers",
    )

    # Feature flags
    enable_progress_bar: bool = Field(
        default=True,
        description="Show Rich progress bar in console",
    )

    enable_metadata_tracking: bool = Field(
        default=True,
        description="Registrar las descargas en el schema meta",
    )

    model_config = {
        "env_prefix": "DOWNLOAD_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @field_validator("hash_algorithm")
    @classmethod
    def validate_hash_algorithm(cls, v: str) -> str:
        """Validate hash algorithm is supported."""
        supported = ["md5", "sha1", "sha256", "sha512"]
        if v.lower() not in supported:
            raise ValueError(
                f"Hash algorithm '{v}' not supported. Supported: {', '.join(supported)}"
            )
        return v.lower()

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level."""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        v_upper = v.upper()
        if v_upper not in valid_levels:
            raise ValueError(f"Invalid log level '{v}'. Valid levels: {', '.join(valid_levels)}")
        return v_upper

    @field_validator("download_directory", "temp_directory", "log_directory")
    @classmethod
    def validate_path(cls, v: Path | None) -> Path | None:
        """Convert string paths to Path objects."""
        if v is None:
            return None
        if isinstance(v, str):
            return Path(v)
        return v

    def model_post_init(self, __context: Any) -> None:
        """Post-initialization setup."""
        # Set temp directory default if not specified
        if self.temp_directory is None:
            self.temp_directory = self.download_directory / ".tmp"

        # Ensure directories exist
        self.download_directory.mkdir(parents=True, exist_ok=True)
        self.temp_directory.mkdir(parents=True, exist_ok=True)
        self.log_directory.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_yaml(cls, path: Path | str) -> "DownloadConfig":
        """
        Load configuration from YAML file.

        Args:
            path: Path to YAML configuration file

        Returns:
            DownloadConfig instance

        Example:
            >>> config = DownloadConfig.from_yaml('config.yaml')
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Configuration file not found: {path}")

        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        return cls(**data)

    @classmethod
    def from_json(cls, path: Path | str) -> "DownloadConfig":
        """
        Load configuration from JSON file.

        Args:
            path: Path to JSON configuration file

        Returns:
            DownloadConfig instance

        Example:
            >>> config = DownloadConfig.from_json('config.json')
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Configuration file not found: {path}")

        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        return cls(**data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DownloadConfig":
        """
        Create configuration from dictionary.

        Args:
            data: Configuration dictionary

        Returns:
            DownloadConfig instance

        Example:
            >>> config = DownloadConfig.from_dict({'max_retries': 5})
        """
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        """
        Export configuration as dictionary.

        Returns:
            Configuration dictionary with serializable values
        """
        data = self.model_dump()
        # Convert Path objects to strings
        for key, value in data.items():
            if isinstance(value, Path):
                data[key] = str(value)
        return data

    def to_yaml(self, path: Path | str) -> None:
        """
        Save configuration to YAML file.

        Args:
            path: Output file path

        Example:
            >>> config.to_yaml('config.yaml')
        """
        path = Path(path)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(self.to_dict(), f, default_flow_style=False, indent=2)

    def to_json(self, path: Path | str, indent: int = 2) -> None:
        """
        Save configuration to JSON file.

        Args:
            path: Output file path
            indent: JSON indentation level

        Example:
            >>> config.to_json('config.json')
        """
        path = Path(path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=indent)

    def get_retry_delay(self, attempt: int) -> float:
        """
        Calculate retry delay with exponential backoff.

        Args:
            attempt: Retry attempt number (0-indexed)

        Returns:
            Delay in seconds

        Example:
            >>> config = DownloadConfig(retry_delay_seconds=10, retry_backoff_multiplier=2)
            >>> config.get_retry_delay(0)  # First retry
            10.0
            >>> config.get_retry_delay(1)  # Second retry
            20.0
            >>> config.get_retry_delay(2)  # Third retry
            40.0
        """
        return self.retry_delay_seconds * (self.retry_backoff_multiplier**attempt)

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"DownloadConfig("
            f"max_retries={self.max_retries}, "
            f"max_parallel={self.max_parallel_downloads}, "
            f"timeout={self.timeout_seconds}s, "
            f"validate={self.validate_downloads})"
        )
