"""La dimension N3: de observaciones por periodo a una fila por identidad.

`entidades_economicas` colapsa las ~82.9M observaciones en un negocio por
identidad resuelta, con su ventana de vida. Lo que se prueba es el colapso:
que cada identidad quede una vez, que los atributos sean los del periodo mas
reciente y que la ventana cubra todas sus apariciones.
"""

from datetime import date

import polars as pl
import pytest
from sqlalchemy import func, select

from src.etl.loaders.entidades import TABLE_NAME, build_entidades_economicas
from src.shared.db.base import get_table
from src.shared.db.copy import copy_dataframe
from src.shared.db.partitions import create_partition

pytestmark = pytest.mark.postgres

#: Cortes del escenario. El negocio con CLEE aparece en los tres.
CORTES = [date(2019, 11, 1), date(2021, 5, 1), date(2022, 11, 1), date(2016, 1, 1)]

#: Un negocio visto en 3 cortes (con mudanza de CP en el ultimo), uno
#: sintetico visto una vez, y una fila sin resolver que debe quedar fuera.
OBSERVACIONES = [
    ("a1", "14TAC123", date(2019, 11, 1), "TACOS EL GUERO", "44100", date(2010, 7, 1)),
    ("a2", "14TAC123", date(2021, 5, 1), "TACOS EL GUERO", "44100", date(2010, 7, 1)),
    ("a3", "14TAC123", date(2022, 11, 1), "TACOS EL GUERO SA", "44200", date(2009, 1, 1)),
    ("b1", "NEW_14H3-abc", date(2016, 1, 1), "MISCELANEA LUPITA", "45500", date(2015, 3, 1)),
    ("c1", None, date(2016, 1, 1), "SIN RESOLVER", "45500", date(2015, 4, 1)),
]


@pytest.fixture
def conn(silver_engine):
    """Conexion con las observaciones del escenario ya cargadas."""
    with silver_engine.begin() as conn:
        for corte in CORTES:
            create_partition(conn, corte)
        copy_dataframe(
            conn,
            pl.DataFrame(
                OBSERVACIONES,
                schema={
                    "id": pl.Utf8,
                    "clee_propagado_radio": pl.Utf8,
                    "period": pl.Date,
                    "nom_estab": pl.Utf8,
                    "cod_postal": pl.Utf8,
                    "fecha_alta_date": pl.Date,
                },
                orient="row",
            ),
            "establecimientos",
        )
        yield conn


def dimension(conn):
    """La dimension como lista de diccionarios, ordenada por identidad."""
    tabla = get_table(TABLE_NAME)
    filas = conn.execute(select(tabla).order_by(tabla.c.clee_propagado_radio)).mappings()
    return [dict(f) for f in filas]


class TestColapso:
    def test_una_fila_por_identidad(self, conn):
        assert build_entidades_economicas(conn) == 2
        assert [f["clee_propagado_radio"] for f in dimension(conn)] == [
            "14TAC123",
            "NEW_14H3-abc",
        ]

    def test_las_observaciones_sin_resolver_quedan_fuera(self, conn):
        """Una fila sin identidad no es un negocio: no puede ser una fila."""
        build_entidades_economicas(conn)
        assert "SIN RESOLVER" not in [f["nom_estab"] for f in dimension(conn)]

    def test_la_ventana_de_vida_cubre_todas_sus_apariciones(self, conn):
        build_entidades_economicas(conn)
        negocio = dimension(conn)[0]
        assert negocio["primer_periodo"] == date(2019, 11, 1)
        assert negocio["ultimo_periodo"] == date(2022, 11, 1)
        assert negocio["n_periodos"] == 3


class TestAtributos:
    def test_gana_el_periodo_mas_reciente(self, conn):
        """Si un negocio se mudo, la dimension debe tener la direccion nueva."""
        build_entidades_economicas(conn)
        negocio = dimension(conn)[0]
        assert negocio["cod_postal"] == "44200"
        assert negocio["nom_estab"] == "TACOS EL GUERO SA"

    def test_la_fecha_de_alta_es_la_minima_conocida(self, conn):
        """Es el alta mas antigua que INEGI reporto, no la del ultimo corte."""
        build_entidades_economicas(conn)
        assert dimension(conn)[0]["fecha_alta_date"] == date(2009, 1, 1)

    def test_las_identidades_sinteticas_quedan_marcadas(self, conn):
        """Los NEW_* son negocios sin match: el analisis debe distinguirlos."""
        build_entidades_economicas(conn)
        por_id = {f["clee_propagado_radio"]: f for f in dimension(conn)}
        assert por_id["14TAC123"]["es_sintetica"] is False
        assert por_id["NEW_14H3-abc"]["es_sintetica"] is True


class TestRebuild:
    def test_reconstruir_no_duplica(self, conn):
        """Es rebuild total porque los ids NEW_* no son estables."""
        build_entidades_economicas(conn)
        assert build_entidades_economicas(conn) == 2

    def test_sin_identidades_resueltas_la_dimension_queda_vacia(self, conn):
        estab = get_table("establecimientos")
        conn.execute(estab.update().values(clee_propagado_radio=None))
        assert build_entidades_economicas(conn) == 0
        assert conn.execute(
            select(func.count()).select_from(get_table(TABLE_NAME))
        ).scalar_one() == 0
