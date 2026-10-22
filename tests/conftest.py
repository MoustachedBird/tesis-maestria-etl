"""Fixtures compartidos de la suite.

Dos clases de prueba: las unitarias arman sus datos a mano y no tocan base
alguna, y las marcadas `postgres` corren contra el servidor de pruebas que
describe `TEST_POSTGRES_URL` --y se saltan solas si no responde.
"""

import functools
import os
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.pool import NullPool

from src.shared.db.models import (
    ActividadEconomica,
    Entidad,
    Establecimiento,
    Localidad,
    Municipio,
)
from src.shared.db.models import (
    TipoAsentamiento as TipoAsentamientoCat,
)
from src.shared.db.models import (
    TipoUnidadEconomica as TipoUnidadEconomicaCat,
)
from src.shared.db.models import (
    TipoVialidad as TipoVialidadCat,
)

# ============================================================================
# CONFIGURATION
# ============================================================================


def _catalog_id(table: str, label_col: str, label: str) -> int:
    """Id de un valor de catalogo leido del seed parquet (fuente de verdad).

    Los tests no pueden hardcodear ids ni derivarlos de un enum: el dominio
    vive en `data/seeds/*.parquet` y de ahi salen los ids que referencian
    los FKs.
    """
    import polars as pl

    from src.etl.loaders.dominios import SEED_FILES
    from src.shared.config import get_settings

    seeds = Path(get_settings().SEEDS_DIR)
    fname = SEED_FILES.get(table, f"seed_{table.removeprefix('denue_')}.parquet")
    df = pl.read_parquet(seeds / fname)
    return int(df.filter(pl.col(label_col) == label)["id"][0])


# ============================================================================
# MANUAL FIXTURES (UNIT TESTS - FAST, ISOLATED)
# ============================================================================


@pytest.fixture
def sample_establecimiento() -> Establecimiento:
    """
    Manually crafted Establecimiento for unit tests.

    Fast, isolated fixture with controlled test data.
    Use for testing helper methods without database dependency.

    Returns:
        Establecimiento instance with manually set fields
    """
    estab = Establecimiento(
        id="test_123456789",
        clee="0512345678901",
        nom_estab="Test Restaurant & Bar",
        raz_social="TEST BUSINESS SA DE CV",
        codigo_act="722515",
        # Business type (FKs por id; el label vive en el catalogo)
        tipounieco_id=_catalog_id("denue_tipos_unidad_economica", "tipounieco", "FIJO"),
        per_ocu_id=_catalog_id("denue_estratos_personal", "per_ocu", "11 A 30 PERSONAS"),
        # Address components
        tipo_vial_id=_catalog_id("denue_tipos_vialidad", "tipo_vial", "CALLE"),
        nom_vial="INSURGENTES SUR",
        numero_ext="1234",
        letra_ext="A",
        numero_int="10",
        letra_int="B",
        edificio="TORRE EMPRESARIAL",
        # Cross streets
        tipo_v_e_1="AVENIDA",
        nom_v_e_1="REFORMA",
        tipo_v_e_2="CALLE",
        nom_v_e_2="CHAPULTEPEC",
        tipo_v_e_3="BOULEVARD",
        nom_v_e_3="CONSTITUYENTES",
        # Settlement
        tipo_asent_id=_catalog_id("denue_tipos_asentamiento", "tipo_asent", "COLONIA"),
        nomb_asent="DEL VALLE",
        # Shopping center
        nom_cencom="PLAZA INSURGENTES",
        num_local="L-42",
        # Postal & geographic
        cod_postal="03100",
        cve_ent="09",
        cve_mun="015",
        cve_loc="0001",
        ageb="0123",
        manzana="045",
        # Coordinates (Mexico City - valid)
        latitud=19.4326,
        longitud=-99.1332,
        # Contact
        telefono="5555551234",
        correoelec="test@example.com",
        www="https://www.testbusiness.com",
        # Temporal
        fecha_alta_str="2020-06",
        fecha_alta_date=date(2020, 6, 1),
        period=date(2023, 11, 1),
    )
    # Objeto transient (sin sesion): las relationships no cargan solas, se
    # asignan los objetos de catalogo para que los helpers de direccion
    # resuelvan los labels igual que con una fila persistida + JOIN.
    estab.tipo_vialidad = TipoVialidadCat(
        id=_catalog_id("denue_tipos_vialidad", "tipo_vial", "CALLE"),
        tipo_vial="CALLE",
        descripcion="test",
    )
    estab.tipo_asentamiento = TipoAsentamientoCat(
        id=_catalog_id("denue_tipos_asentamiento", "tipo_asent", "COLONIA"),
        tipo_asent="COLONIA",
        descripcion="test",
    )
    estab.tipo_unidad = TipoUnidadEconomicaCat(
        id=_catalog_id("denue_tipos_unidad_economica", "tipounieco", "FIJO"),
        tipounieco="FIJO",
        descripcion="test",
    )
    return estab


@pytest.fixture
def sample_establecimiento_minimal() -> Establecimiento:
    """
    Minimal Establecimiento with only required fields.

    Use for testing edge cases and validation logic.

    Returns:
        Establecimiento with minimal data
    """
    return Establecimiento(
        id="test_minimal_001",
        nom_estab="Minimal Business",
        codigo_act="722515",
        per_ocu_id=_catalog_id("denue_estratos_personal", "per_ocu", "0 A 5 PERSONAS"),
        cve_ent="09",
    )


@pytest.fixture
def sample_establecimiento_invalid_coords() -> Establecimiento:
    """
    Establecimiento with invalid coordinates (outside Mexico).

    Use for testing coordinate validation.

    Returns:
        Establecimiento with invalid coordinates
    """
    estab = Establecimiento(
        id="test_invalid_coords",
        nom_estab="Invalid Location Business",
        codigo_act="722515",
        latitud=51.5074,  # London, UK
        longitud=-0.1278,
        cve_ent="09",
    )
    return estab


@pytest.fixture
def sample_entidad() -> Entidad:
    """
    Manually crafted Entidad for unit tests.

    Returns:
        Entidad instance (Mexico City)
    """
    return Entidad(cve_ent="09", entidad="CIUDAD DE MÉXICO")


@pytest.fixture
def sample_municipio() -> Municipio:
    """
    Manually crafted Municipio for unit tests.

    Returns:
        Municipio instance (Benito Juárez, CDMX)
    """
    return Municipio(cve_ent="09", cve_mun="015", municipio="BENITO JUÁREZ")


@pytest.fixture
def sample_localidad() -> Localidad:
    """
    Manually crafted Localidad for unit tests.

    Returns:
        Localidad instance
    """
    return Localidad(cve_ent="09", cve_mun="015", cve_loc="0001", localidad="CIUDAD DE MÉXICO")


@pytest.fixture
def sample_actividad_economica() -> ActividadEconomica:
    """
    Manually crafted ActividadEconomica for unit tests.

    Returns:
        ActividadEconomica instance (Restaurants)
    """
    return ActividadEconomica(
        codigo_act="722515",
        nombre_act="Restaurantes con servicio de preparación de alimentos a la carta o de comida corrida",
        sector="72",
        subsector="722",
        rama="7225",
    )


# ============================================================================
# HELPER FIXTURES
# ============================================================================


# ============================================================================
# SERVIDOR POSTGRES DE PRUEBA (migracion Fase 3)
# ============================================================================
# Los tests que tocan el esquema real van marcados `@pytest.mark.postgres` y
# se saltan solos cuando no hay servidor, de modo que la suite corre completa
# en una maquina sin nada levantado y completa de verdad cuando lo hay.
#
#     docker compose up -d postgres
#     docker compose run --rm tools pytest -m postgres

#: Variable de entorno que apunta al Postgres desechable de los tests. Si no
#: esta definida se deriva de la config del proyecto con el sufijo `_test`,
#: para no escribir jamas sobre la base real por accidente.
TEST_POSTGRES_URL_ENV = "TEST_POSTGRES_URL"

#: Sufijo que separa la base de pruebas de la de produccion.
TEST_DB_SUFFIX = "_test"

#: Segundos que se espera al servidor antes de declararlo ausente. Corto a
#: proposito: es una sonda de disponibilidad, no una conexion de trabajo.
TEST_POSTGRES_CONNECT_TIMEOUT = 2


def get_test_postgres_url() -> str:
    """URL del Postgres de pruebas.

    Prioriza `TEST_POSTGRES_URL`; si no esta, reusa la configuracion del
    proyecto pero cambiando el nombre de la base por `<POSTGRES_DB>_test`.
    Nunca devuelve la base de produccion.

    Returns:
        str: URL de SQLAlchemy con driver psycopg3.
    """
    override = os.getenv(TEST_POSTGRES_URL_ENV)
    if override:
        return override

    from src.shared.config import get_settings

    settings = get_settings()
    return settings._get_postgresql_url().replace(
        f"/{settings.POSTGRES_DB}", f"/{settings.POSTGRES_DB}{TEST_DB_SUFFIX}"
    )


def _primera_linea(exc: Exception) -> str:
    """Primera linea del error, acotada.

    Solo el tipo y la primera linea: el detalle de psycopg trae el DSN
    completo y lo ultimo que se quiere es un password en el log de pytest.

    Args:
        exc: excepcion a resumir.

    Returns:
        str: mensaje de una linea, maximo 120 caracteres.
    """
    texto = str(exc).strip()
    return texto.splitlines()[0][:120] if texto else ""


def _maintenance_url(test_url: str) -> str:
    """La misma conexion pero contra la base `postgres`.

    `CREATE DATABASE` no se puede ejecutar desde la base que se va a crear,
    asi que la sonda necesita una base que siempre existe en un servidor
    vivo.

    Args:
        test_url: URL de la base de pruebas.

    Returns:
        str: URL con el nombre de base reemplazado por `postgres`.
    """
    base, _, _ = test_url.rpartition("/")
    return f"{base}/postgres"


def _crear_base_de_pruebas(test_url: str) -> str | None:
    """Crea la base de pruebas si el servidor responde y ella no existe.

    Existe para que una maquina con servidor no salte 157 tests por una
    base que falta: eso haria que una suite "verde" no probara nada de lo
    que toca disco, y el motivo del skip se confundiria con "no hay
    servidor".

    `CREATE DATABASE` no admite transaccion, de ahi el AUTOCOMMIT. El
    nombre se interpola porque PostgreSQL no acepta bind params en DDL; su
    valor sale de la config del proyecto con sufijo fijo, no de entrada
    externa.

    Args:
        test_url: URL de la base de pruebas.

    Returns:
        str | None: motivo por el que no se pudo, o None si ya existe o
        quedo creada.
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import SQLAlchemyError

    nombre = test_url.rpartition("/")[2].split("?")[0]
    engine = create_engine(
        _maintenance_url(test_url),
        connect_args={"connect_timeout": TEST_POSTGRES_CONNECT_TIMEOUT},
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )
    try:
        with engine.connect() as conn:
            # Conectar aqui ya prueba que el servidor esta: lo que falle de
            # esta linea en adelante es un problema de la base, no del
            # servidor, y los dos merecen mensajes distintos.
            existe = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :nombre"), {"nombre": nombre}
            ).scalar()
            if existe:
                return None
            try:
                conn.execute(text(f'CREATE DATABASE "{nombre}"'))
            except SQLAlchemyError as exc:
                return (
                    f"falta la base de pruebas '{nombre}' y el rol no pudo crearla "
                    f"({_primera_linea(exc)}). Crearla a mano con: "
                    f"docker compose exec -T postgres psql -U postgres "
                    f"-c 'CREATE DATABASE \"{nombre}\" OWNER etl_rw;'"
                )
    except (SQLAlchemyError, OSError) as exc:
        # No se llego ni a la base `postgres`: el servidor no esta.
        return f"{type(exc).__name__}: {_primera_linea(exc)}"
    finally:
        engine.dispose()
    return None


@functools.lru_cache(maxsize=1)
def postgres_unavailable_reason() -> str | None:
    """Sondea el Postgres de pruebas una sola vez por sesion.

    Si el servidor responde pero la base de pruebas no existe, la crea: una
    base ausente es un problema de arranque con arreglo obvio, no una razon
    para saltarse los tests en silencio. Solo se salta cuando el servidor
    mismo no esta.

    Returns:
        str | None: motivo por el que no se puede usar, o None si responde.
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import SQLAlchemyError

    test_url = get_test_postgres_url()

    faltante = _crear_base_de_pruebas(test_url)
    if faltante is not None:
        return faltante

    try:
        engine = create_engine(
            test_url,
            connect_args={"connect_timeout": TEST_POSTGRES_CONNECT_TIMEOUT},
            poolclass=NullPool,
        )
    except Exception as exc:  # URL mal formada, driver ausente
        return f"{type(exc).__name__}: {str(exc).strip()[:120]}"

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError) as exc:
        return f"{type(exc).__name__}: {_primera_linea(exc)}"
    finally:
        engine.dispose()
    return None


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Salta los tests marcados `postgres` cuando no hay servidor.

    Args:
        item: test que pytest esta por ejecutar.
    """
    if item.get_closest_marker("postgres") is None:
        return
    reason = postgres_unavailable_reason()
    if reason is not None:
        pytest.skip(f"Postgres de pruebas no disponible ({reason})")


@pytest.fixture(scope="session")
def pg_engine():
    """Engine contra el Postgres de pruebas, con los 3 schemas creados.

    Scope de sesion: levantar el esquema es caro y los tests que lo usan se
    encargan de limpiar sus propias tablas.

    Yields:
        Engine: conexion al Postgres de pruebas.
    """
    from sqlalchemy import create_engine, text

    reason = postgres_unavailable_reason()
    if reason is not None:
        pytest.skip(f"Postgres de pruebas no disponible ({reason})")

    engine = create_engine(get_test_postgres_url(), poolclass=NullPool)
    with engine.begin() as conn:
        for schema in ("silver", "gold", "meta"):
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    yield engine
    engine.dispose()


@pytest.fixture
def tracker(pg_engine):
    """`MetadataTracker` sobre un schema `meta` recien creado y vacio.

    Recrea las tablas en cada test en vez de limpiarlas: es barato (son 6
    tablas sin datos) y deja cada prueba partiendo del mismo estado sin
    depender del orden de ejecucion.

    Args:
        pg_engine: engine del Postgres de pruebas.

    Returns:
        MetadataTracker: apuntando a la base de pruebas.
    """
    from sqlalchemy.orm import sessionmaker

    from src.meta.tracker import MetadataTracker
    from src.shared.db.base import Base, tables_for

    tablas = tables_for("meta")
    Base.metadata.drop_all(pg_engine, tables=tablas)
    Base.metadata.create_all(pg_engine, tables=tablas)
    return MetadataTracker(session_factory=sessionmaker(bind=pg_engine))


@pytest.fixture
def silver_engine(pg_engine):
    """Engine con el schema `silver` recien creado desde el modelo.

    El mismo camino que la migracion (`create_all` sobre los `Table` de la
    capa), de modo que lo que se prueba aqui es el esquema real y no una
    copia escrita a mano.

    Args:
        pg_engine: engine del Postgres de pruebas.

    Yields:
        Engine: con `silver` poblado de tablas vacias.
    """
    from sqlalchemy import text

    from src.shared.db.base import Base, tables_for

    # DROP SCHEMA CASCADE y no `drop_all`: una particion desenganchada
    # queda como tabla suelta que conserva sus FKs a los catalogos, y
    # entonces `drop_all` no puede con ellos ("cannot drop table ...
    # because other objects depend on it"). CASCADE no distingue.
    with pg_engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS silver CASCADE"))
        conn.execute(text("CREATE SCHEMA silver"))
    Base.metadata.create_all(pg_engine, tables=tables_for("silver"))
    yield pg_engine
