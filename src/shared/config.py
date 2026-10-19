"""
Configuracion de la aplicacion con Pydantic Settings.

Un solo motor: PostgreSQL, una base con los schemas silver, gold y meta.
Las variables DUCKDB_* que quedan son legado de los stages que aun no se
reescriben (Fase 4), no una opcion de configuracion.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # El motor es PostgreSQL, punto. No hay flag de backend: un segundo
    # motor soportado significaria dos caminos de codigo, dos dialectos de
    # SQL y la posibilidad de que `make db-migrate` aplique el baseline
    # sobre un archivo por error.

    # -------------------------------------------------------------------------
    # DuckDB — LEGADO, se retira en la Fase 4
    # -------------------------------------------------------------------------
    # Siguen aqui porque los stages del ETL y la suite de tests aun operan
    # sobre archivos DuckDB. Equivalentes en PostgreSQL: DUCKDB_THREADS ->
    # PG_MAX_PARALLEL_WORKERS, DUCKDB_MEMORY_LIMIT -> PG_SHARED_BUFFERS +
    # PG_WORK_MEM (ver docs/entorno_linux.md §2).
    SILVER_DUCKDB_PATH: Path | None = None
    GOLD_DUCKDB_PATH: Path | None = None
    DUCKDB_THREADS: int = 8
    DUCKDB_MEMORY_LIMIT: str = "16GB"

    # Respaldo rotativo del warehouse antes de cada comando que lo modifica
    # (ver utils/backup.py). En APFS es un clon copy-on-write: instantaneo
    # y sin costo de espacio, por eso viene encendido por default.
    WAREHOUSE_BACKUP_ENABLED: bool = True

    # -------------------------------------------------------------------------
    # PostgreSQL Configuration
    # -------------------------------------------------------------------------
    # Una sola base con tres schemas (silver, gold, meta): ver
    # arquitectura.md §2.3. Desde el contenedor del ETL el host es el nombre
    # del servicio de compose ("postgres"); desde el host, "localhost".
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "mipymes"

    # Rol dueño del esquema: ETL, scheduler y Alembic.
    POSTGRES_USER: str = "etl_rw"
    POSTGRES_PASSWORD: str = ""

    # Rol de solo lectura del API: SELECT en gold y meta, nunca en silver.
    # Es la salvaguarda estructural de §2.3.1 — con este rol,
    # `establecimientos` sin calificar solo puede resolver al de gold, así
    # que el API no puede confundir los dos granos de la tabla.
    POSTGRES_RO_USER: str = "api_ro"
    POSTGRES_RO_PASSWORD: str = ""

    # Filas por bloque en `shared/db/copy.py`. Es la cota de RAM de las
    # escrituras masivas: el buffer CSV de un bloque vive entero en memoria
    # antes de irse al servidor, asi que RAM_pico ~ chunk x bytes_por_fila.
    # 500k x ~400 B = ~200 MiB por bloque, holgado en la workstation y
    # tolerable en una laptop. Subirlo ayuda poco (el cuello es el parseo
    # del servidor); bajarlo es la palanca si un ingest se queda sin RAM.
    COPY_CHUNK_ROWS: int = 500_000

    # SQLAlchemy Configuration
    SQLALCHEMY_ECHO: bool = False
    SQLALCHEMY_POOL_SIZE: int = 5
    SQLALCHEMY_MAX_OVERFLOW: int = 10
    SQLALCHEMY_POOL_PRE_PING: bool = True

    # -------------------------------------------------------------------------
    # SESNSP (incidencia delictiva municipal)
    # -------------------------------------------------------------------------
    # API CKAN de datos.gob.mx que describe el dataset "Incidencia delictiva".
    # El extractor la consulta para descubrir la URL del CSV del corte
    # vigente (el nombre de archivo cambia con cada corte mensual, p. ej.
    # IDM_NM_dic25.csv -> IDM_NM_jun26.csv), asi que nunca se fija la URL
    # del archivo en codigo.
    SESNSP_CKAN_PACKAGE_URL: str = (
        "https://www.datos.gob.mx/api/3/action/package_show?id=incidencia_delictiva"
    )
    # Nombre del recurso CKAN a descargar dentro del paquete.
    SESNSP_RESOURCE_NAME: str = "Incidencia delictiva municipal"
    # Override manual: si se define, el extractor se salta el descubrimiento
    # CKAN y descarga directamente esta URL (util si datos.gob.mx cambia de
    # estructura o para apuntar a un corte especifico).
    SESNSP_DOWNLOAD_URL: str | None = None

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
    @field_validator("SILVER_DUCKDB_PATH", mode="before")
    @classmethod
    def set_silver_duckdb_path(cls, v: Path | None, info) -> Path:
        """Set default Silver DuckDB path if not provided."""
        if v is None:
            silver_dir = info.data.get("SILVER_DIR")
            if silver_dir is None:
                data_dir = info.data.get("DATA_DIR", Path(__file__).parent.parent.parent / "data")
                silver_dir = data_dir / "silver"
            return silver_dir / "mipymes.duckdb"
        return Path(v) if not isinstance(v, Path) else v

    @field_validator("GOLD_DUCKDB_PATH", mode="before")
    @classmethod
    def set_gold_duckdb_path(cls, v: Path | None, info) -> Path:
        """Set default Gold DuckDB path if not provided."""
        if v is None:
            gold_dir = info.data.get("GOLD_DIR")
            if gold_dir is None:
                data_dir = info.data.get("DATA_DIR", Path(__file__).parent.parent.parent / "data")
                gold_dir = data_dir / "gold"
            return gold_dir / "mipymes.duckdb"
        return Path(v) if not isinstance(v, Path) else v

    # -------------------------------------------------------------------------
    # Computed Properties
    # -------------------------------------------------------------------------
    @computed_field
    @property
    def DATABASE_URL(self) -> str:
        """
        URL de conexion a la base: una sola para silver, gold y meta.

        No hay `SILVER_DATABASE_URL` ni `GOLD_DATABASE_URL` separadas --
        esa division existia cuando eran archivos distintos y es justo lo
        que el rediseno elimina (arquitectura.md §1.3). Las capas se
        distinguen por schema, no por conexion.

        Returns:
            str: SQLAlchemy-compatible database URL
        """
        return self._get_postgresql_url()

    @computed_field
    @property
    def DATABASE_URL_READ_ONLY(self) -> str:
        """
        Connection URL for the API's read-only role (`api_ro`).

        Separado de DATABASE_URL a proposito: el API nunca debe conectarse
        con el rol dueno. Con `api_ro` los GRANT solo cubren gold y meta, asi
        que un bug en un endpoint no puede tocar silver ni confundir los dos
        granos de `establecimientos` (arquitectura.md §2.3.1).

        Returns:
            str: SQLAlchemy-compatible read-only database URL
        """
        return self._get_postgresql_url(read_only=True)

    @computed_field
    @property
    def DATABASE_URL_ASYNC(self) -> str:
        """
        URL asincrona (driver asyncpg), para endpoints async del API.

        Returns:
            str: Async SQLAlchemy-compatible database URL
        """
        return self._get_postgresql_url(async_driver=True)

    # -------------------------------------------------------------------------
    # Helper Methods
    # -------------------------------------------------------------------------
    def _get_postgresql_url(
        self,
        async_driver: bool = False,
        read_only: bool = False,
    ) -> str:
        """
        Construct PostgreSQL URL with proper driver.

        Args:
            async_driver: If True, use asyncpg driver instead of psycopg3
            read_only: If True, use the api_ro role instead of the owner

        Returns:
            str: PostgreSQL connection URL
        """
        # `postgresql+psycopg` es psycopg3 (psycopg2 seria `+psycopg2`).
        # Requisito de `shared/db/copy.py`: COPY en formato binario.
        driver = "postgresql+asyncpg" if async_driver else "postgresql+psycopg"

        user = self.POSTGRES_RO_USER if read_only else self.POSTGRES_USER
        raw_password = self.POSTGRES_RO_PASSWORD if read_only else self.POSTGRES_PASSWORD

        if not user:
            role = "POSTGRES_RO_USER" if read_only else "POSTGRES_USER"
            raise ValueError(f"{role} must be set for PostgreSQL backend")

        # URL encode password if it contains special characters
        from urllib.parse import quote_plus

        password = quote_plus(raw_password) if raw_password else ""

        credentials = f"{user}:{password}" if password else user
        return (
            f"{driver}://{credentials}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )


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
