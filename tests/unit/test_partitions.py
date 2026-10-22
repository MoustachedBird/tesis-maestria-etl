"""Ciclo de vida de las particiones de `silver.establecimientos`.

Particionar por `period` convierte al corte en la unidad de recuperacion:
uno que se cargo mal se desengancha y se rehace sin tocar los otros. Eso es
lo que estos tests verifican que sea cierto.
"""

from datetime import date

import polars as pl
import psycopg
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from src.shared.db.base import get_table
from src.shared.db.copy import copy_dataframe
from src.shared.db.partitions import (
    create_partition,
    detach_partition,
    drop_partition,
    existing_partitions,
    partition_name,
    qualified_partition,
)

pytestmark = pytest.mark.postgres

PERIODO = date(2024, 11, 1)
OTRO_PERIODO = date(2025, 3, 1)


@pytest.fixture
def conn(silver_engine):
    """Conexion sobre un `silver` vacio."""
    with silver_engine.begin() as conn:
        yield conn


def poblar(conn, period: date, n: int) -> None:
    """Crea la particion de un corte y le mete n filas."""
    create_partition(conn, period)
    copy_dataframe(
        conn,
        pl.DataFrame(
            {
                "id": [f"{period.isoformat()}-{i}" for i in range(n)],
                "period": [period] * n,
            }
        ),
        "establecimientos",
    )


def contar(conn) -> int:
    """Filas que ve la tabla padre."""
    return conn.execute(
        select(func.count()).select_from(get_table("establecimientos"))
    ).scalar_one()


class TestCrear:
    def test_una_particion_nueva_aparece_enganchada(self, conn):
        create_partition(conn, PERIODO)
        assert existing_partitions(conn) == [partition_name(PERIODO)]

    def test_crear_dos_veces_no_falla(self, conn):
        create_partition(conn, PERIODO)
        create_partition(conn, PERIODO)
        assert existing_partitions(conn) == [partition_name(PERIODO)]

    def test_con_if_not_exists_false_la_segunda_falla(self, conn):
        # DDL envuelto por SQLAlchemy; solo el COPY escapa a psycopg.
        create_partition(conn, PERIODO)
        with pytest.raises(DBAPIError):
            create_partition(conn, PERIODO, if_not_exists=False)

    def test_sin_particion_no_se_puede_insertar(self, conn):
        """El motivo de que `create_partition` exista."""
        df = pl.DataFrame({"id": ["x"], "period": [PERIODO]})
        with pytest.raises(psycopg.Error, match="no partition"):
            copy_dataframe(conn, df, "establecimientos")

    def test_cada_corte_recibe_solo_sus_filas(self, conn):
        poblar(conn, PERIODO, 10)
        poblar(conn, OTRO_PERIODO, 5)

        en_primera = conn.execute(
            text(f"SELECT COUNT(*) FROM {qualified_partition(PERIODO)}")
        ).scalar_one()
        en_segunda = conn.execute(
            text(f"SELECT COUNT(*) FROM {qualified_partition(OTRO_PERIODO)}")
        ).scalar_one()
        assert (en_primera, en_segunda) == (10, 5)


class TestDesengancharYEliminar:
    def test_detach_saca_las_filas_del_padre(self, conn):
        poblar(conn, PERIODO, 10)
        poblar(conn, OTRO_PERIODO, 5)

        detach_partition(conn, PERIODO)
        assert contar(conn) == 5
        assert existing_partitions(conn) == [partition_name(OTRO_PERIODO)]

    def test_detach_conserva_los_datos_en_una_tabla_suelta(self, conn):
        """Es lo que permite inspeccionar o respaldar antes de recargar."""
        poblar(conn, PERIODO, 10)
        detach_partition(conn, PERIODO)

        sueltas = conn.execute(
            text(f"SELECT COUNT(*) FROM {qualified_partition(PERIODO)}")
        ).scalar_one()
        assert sueltas == 10

    def test_drop_se_lleva_la_particion_y_sus_filas(self, conn):
        poblar(conn, PERIODO, 10)
        drop_partition(conn, PERIODO)

        assert existing_partitions(conn) == []
        assert contar(conn) == 0

    def test_drop_de_algo_inexistente_no_falla(self, conn):
        drop_partition(conn, PERIODO)

    def test_con_if_exists_false_si_falla(self, conn):
        with pytest.raises(DBAPIError):
            drop_partition(conn, PERIODO, if_exists=False)

    def test_recargar_un_corte_no_toca_a_los_demas(self, conn):
        """El ciclo completo de recuperacion de un solo corte."""
        poblar(conn, PERIODO, 10)
        poblar(conn, OTRO_PERIODO, 5)

        detach_partition(conn, PERIODO)
        drop_partition(conn, PERIODO)
        poblar(conn, PERIODO, 99)

        assert contar(conn) == 104
        assert sorted(existing_partitions(conn)) == sorted(
            [partition_name(PERIODO), partition_name(OTRO_PERIODO)]
        )


class TestPruning:
    def test_una_consulta_por_corte_toca_una_sola_particion(self, conn):
        """Si esto deja de cumplirse, cada consulta barre los 20 cortes."""
        import re

        poblar(conn, PERIODO, 10)
        poblar(conn, OTRO_PERIODO, 10)

        plan = "\n".join(
            conn.execute(
                text(
                    "EXPLAIN SELECT COUNT(*) FROM silver.establecimientos "
                    f"WHERE period = DATE '{PERIODO.isoformat()}'"
                )
            ).scalars()
        )
        # Se extraen los nombres con un patron exacto: los de indice tambien
        # contienen "establecimientos_" y darian falsos positivos.
        tocadas = set(re.findall(r"\bestablecimientos_\d{4}_\d{2}\b", plan))
        assert tocadas == {partition_name(PERIODO)}

    def test_listar_particiones_devuelve_orden_cronologico(self, conn):
        create_partition(conn, OTRO_PERIODO)
        create_partition(conn, PERIODO)
        assert existing_partitions(conn) == [
            partition_name(PERIODO),
            partition_name(OTRO_PERIODO),
        ]
