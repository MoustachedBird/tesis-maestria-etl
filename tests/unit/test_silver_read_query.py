"""La consulta de lectura de Silver que usa la resolución CLEE.

Toda tabla va calificada con su schema: la conexión del ETL no fija
`search_path`, así que un nombre desnudo levanta `relation does not exist` a
mitad de la corrida — después de que la fase determinista ya escribió.
"""

import re

import pytest

from src.etl.utils.clee_resolution_config import (
    ORIGEN_COLUMN,
    RESOLVED_COLUMN,
    silver_read_query,
)

#: Captura el nombre que sigue a FROM o JOIN.
FUENTES = re.compile(r"(?:FROM|JOIN)\s+([^\s]+)")


@pytest.mark.parametrize("clee_table", ["clee_radio", "clee_h3", None], ids=str)
def test_toda_tabla_va_calificada(clee_table):
    """Sin esto la consulta falla en cuanto se usa de verdad."""
    desnudas = [t for t in FUENTES.findall(silver_read_query(clee_table)) if "." not in t]
    assert not desnudas, f"tablas sin schema: {desnudas}"


def test_une_la_tabla_del_metodo_por_id_y_periodo():
    """El `period` en el ON es lo que hace el join partition-wise."""
    q = silver_read_query("clee_h3")
    assert "LEFT JOIN silver.clee_h3 r ON r.id = e.id AND r.period = e.period" in q


def test_sin_metodo_no_une_ni_inventa_identidad():
    """La evaluación con CLEEs enmascarados lee sin la resolución materializada."""
    q = silver_read_query(None)
    assert "clee_radio" not in q and "clee_h3" not in q
    assert f"CAST(NULL AS VARCHAR) AS {RESOLVED_COLUMN}" in q
    assert f"CAST(NULL AS VARCHAR) AS {ORIGEN_COLUMN}" in q
