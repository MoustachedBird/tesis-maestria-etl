"""`COPY ... FROM STDIN` desde DataFrames de Polars.

Es el camino por el que entran las 82.9M filas de Silver, asi que lo que se
prueba aqui es sobre todo que los valores lleguen tal cual salieron: lo
barato de romper en una carga masiva no es el conteo --eso se nota-- sino
un NULL que se vuelve cadena vacia en 40 millones de filas.
"""

from datetime import date

import polars as pl
import psycopg
import pytest
from sqlalchemy import func, select, text

from src.shared.db.base import get_table
from src.shared.db.copy import copy_dataframe
from src.shared.db.partitions import create_partition

pytestmark = pytest.mark.postgres

#: Corte de las filas de prueba.
PERIODO = date(2024, 11, 1)


@pytest.fixture
def conn(silver_engine):
    """Conexion con la particion del corte de prueba ya creada."""
    with silver_engine.begin() as conn:
        create_partition(conn, PERIODO)
        yield conn


def filas(n: int, **overrides) -> pl.DataFrame:
    """DataFrame minimo de establecimientos.

    Args:
        n: cuantas filas.
        **overrides: columnas que reemplazan a las generadas.

    Returns:
        pl.DataFrame: listo para `copy_dataframe`.
    """
    datos = {
        "id": [f"id-{i:05d}" for i in range(n)],
        "period": [PERIODO] * n,
        "nom_estab": [f"Negocio {i}" for i in range(n)],
    }
    datos.update(overrides)
    return pl.DataFrame(datos)


def contar(conn) -> int:
    """Filas en la tabla de hechos."""
    return conn.execute(
        select(func.count()).select_from(get_table("establecimientos"))
    ).scalar_one()


class TestValoresQueSobreviven:
    """Lo que entra es lo que sale."""

    def test_distingue_null_de_cadena_vacia(self, conn):
        """La hipotesis central del formato elegido.

        Polars escribe NULL y "" igual por default, lo que los volveria
        indistinguibles al llegar. `copy.py` usa `quote_style="non_numeric"`
        justamente para que `""` salga entrecomillado y el NULL no. Los
        datos del DENUE son de encuesta y vienen llenos de campos vacios,
        asi que confundirlos no es un detalle.
        """
        df = filas(3, nom_estab=["Hola", "", None])
        copy_dataframe(conn, df, "establecimientos")

        estab = get_table("establecimientos")
        nulos = conn.execute(
            select(func.count()).select_from(estab).where(estab.c.nom_estab.is_(None))
        ).scalar_one()
        vacios = conn.execute(
            select(func.count()).select_from(estab).where(estab.c.nom_estab == "")
        ).scalar_one()
        assert (nulos, vacios) == (1, 1)

    def test_preserva_texto_con_comas_comillas_y_saltos(self, conn):
        """El CSV se arma en el cliente: si el escapado falla, se corre todo."""
        raros = ["con,coma", 'con"comilla', "con\nsalto", "con\ttab", "acentós ñ"]
        copy_dataframe(conn, filas(len(raros), nom_estab=raros), "establecimientos")

        estab = get_table("establecimientos")
        guardados = conn.execute(select(estab.c.nom_estab).order_by(estab.c.id)).scalars().all()
        assert guardados == raros

    def test_preserva_precision_de_coordenadas(self, conn):
        """`Double` y no `Float`: en float32 una coordenada se corre ~4 cm."""
        lat, lon = 19.432608312345678, -99.133208712345678
        df = filas(1).with_columns(latitud=pl.lit(lat), longitud=pl.lit(lon))
        copy_dataframe(conn, df, "establecimientos")

        estab = get_table("establecimientos")
        fila = conn.execute(select(estab.c.latitud, estab.c.longitud)).one()
        assert fila == (lat, lon)


class TestBloques:
    """El troceado es la cota de RAM; no debe alterar el resultado."""

    def test_un_chunk_mas_chico_que_los_datos_carga_todo(self, conn):
        copy_dataframe(conn, filas(250), "establecimientos", chunk_rows=100)
        assert contar(conn) == 250

    def test_el_tamano_de_bloque_no_cambia_lo_cargado(self, conn):
        """Mismos datos con otro troceado deben dar el mismo resultado."""
        copy_dataframe(conn, filas(100), "establecimientos", chunk_rows=7)
        estab = get_table("establecimientos")
        ids = conn.execute(select(estab.c.id).order_by(estab.c.id)).scalars().all()
        assert ids == sorted(f"id-{i:05d}" for i in range(100))

    def test_un_bloque_que_divide_exacto_no_manda_uno_vacio(self, conn):
        """100 filas en bloques de 50 son dos bloques, no tres."""
        escritas = copy_dataframe(conn, filas(100), "establecimientos", chunk_rows=50)
        assert escritas == 100
        assert contar(conn) == 100

    def test_chunk_no_positivo_es_error(self, conn):
        with pytest.raises(ValueError, match="positivo"):
            copy_dataframe(conn, filas(5), "establecimientos", chunk_rows=0)


class TestContrato:
    """Que falle temprano y claro en vez de desalinear los datos."""

    def test_dataframe_vacio_no_emite_copy(self, conn):
        assert copy_dataframe(conn, filas(0), "establecimientos") == 0
        assert contar(conn) == 0

    def test_columna_inexistente_en_el_df_es_error(self, conn):
        with pytest.raises(ValueError, match="columnas ausentes"):
            copy_dataframe(conn, filas(2), "establecimientos", columns=["id", "no_existe"])

    def test_columna_que_la_tabla_no_tiene_falla_en_el_servidor(self, conn):
        """Mejor un error del servidor que escribir en la columna equivocada."""
        df = filas(2).with_columns(columna_inventada=pl.lit("x"))
        with pytest.raises(psycopg.Error):
            copy_dataframe(conn, df, "establecimientos")

    def test_subconjunto_de_columnas_deja_el_resto_en_null(self, conn):
        copy_dataframe(conn, filas(2), "establecimientos", columns=["id", "period"])
        estab = get_table("establecimientos")
        assert conn.execute(select(estab.c.nom_estab)).scalars().all() == [None, None]

    def test_respeta_el_orden_de_columnas_pedido(self, conn):
        """El orden lo manda `columns`, no el del DataFrame."""
        df = filas(1, nom_estab=["Primero"])
        copy_dataframe(conn, df, "establecimientos", columns=["nom_estab", "period", "id"])
        estab = get_table("establecimientos")
        assert conn.execute(select(estab.c.nom_estab)).scalar_one() == "Primero"


class TestIntegridad:
    """El COPY no esquiva las restricciones de la tabla."""

    def test_fila_de_otro_periodo_no_tiene_particion(self, conn):
        """Sin particion para ese corte, PostgreSQL la rechaza."""
        df = filas(1, period=[date(2030, 7, 1)])
        with pytest.raises(psycopg.Error, match="no partition"):
            copy_dataframe(conn, df, "establecimientos")

    def test_fk_invalida_se_rechaza(self, conn):
        df = filas(1).with_columns(codigo_act=pl.lit("999999"))
        with pytest.raises(psycopg.Error):
            copy_dataframe(conn, df, "establecimientos")

    def test_pk_duplicada_se_rechaza(self, conn):
        copy_dataframe(conn, filas(1), "establecimientos")
        with pytest.raises(psycopg.Error):
            copy_dataframe(conn, filas(1), "establecimientos")


class TestEscala:
    """Un volumen que ejercite varios bloques de verdad."""

    def test_cincuenta_mil_filas_en_bloques(self, conn):
        n = 50_000
        escritas = copy_dataframe(conn, filas(n), "establecimientos", chunk_rows=8_192)
        assert escritas == n
        assert contar(conn) == n

        # El COPY va a la tabla padre y PostgreSQL enruta por la clave de
        # particion: todo debe haber aterrizado en la particion del corte.
        en_particion = conn.execute(
            text("SELECT COUNT(*) FROM silver.establecimientos_2024_11")
        ).scalar_one()
        assert en_particion == n
