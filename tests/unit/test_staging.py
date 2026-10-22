"""Cargar un corte fuera de Silver y engancharlo solo si cuadra.

El diagnostico que origina este modulo fue un OOM a media carga que dejo
filas parciales y obligo a reconstruir el warehouse entero. Lo que se prueba
aqui es la propiedad que lo evita: mientras la carga esta en curso, Silver
no ve nada; cuando termina, lo ve todo de golpe. No hay estado intermedio
visible.
"""

from datetime import date

import polars as pl
import pytest
from sqlalchemy import func, select, text

from src.shared.db.base import get_table
from src.shared.db.copy import copy_dataframe
from src.shared.db.partitions import existing_partitions, partition_name
from src.shared.db.staging import StagingValidationError, staging_partition

pytestmark = pytest.mark.postgres

PERIODO = date(2024, 11, 1)
OTRO_PERIODO = date(2025, 3, 1)


@pytest.fixture
def conn(silver_engine):
    """Conexion sobre un `silver` vacio, sin particiones."""
    with silver_engine.begin() as conn:
        yield conn


def filas(n: int, period: date = PERIODO) -> pl.DataFrame:
    """DataFrame minimo de establecimientos de un corte.

    Args:
        n: cuantas filas.
        period: corte al que pertenecen.

    Returns:
        pl.DataFrame: listo para `copy_dataframe`.
    """
    return pl.DataFrame(
        {
            "id": [f"{period.isoformat()}-{i:05d}" for i in range(n)],
            "period": [period] * n,
            "nom_estab": [f"Negocio {i}" for i in range(n)],
        }
    )


def contar(conn) -> int:
    """Filas que ve la tabla padre."""
    return conn.execute(
        select(func.count()).select_from(get_table("establecimientos"))
    ).scalar_one()


class TestCargaExitosa:
    def test_engancha_la_particion_con_sus_filas(self, conn):
        with staging_partition(conn, PERIODO) as target:
            copy_dataframe(conn, filas(100), target.table, target.schema)

        assert existing_partitions(conn) == [partition_name(PERIODO)]
        assert contar(conn) == 100

    def test_la_tabla_de_staging_desaparece_tras_enganchar(self, conn):
        """Se renombra al nombre definitivo, no se queda un duplicado."""
        with staging_partition(conn, PERIODO) as target:
            nombre_staging = target.table
            copy_dataframe(conn, filas(10), target.table, target.schema)

        existe = conn.execute(
            text("SELECT to_regclass(:nombre)"), {"nombre": f"silver.{nombre_staging}"}
        ).scalar_one()
        assert existe is None

    def test_dos_cortes_conviven(self, conn):
        for period in (PERIODO, OTRO_PERIODO):
            with staging_partition(conn, period) as target:
                copy_dataframe(conn, filas(50, period), target.table, target.schema)

        assert sorted(existing_partitions(conn)) == sorted(
            [partition_name(PERIODO), partition_name(OTRO_PERIODO)]
        )
        assert contar(conn) == 100

    def test_silver_no_ve_nada_hasta_el_attach(self, conn):
        """La propiedad central: no hay estado intermedio visible."""
        with staging_partition(conn, PERIODO) as target:
            copy_dataframe(conn, filas(100), target.table, target.schema)
            assert contar(conn) == 0, "Silver vio filas a medio cargar"
        assert contar(conn) == 100


class TestCargaFallida:
    def test_una_excepcion_deja_silver_intacta(self, conn):
        class OOMSimulado(RuntimeError):
            """Lo que de verdad paso: memoria agotada a media carga."""

        with pytest.raises(OOMSimulado):
            with staging_partition(conn, PERIODO) as target:
                copy_dataframe(conn, filas(50), target.table, target.schema)
                raise OOMSimulado

        assert contar(conn) == 0
        assert existing_partitions(conn) == []

    def test_no_deja_tabla_de_staging_huerfana(self, conn):
        with pytest.raises(RuntimeError):
            with staging_partition(conn, PERIODO) as target:
                nombre = target.table
                raise RuntimeError

        existe = conn.execute(
            text("SELECT to_regclass(:nombre)"), {"nombre": f"silver.{nombre}"}
        ).scalar_one()
        assert existe is None

    def test_un_corte_previo_sobrevive_al_fallo_del_siguiente(self, conn):
        """Lo que da sentido a la particion como unidad de recuperacion."""
        with staging_partition(conn, PERIODO) as target:
            copy_dataframe(conn, filas(30), target.table, target.schema)

        with pytest.raises(RuntimeError):
            with staging_partition(conn, OTRO_PERIODO) as target:
                copy_dataframe(conn, filas(30, OTRO_PERIODO), target.table, target.schema)
                raise RuntimeError

        assert contar(conn) == 30
        assert existing_partitions(conn) == [partition_name(PERIODO)]

    def test_reintentar_tras_un_fallo_funciona(self, conn):
        """La tabla de staging del intento anterior no debe estorbar."""
        with pytest.raises(RuntimeError):
            with staging_partition(conn, PERIODO) as target:
                copy_dataframe(conn, filas(10), target.table, target.schema)
                raise RuntimeError

        with staging_partition(conn, PERIODO) as target:
            copy_dataframe(conn, filas(40), target.table, target.schema)

        assert contar(conn) == 40


class TestValidacion:
    """Se rechaza antes de tocar la tabla padre."""

    def test_un_corte_vacio_se_rechaza(self, conn):
        with pytest.raises(StagingValidationError, match="menos que el minimo"):
            with staging_partition(conn, PERIODO):
                pass
        assert existing_partitions(conn) == []

    def test_filas_de_otro_periodo_se_rechazan(self, conn):
        with pytest.raises(StagingValidationError, match="no son del corte"):
            with staging_partition(conn, PERIODO) as target:
                copy_dataframe(conn, filas(5, OTRO_PERIODO), target.table, target.schema)
        assert contar(conn) == 0

    def test_el_error_dice_cuantas_filas_estaban_mal(self, conn):
        """Un mensaje accionable: cuantas, de cuantas, y de que corte."""
        with pytest.raises(StagingValidationError) as exc:
            with staging_partition(conn, PERIODO) as target:
                copy_dataframe(conn, filas(7, OTRO_PERIODO), target.table, target.schema)
        assert "7 de 7" in str(exc.value)
        assert PERIODO.isoformat() in str(exc.value)

    def test_min_rows_personalizado(self, conn):
        with pytest.raises(StagingValidationError, match="menos que el minimo 100"):
            with staging_partition(conn, PERIODO, min_rows=100) as target:
                copy_dataframe(conn, filas(99), target.table, target.schema)


class TestEstructura:
    """La particion enganchada es indistinguible de una creada a mano."""

    def test_hereda_los_indices_del_padre(self, conn):
        """El ATTACH los construye: por eso se carga sin ellos y es rapido."""
        from src.etl.db.indexes import expected_indexes

        with staging_partition(conn, PERIODO) as target:
            copy_dataframe(conn, filas(10), target.table, target.schema)

        indices_hijos = conn.execute(
            text(
                "SELECT COUNT(*) FROM pg_indexes WHERE schemaname = 'silver' AND tablename = :tabla"
            ),
            {"tabla": partition_name(PERIODO)},
        ).scalar_one()
        # Los declarados mas el que respalda la PK.
        assert indices_hijos == len(expected_indexes()) + 1

    def test_no_queda_el_check_redundante_del_attach(self, conn):
        """Se agrega para acortar el bloqueo y se retira: ya lo dice el limite."""
        with staging_partition(conn, PERIODO) as target:
            copy_dataframe(conn, filas(10), target.table, target.schema)

        checks = conn.execute(
            text(
                "SELECT COUNT(*) FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE t.relname = :tabla AND c.contype = 'c'"
            ),
            {"tabla": partition_name(PERIODO)},
        ).scalar_one()
        assert checks == 0
