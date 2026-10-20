"""Cada loader siembra su tabla desde el parquet, via el camino central.

La logica de carga/validacion vive en `loaders/seeds.py` (probada en
test_seeds.py); aqui se verifica que cada loader apunta a la tabla y el
archivo correctos y que re-sembrar deja el catalogo igual.
"""

import pytest
from sqlalchemy import func, select

from src.etl.loaders import dominios, estratos, geography, scian
from src.shared.db.base import Base, get_table
from src.shared.db.models import Establecimiento  # noqa: F401  (puebla el metadata)

pytestmark = pytest.mark.postgres


@pytest.fixture()
def connection(silver_engine, request):
    """Conexion con las tablas del marcador `tables` ya creadas.

    El esquema sale del modelo, igual que en la migracion, asi que estos
    tests fallan si el seed deja de encajar con la tabla que lo recibe.
    """
    table_names = request.node.get_closest_marker("tables").args
    Base.metadata.create_all(silver_engine, tables=[get_table(n) for n in table_names])
    with silver_engine.begin() as conn:
        yield conn


def _count(conn, table_name: str) -> int:
    """Filas en una tabla del catalogo.

    Args:
        conn: conexion SQLAlchemy.
        table_name: tabla a contar.

    Returns:
        int: numero de filas.
    """
    return conn.execute(select(func.count()).select_from(get_table(table_name))).scalar_one()


@pytest.mark.unit
class TestSiembraCadaCatalogo:
    """Cada loader puebla su tabla desde el parquet del repo."""

    @pytest.mark.tables("denue_entidades", "denue_municipios", "denue_localidades")
    def test_geografia(self, connection):
        stats = geography.load_seeds(connection)
        assert stats["denue_entidades"] == 32
        assert _count(connection, "denue_entidades") == 32

    @pytest.mark.tables("denue_actividades_economicas")
    def test_scian_deriva_jerarquia(self, connection):
        scian.load_seeds(connection)
        actividades = get_table("denue_actividades_economicas")
        row = connection.execute(
            select(
                actividades.c.sector, actividades.c.subsector, actividades.c.rama
            ).where(actividades.c.codigo_act == "461110")
        ).one()
        assert tuple(row) == ("46", "461", "4611")

    @pytest.mark.tables(
        "denue_tipos_vialidad",
        "denue_tipos_asentamiento",
        "denue_tipos_unidad_economica",
    )
    def test_dominios(self, connection):
        stats = dominios.load_seeds(connection)
        assert stats["denue_tipos_asentamiento"] == 45

    @pytest.mark.tables("denue_estratos_personal")
    def test_estratos(self, connection):
        stats = estratos.load_seeds(connection)
        assert stats["denue_estratos_personal"] == 8


@pytest.mark.unit
class TestReSembrar:
    """Volver a sembrar deja el catalogo exactamente igual."""

    @pytest.mark.tables("denue_entidades", "denue_municipios", "denue_localidades")
    def test_reseed_no_duplica(self, connection):
        geography.load_seeds(connection)
        geography.load_seeds(connection)
        assert _count(connection, "denue_entidades") == 32

    @pytest.mark.tables("denue_entidades", "denue_municipios", "denue_localidades")
    def test_reseed_no_reescribe_si_ya_coincide(self, connection):
        """La segunda siembra no toca la tabla: compara antes de escribir.

        Importa porque los catalogos estan referenciados por FKs de las
        tablas de hechos; un DELETE + INSERT gratuito en cada migracion
        seria un riesgo innecesario sobre filas referenciadas.
        """
        geography.load_seeds(connection)
        entidades = get_table("denue_entidades")
        antes = connection.execute(select(entidades).order_by(entidades.c.cve_ent)).all()

        geography.load_seeds(connection)
        despues = connection.execute(select(entidades).order_by(entidades.c.cve_ent)).all()
        assert antes == despues
