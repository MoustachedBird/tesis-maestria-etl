# src/core/config.py
"""
Configuration management using Pydantic Settings.
Supports both PostgreSQL and DuckDB backends with proper URL construction.
"""

from pydantic import field_validator, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path
from typing import Literal, Optional
from functools import lru_cache


class Settings(BaseSettings):
    """
    Application settings with environment variable support.

    Environment variables can be set in .env file or system environment.
    All settings are case-insensitive when reading from environment.
    """

    # -------------------------------------------------------------------------
    # Project Paths
    # -------------------------------------------------------------------------
    PROJECT_ROOT: Path = Path(__file__).parent.parent.parent

    # Data directories
    DATA_DIR: Path = PROJECT_ROOT / "data"
    BRONZE_DIR: Path = DATA_DIR / "bronze"
    SILVER_DIR: Path = DATA_DIR / "silver"
    GOLD_DIR: Path = DATA_DIR / "gold"
    SEEDS_DIR: Path = DATA_DIR / "seeds"

    # Logs
    LOGS_DIR: Path = PROJECT_ROOT / "logs"

    # -------------------------------------------------------------------------
    # Database Configuration
    # -------------------------------------------------------------------------
    DATABASE_BACKEND: Literal["duckdb", "postgresql"] = "duckdb"

    # DuckDB Configuration
    DUCKDB_PATH: Optional[Path] = None
    DUCKDB_THREADS: int = 8
    DUCKDB_MEMORY_LIMIT: str = "16GB"

    # PostgreSQL Configuration
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "denue"
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = ""

    # SQLAlchemy Configuration
    SQLALCHEMY_ECHO: bool = False
    SQLALCHEMY_POOL_SIZE: int = 5
    SQLALCHEMY_MAX_OVERFLOW: int = 10
    SQLALCHEMY_POOL_PRE_PING: bool = True

    # -------------------------------------------------------------------------
    # Application Settings
    # -------------------------------------------------------------------------
    # Logging
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # API
    API_TITLE: str = "DENUE ETL API"
    API_VERSION: str = "0.1.0"
    API_PREFIX: str = "/api/v1"

    # -------------------------------------------------------------------------
    # Validators
    # -------------------------------------------------------------------------
    @field_validator("DUCKDB_PATH", mode="before")
    @classmethod
    def set_duckdb_path(cls, v: Optional[Path], info) -> Path:
        """Set default DuckDB path if not provided."""
        if v is None:
            # Get GOLD_DIR from the data being validated
            gold_dir = info.data.get("GOLD_DIR")
            if gold_dir is None:
                data_dir = info.data.get(
                    "DATA_DIR", Path(__file__).parent.parent.parent / "data"
                )
                gold_dir = data_dir / "gold"
            return gold_dir / "denue.duckdb"
        return Path(v) if not isinstance(v, Path) else v

    # -------------------------------------------------------------------------
    # Computed Properties
    # -------------------------------------------------------------------------
    @computed_field
    @property
    def DATABASE_URL(self) -> str:
        """
        Get the database URL based on the selected backend.

        Returns:
            str: SQLAlchemy-compatible database URL
        """
        if self.DATABASE_BACKEND == "postgresql":
            return self._get_postgresql_url()
        elif self.DATABASE_BACKEND == "duckdb":
            return self._get_duckdb_url()
        else:
            raise ValueError(f"Unsupported database backend: {self.DATABASE_BACKEND}")

    @computed_field
    @property
    def DATABASE_URL_ASYNC(self) -> str:
        """
        Get async database URL for async SQLAlchemy (PostgreSQL only).

        Returns:
            str: Async SQLAlchemy-compatible database URL
        """
        if self.DATABASE_BACKEND == "postgresql":
            return self._get_postgresql_url(async_driver=True)
        else:
            # DuckDB doesn't support async yet
            return self.DATABASE_URL

    # -------------------------------------------------------------------------
    # Helper Methods
    # -------------------------------------------------------------------------
    def _get_postgresql_url(self, async_driver: bool = False) -> str:
        """
        Construct PostgreSQL URL with proper driver.

        Args:
            async_driver: If True, use asyncpg driver instead of psycopg2

        Returns:
            str: PostgreSQL connection URL
        """
        driver = "postgresql+asyncpg" if async_driver else "postgresql+psycopg2"

        if not self.POSTGRES_USER:
            raise ValueError("POSTGRES_USER must be set for PostgreSQL backend")

        # URL encode password if it contains special characters
        from urllib.parse import quote_plus

        password = quote_plus(self.POSTGRES_PASSWORD) if self.POSTGRES_PASSWORD else ""

        if password:
            return (
                f"{driver}://{self.POSTGRES_USER}:{password}"
                f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
            )
        else:
            return (
                f"{driver}://{self.POSTGRES_USER}"
                f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
            )

    def _get_duckdb_url(self) -> str:
        """
        Construct DuckDB URL.

        Returns:
            str: DuckDB connection URL
        """
        # Ensure parent directory exists
        if self.DUCKDB_PATH:
            self.DUCKDB_PATH.parent.mkdir(parents=True, exist_ok=True)
            return f"duckdb:///{self.DUCKDB_PATH}"
        raise ValueError("DUCKDB_PATH must be set for DuckDB backend")

    # -------------------------------------------------------------------------
    # Model Configuration
    # -------------------------------------------------------------------------
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        validate_default=True,
    )


@lru_cache
def get_settings() -> Settings:
    """
    Get cached settings instance.

    This function is cached to ensure we only create one Settings instance.
    Use this in FastAPI dependencies.

    Returns:
        Settings: The application settings
    """
    return Settings()


# Singleton instance for backward compatibility
settings = get_settings()
