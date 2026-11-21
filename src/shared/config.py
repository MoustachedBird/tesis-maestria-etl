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

    # Segundos entre latidos del loader de permanencia. Su `INSERT ... SELECT`
    # sobre 82.9M filas es UNA sentencia que corre por horas sin que Python
    # tenga nada que reportar, asi que el latido lo emite un hilo que consulta
    # `pg_stat_activity`. Subirlo ensucia menos el log; bajarlo da mas
    # resolucion para ver en que fase esta. En 0 se apaga el hilo.
    PERMANENCIA_LATIDO_SEGUNDOS: int = 60

    # Bloques de identidades en que se trocea la permanencia. Cada bloque es
    # su propia transaccion, asi que un fallo cuesta a lo mas 1/N del
    # trabajo en vez de todo. Ademas suele ser mas RAPIDO: el GROUP BY
    # completo ordena 10M de grupos y se derrama a disco, mientras que los
    # ~300k de un bloque caben en work_mem.
    # Divisor de 256 para que el reparto por md5 sea uniforme. En 1 se hace
    # de una sola vez, como antes.
    PERMANENCIA_BLOQUES: int = 32

    # Cobertura minima de `clee` para considerar OFICIAL a un corte, y con eso
    # dejarlo entrar a la ventana de gold.negocios_clee_oficial. Es el mismo
    # 99.9% que ya usaba scripts/evaluate_masked_clee.py, adoptado para que
    # las dos partes del proyecto llamen oficial a lo mismo.
    # Medido el 2026-11-08 la cobertura es exactamente 0% o exactamente 100%,
    # sin cortes parciales, asi que hoy el umbral no decide nada: es la guarda
    # para el dia en que INEGI publique un corte a medias.
    GOLD_COBERTURA_CLEE_MINIMA: float = 0.999

    # MESES sin aparecer en el DENUE a partir de los cuales Gold declara que
    # un negocio cerro. Es la regla que la tabla existe para VALIDAR: el DENUE
    # no reporta cierres, solo deja de listar, asi que `cerrado` es una
    # hipotesis y la muestra verificada con agentes es la prueba.
    # Se mide en meses y no en cortes porque los cortes no son equidistantes
    # (6, 6, 6, 12, 6, 6, 6, 12 meses): "un corte ausente" vale el doble de
    # evidencia para unos negocios que para otros. 12 meses son dos ciclos
    # de la cadencia semestral del DENUE, que es la regla preliminar del
    # protocolo ("dos cortes consecutivos") expresada en tiempo. Bajarlo
    # detecta mas cierres con mas falsos positivos; subirlo exige mas
    # evidencia a cambio de perder los cierres recientes por censura.
    # `meses_sin_aparecer` y `huecos` quedan en la tabla, asi que cambiar esto
    # permite recalcular la columna sin reconstruir los 9.58M de filas.
    CIERRE_MESES_AUSENTE: int = 12

    # Version de las reglas de normalizacion con que se construye Gold. Queda
    # en cada fila (`version_reglas`): cambiar una regla cambia los numeros que
    # reporta la tesis, asi que la fila tiene que decir bajo que reglas nacio.
    GOLD_VERSION_REGLAS: str = "v1"

    # -------------------------------------------------------------------------
    # Agente de supervivencia (verificacion_cierre.md §5, crear_agentes.md §9)
    # -------------------------------------------------------------------------
    # Donde escucha Ollama: el servicio `etl-ollama` de docker-compose.yml, con
    # la GPU, visto por nombre dentro de la red de compose.
    OLLAMA_URL: str = "http://etl-ollama:11434"
    # Modelo de pesos abiertos con llamada de herramientas. Queda grabado en
    # cada veredicto (`agente = modelo/version`), asi que comparar dos modelos
    # sobre la misma muestra es correr dos veces con distinto valor aqui.
    # Opciones medidas para 32 GB de VRAM: qwen3:32b (default, mejor espanol
    # y tool-calling), gpt-oss:20b (mas rapido), mistral-small3.1:24b.
    OLLAMA_MODEL: str = "qwen3:32b"
    # Segundos maximos por llamada al modelo. Un 32B con 30k de contexto puede
    # tardar minutos en una vuelta; esto no es el timeout de un API.
    OLLAMA_TIMEOUT_SEGUNDOS: int = 600
    # Ventana de contexto que se pide. El default de Ollama (4k) no alcanza
    # para el insumo mas dos paginas leidas; 32k cabe en la 5090 con un 32B
    # en Q4. Subirlo cuesta VRAM y tiempo por vuelta.
    OLLAMA_CONTEXTO_TOKENS: int = 32768
    # Si el modelo razona antes de responder (`think`). Mejora las decisiones
    # dificiles a cambio de 2-5x de tokens por vuelta. Apagado para el piloto;
    # encenderlo es un experimento que se compara por `agente`.
    OLLAMA_PENSAR: bool = False
    # Version del agente (prompt + protocolo). Sube cuando cambie el prompt o
    # las reglas de decision: dos veredictos con distinta version no son
    # comparables aunque sean del mismo modelo.
    AGENTE_VERSION: str = "v5"
    # Las fuentes que se consultan, por nombre de `Recolector`, separadas por
    # coma. El orden importa solo dentro de un especialista; entre grupos
    # manda el orden fijo de `Especialidad` (los buscadores van antes que
    # redes, plataformas y archivo, que abren lo que ellos encontraron).
    # Quitar una fuente cambia el expediente: sube AGENTE_VERSION con ello.
    # `google` y `duckduckgo` existen pero no van por default: en indominus
    # bloquean desde la primera consulta (sondeo del 2026-11-21); sus
    # resultados llegan igual por los motores de SearXNG (`web`).
    AGENTE_FUENTES: str = (
        "maps,osm,web,bing,brave,redes,plataformas,sitio,wayback,common_crawl,dominio,sat"
    )
    # Segundos minimos entre dos peticiones al mismo dominio (mas un azar de
    # hasta la mitad). Es lo que mantiene a Google, Bing y Maps sin CAPTCHA
    # a volumen de muestra; el tiempo no es la restriccion, el bloqueo si.
    AGENTE_PAUSA_SEGUNDOS: float = 3.0
    # Bloqueos SEGUIDOS (CAPTCHA, "trafico inusual") de un motor antes de
    # apagarlo por el resto de la corrida. Cada consulta saltada queda como
    # pieza ERROR, asi que el hueco se puede medir.
    AGENTE_MAX_BLOQUEOS: int = 3
    # Perfil de Chrome persistente, SIN sesion: guarda solo cookies de
    # consentimiento, para no toparse con "Antes de ir a Google" en cada
    # consulta. Vacio = un perfil desechable por corrida.
    AGENTE_PERFIL_CHROME: str = "data/agente/chrome"
    # Fichas de plataformas (TripAdvisor, Yelp, Rappi...) que se abren de las
    # que trajeron los buscadores.
    AGENTE_MAX_PLATAFORMAS: int = 3
    # URLs (sitio propio y redes) cuyo historial se busca en el Internet
    # Archive y en Common Crawl.
    AGENTE_MAX_ARCHIVO: int = 3
    # Crawls mas recientes de Common Crawl que se consultan (uno por mes o
    # dos, aproximadamente). Cada uno es una peticion lenta.
    AGENTE_CRAWLS_COMMON_CRAWL: int = 3
    # Radio alrededor de las coordenadas del DENUE en que se buscan objetos
    # de OpenStreetMap. El GPS del DENUE se desvia decenas de metros.
    AGENTE_RADIO_METROS: int = 150
    # Dias que vale la copia local de una lista descargada (la del SAT).
    AGENTE_CACHE_DIAS: int = 30
    # Servidores de Overpass (OpenStreetMap), separados por coma: se prueba
    # el siguiente si uno no contesta. Los publicos son gratuitos con uso
    # razonable, pero se saturan (el principal no contesto en 30 s el
    # 2026-11-21).
    OVERPASS_URL: str = (
        "https://overpass-api.de/api/interpreter,"
        "https://overpass.kumi.systems/api/interpreter,"
        "https://overpass.private.coffee/api/interpreter"
    )
    # Lista de contribuyentes NO LOCALIZADOS del SAT (art. 69 CFF), en CSV.
    # Si el SAT la mueve, se cambia aqui; `sondear` dice si se pudo bajar.
    SAT_NO_LOCALIZADOS_URL: str = (
        "http://omawww.sat.gob.mx/cifras_sat/Documents/No%20localizados.csv"
    )
    # Buscador web. SearXNG es el servicio de compose (gratis, sin llave, sin
    # cuota); si hay BRAVE_API_KEY se usa la API de Brave en su lugar, que
    # tiene indice propio y no cae en la zona gris de raspar un motor
    # (crear_agentes.md §11.3).
    SEARXNG_URL: str = "http://searxng:8080"
    BRAVE_API_KEY: str = ""
    # Recoleccion base: lo que se consulta de cada fuente, igual para todos
    # los negocios. Resultados por busqueda web, paginas de redes que se
    # abren y sitios propios del DENUE que se leen.
    AGENTE_MAX_RESULTADOS: int = 8
    AGENTE_MAX_REDES: int = 3
    AGENTE_MAX_SITIOS: int = 2
    # Consultas extra que cada especialista puede pedir DENTRO de su fuente
    # (otro nombre en Maps, otra busqueda). 0 apaga la ampliacion y deja solo
    # la recoleccion base.
    AGENTE_MAX_AMPLIACIONES: int = 2
    # Vueltas que tiene cada rol (especialista, decisor, juez) para producir
    # una salida que pase la validacion; agotadas, su salida es automatica
    # (y lo dice) en vez de inventarse una.
    AGENTE_MAX_INTENTOS: int = 3
    # Modelo del juez. Vacio = el mismo que el equipo (OLLAMA_MODEL). Uno
    # distinto da una auditoria mas independiente, pero dos modelos de ~20 GB
    # no caben juntos en la 5090: Ollama los alternaria en VRAM cada negocio.
    JUEZ_MODEL: str = ""
    # Caracteres de una pagina que se le entregan al modelo. Mas no cabe en
    # el contexto junto con el resto; lo util (estado, fechas, resenas) suele
    # estar al principio.
    AGENTE_MAX_CARACTERES_PAGINA: int = 6000
    # Texto visible minimo para que una pagina cuente como leida. Por debajo
    # es un muro de inicio de sesion (Facebook devuelve ~50 caracteres) o un
    # cascaron de JavaScript, y se le devuelve al modelo como ERROR.
    AGENTE_MIN_CARACTERES_PAGINA: int = 200
    # Timeout de cada peticion HTTP a un buscador o a una pagina.
    AGENTE_TIMEOUT_HTTP_SEGUNDOS: int = 20
    # Segundos maximos que el navegador espera a que el JavaScript de Maps o
    # de Facebook pinte el contenido. Se corta antes si el texto deja de
    # crecer; subirlo solo ayuda si las fichas llegan a medias.
    AGENTE_NAVEGADOR_ESPERA_SEGUNDOS: float = 5.0
    # Muestra: negocios por estrato y semilla de `setseed()` (en [-1, 1]).
    # Con la misma semilla y el mismo gold la tirada es identica.
    MUESTRA_CUOTA_POR_ESTRATO: int = 100
    MUESTRA_SEMILLA: float = 0.42

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

    # -------------------------------------------------------------------------
    # Helper Methods
    # -------------------------------------------------------------------------
    def _get_postgresql_url(self) -> str:
        """
        Construct PostgreSQL URL with proper driver.

        El rol sale de `POSTGRES_USER`, y quien decide cual es el entorno del
        proceso: el servicio `api` de compose recibe `api_ro` (GRANT solo en
        gold y meta, arquitectura.md §2.3.1) y el ETL el rol dueno. Un solo
        camino de conexion, sin ramas por rol en el codigo.

        Returns:
            str: PostgreSQL connection URL
        """
        # `postgresql+psycopg` es psycopg3 (psycopg2 seria `+psycopg2`):
        # lo pide `shared/db/copy.py` para el COPY por bloques.
        driver = "postgresql+psycopg"

        if not self.POSTGRES_USER:
            raise ValueError("POSTGRES_USER must be set for PostgreSQL backend")

        user = self.POSTGRES_USER

        # URL encode password if it contains special characters
        from urllib.parse import quote_plus

        password = quote_plus(self.POSTGRES_PASSWORD) if self.POSTGRES_PASSWORD else ""

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
