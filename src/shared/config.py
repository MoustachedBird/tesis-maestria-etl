"""Configuracion de la aplicacion con Pydantic Settings.

Un solo motor: PostgreSQL, una base con los schemas silver, gold y meta.
No hay flag de backend ni rutas de archivo: la unica forma de llegar a los
datos es la conexion que se arma aqui.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import computed_field
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

    # Lo unico que sigue siendo archivo es la entrada: los ZIP de Bronze y
    # los seeds versionados. Silver y Gold viven en la base.
    DATA_DIR: Path = PROJECT_ROOT / "data"
    BRONZE_DIR: Path = DATA_DIR / "bronze"
    SEEDS_DIR: Path = DATA_DIR / "seeds"

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
    # DENUE (scraping de la tabla de descargas de INEGI)
    # -------------------------------------------------------------------------
    # Segundos a esperar a que la tabla `tblDescargaArchivos_denue` termine
    # de poblarse por AJAX. No basta con que el elemento exista: INEGI
    # renderiza primero unas pocas filas placeholder y despues inyecta el
    # catalogo completo (~484 archivos), asi que la espera es por contenido
    # (filas con enlace csv.zip) y este es su tope.
    DENUE_SCRAPE_TIMEOUT_SECONDS: int = 120
    # Minimo de filas con enlace .zip para considerar el scrape completo.
    # El catalogo trae ~484; cualquier cosa por debajo de esto es una tabla
    # a medio renderizar o un cambio de markup, y se trata como error en
    # lugar de "no hay archivos nuevos".
    DENUE_SCRAPE_MIN_LINKS: int = 100

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

    # -------------------------------------------------------------------------
    # Helper Methods
    # -------------------------------------------------------------------------
    def _get_postgresql_url(self, read_only: bool = False) -> str:
        """
        Construct PostgreSQL URL with proper driver.

        Args:
            read_only: If True, use the api_ro role instead of the owner

        Returns:
            str: PostgreSQL connection URL
        """
        # `postgresql+psycopg` es psycopg3 (psycopg2 seria `+psycopg2`):
        # lo pide `shared/db/copy.py` para el COPY por bloques.
        driver = "postgresql+psycopg"

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
