"""Tests de las metricas de exito de propagacion (scripts/clee_propagation_stats.py)."""

import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from clee_propagation_stats import (  # noqa: E402
    compute_agreement,
    compute_identity_metrics,
    compute_period_metrics,
    summarize_periods,
)


@pytest.fixture
def conn():
    """
    Warehouse sintetico con los tres regimenes de la propagacion real.

    Dos cortes oficiales (2022, 2021: `clee` presente, resolucion
    deterministica) y dos cortes propagados (2020, 2019: sin `clee`).
    Identidades:
    - C1: vive en los 4 cortes -> cadena larga, vinculacion exitosa.
    - C2: oficial en 2022/2021, no se propago a los viejos.
    - C3: solo 2021 -> cadena de 1 periodo.
    - NEW_R-1: nace en 2020 (no encontro pareja) y se hereda a 2019.
    - NEW_R-2: nace en 2019, cadena de 1 solo periodo.
    - En 2019, C1 aparece DOS veces (colision intra-periodo, fila x9).

    La columna h3 replica la radio salvo por: los prefijos sinteticos
    (NEW_H3-*, nunca literalmente iguales a los de radio) y la fila b3,
    donde h3 asigno C2 y radio C3 (desacuerdo real entre metodos).
    """
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE establecimientos (
            id VARCHAR, period DATE, clee VARCHAR,
            clee_propagado_radio VARCHAR, clee_propagado_h3 VARCHAR
        )
    """)
    con.execute("""
        INSERT INTO establecimientos VALUES
            ('a1', DATE '2022-01-01', 'C1', 'C1', 'C1'),
            ('a2', DATE '2022-01-01', 'C2', 'C2', 'C2'),
            ('b1', DATE '2021-01-01', 'C1', 'C1', 'C1'),
            ('b2', DATE '2021-01-01', 'C2', 'C2', 'C2'),
            ('b3', DATE '2021-01-01', 'C3', 'C3', 'C2'),
            ('c1', DATE '2020-01-01', NULL, 'C1', 'C1'),
            ('c2', DATE '2020-01-01', NULL, 'NEW_R-1', 'NEW_H3-1'),
            ('d1', DATE '2019-01-01', NULL, 'C1', 'C1'),
            ('d2', DATE '2019-01-01', NULL, 'NEW_R-1', 'NEW_H3-1'),
            ('d3', DATE '2019-01-01', NULL, 'NEW_R-2', 'NEW_H3-2'),
            ('x9', DATE '2019-01-01', NULL, 'C1', 'C1')
    """)
    yield con
    con.close()


@pytest.mark.unit
class TestPeriodMetrics:
    """compute_period_metrics(): cobertura corte por corte."""

    def test_conteos_por_periodo(self, conn):
        rows = {
            r["period"]: r for r in compute_period_metrics(conn, "clee_propagado_radio").to_dicts()
        }

        assert rows["2022-01-01"]["total"] == 2
        assert rows["2022-01-01"]["con_clee_oficial"] == 2
        assert rows["2022-01-01"]["vinculadas"] == 2
        assert rows["2022-01-01"]["sinteticas"] == 0

        # Corte propagado: 1 de 2 filas quedo con identidad real.
        assert rows["2020-01-01"]["con_clee_oficial"] == 0
        assert rows["2020-01-01"]["vinculadas"] == 1
        assert rows["2020-01-01"]["sinteticas"] == 1
        assert rows["2020-01-01"]["tasa_vinculacion"] == 0.5

        # 2019: C1 (x2, colision) reales + dos sinteticas.
        assert rows["2019-01-01"]["total"] == 4
        assert rows["2019-01-01"]["vinculadas"] == 2
        assert rows["2019-01-01"]["sinteticas"] == 2

    def test_cobertura_detecta_filas_sin_resolver(self, conn):
        conn.execute("UPDATE establecimientos SET clee_propagado_radio = NULL WHERE id = 'd3'")
        rows = {
            r["period"]: r for r in compute_period_metrics(conn, "clee_propagado_radio").to_dicts()
        }
        assert rows["2019-01-01"]["asignadas"] == 3
        assert rows["2019-01-01"]["tasa_cobertura"] == 0.75


@pytest.mark.unit
class TestSummarizePeriods:
    """summarize_periods(): separa cortes oficiales de propagados."""

    def test_tasa_de_exito_solo_sobre_propagados(self, conn):
        resumen = summarize_periods(compute_period_metrics(conn, "clee_propagado_radio"))

        assert resumen["oficiales"]["periodos"] == 2
        assert resumen["oficiales"]["total"] == 5
        assert resumen["propagados"]["periodos"] == 2
        assert resumen["propagados"]["total"] == 6
        assert resumen["propagados"]["vinculadas"] == 3
        assert resumen["propagados"]["sinteticas"] == 3
        # 3 de 6 filas sin CLEE oficial heredaron un CLEE real de INEGI.
        assert resumen["tasa_vinculacion_propagados"] == 0.5
        assert resumen["tasa_cobertura_global"] == 1.0

    def test_warehouse_vacio(self, conn):
        conn.execute("DELETE FROM establecimientos")
        resumen = summarize_periods(compute_period_metrics(conn, "clee_propagado_radio"))
        assert resumen["total"] == 0
        assert resumen["tasa_vinculacion_propagados"] == 0.0


@pytest.mark.unit
class TestIdentityMetrics:
    """compute_identity_metrics(): nacimientos, cadenas y colisiones."""

    def test_conteo_de_identidades(self, conn):
        ident = compute_identity_metrics(conn, "clee_propagado_radio")["identidades"]
        assert ident["total"] == 5  # C1, C2, C3, NEW_R-1, NEW_R-2
        assert ident["reales"] == 3
        assert ident["sinteticas"] == 2
        assert ident["longitudinales"] == 3  # C1 (4 cortes), C2 (2), NEW_R-1 (2)

    def test_nacimiento_sintetico_es_el_periodo_mas_nuevo(self, conn):
        nac = {
            n["period"]: n["identidades_nacidas"]
            for n in compute_identity_metrics(conn, "clee_propagado_radio")[
                "nacimientos_sinteticos"
            ]
        }
        # NEW_R-1 aparece en 2020 y 2019: nace en 2020 (propagacion futuro->pasado).
        assert nac == {"2020-01-01": 1, "2019-01-01": 1}

    def test_distribucion_de_cadenas(self, conn):
        cadenas = compute_identity_metrics(conn, "clee_propagado_radio")["cadenas"]
        largos = {(c["periodos"], c["sintetica"]): c["identidades"] for c in cadenas}
        assert largos[(4, False)] == 1  # C1
        assert largos[(2, False)] == 1  # C2
        assert largos[(1, False)] == 1  # C3
        assert largos[(2, True)] == 1  # NEW_R-1
        assert largos[(1, True)] == 1  # NEW_R-2

    def test_colision_intra_periodo(self, conn):
        col = compute_identity_metrics(conn, "clee_propagado_radio")["colisiones"]
        # C1 aparece dos veces en 2019-01-01 (filas d1 y x9).
        assert col["identidades_con_colision"] == 1
        assert col["filas_excedentes"] == 1

    def test_colisiones_desglosadas_por_corte(self, conn):
        por_periodo = compute_identity_metrics(conn, "clee_propagado_radio")["colisiones"][
            "por_periodo"
        ]
        assert por_periodo == [
            {
                "period": "2019-01-01",
                "identidades_con_colision": 1,
                "filas_involucradas": 2,
                "filas_excedentes": 1,
            }
        ]

    def test_sin_colisiones_cuando_cada_identidad_es_unica_por_corte(self, conn):
        conn.execute("DELETE FROM establecimientos WHERE id = 'x9'")
        col = compute_identity_metrics(conn, "clee_propagado_radio")["colisiones"]
        assert col["identidades_con_colision"] == 0
        assert col["filas_excedentes"] == 0


@pytest.mark.unit
class TestAgreement:
    """compute_agreement(): radio vs h3 (los NEW_* nunca coinciden literal)."""

    def test_acuerdo_semantico_ignora_uuids_sinteticos(self, conn):
        ac = compute_agreement(conn, "clee_propagado_radio", "clee_propagado_h3")

        assert ac["total"] == 11
        assert ac["identicas"] == 7  # todas menos b3 y las 3 filas sinteticas
        assert ac["ambas_sinteticas"] == 3
        assert ac["real_distinta"] == 1  # b3: C1 vs C2
        assert ac["real_vs_sintetica"] == 0
        assert ac["tasa_acuerdo_semantico"] == pytest.approx(10 / 11)

    def test_real_vs_sintetica_se_cuenta_aparte(self, conn):
        conn.execute("UPDATE establecimientos SET clee_propagado_h3 = 'C2' WHERE id = 'd3'")
        ac = compute_agreement(conn, "clee_propagado_radio", "clee_propagado_h3")
        assert ac["real_vs_sintetica"] == 1
        assert ac["ambas_sinteticas"] == 2
