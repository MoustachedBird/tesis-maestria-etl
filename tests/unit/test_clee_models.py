"""Propiedades estructurales de las tablas de resolución CLEE.

Fijan lo que el diseño de `docs/separacion_ingest_clee.md` necesita para
funcionar: la llave compuesta que permite particionar, el particionado que
permite el reemplazo atómico, y que los métodos compartan núcleo pero no
tabla. Se leen del metadata del ORM, así que no necesitan servidor.
"""

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from src.shared.db.base import Base
from src.shared.db.models import clee
from src.shared.enums import OrigenClee

TABLAS = ("silver.clee_radio", "silver.clee_h3")

NUCLEO = {"id", "period", "clee_resuelto", "origen", "resuelto_en"}
"""Columnas que el mixin aporta a todo método de resolución."""


@pytest.fixture(params=TABLAS, ids=[t.split(".")[1] for t in TABLAS])
def tabla(request):
    """Cada tabla de resolución, una por caso de prueba."""
    return Base.metadata.tables[request.param]


def test_llave_compuesta_id_y_periodo(tabla):
    """La PK es `(id, period)`.

    `period` no desambigua —ya es uno de los componentes del hash de `id`—
    pero PostgreSQL exige la clave de partición dentro de toda PK.
    """
    assert [c.name for c in tabla.primary_key] == ["id", "period"]


def test_particionada_por_periodo(tabla):
    """`LIST (period)`: una partición por corte, la unidad de recuperación.

    Es lo que permite reemplazar el resultado de un corte con ATTACH
    PARTITION en vez de borrarlo y reinsertarlo.
    """
    assert tabla.dialect_options["postgresql"].get("partition_by") == "LIST (period)"
    assert "PARTITION BY LIST (period)" in str(
        CreateTable(tabla).compile(dialect=postgresql.dialect())
    )


def test_el_nucleo_viene_del_mixin(tabla):
    """Toda tabla de resolución trae las mismas columnas base."""
    assert NUCLEO <= {c.name for c in tabla.columns}


def test_la_identidad_y_su_origen_son_obligatorios(tabla):
    """Sin `clee_resuelto` y `origen` la fila no dice nada util."""
    assert not tabla.c.clee_resuelto.nullable
    assert not tabla.c.origen.nullable


def test_indices_de_consulta(tabla):
    """`clee_resuelto` y `origen` indexados; `period` no.

    La unidad de análisis agrupa por identidad, y filtrar por origen separa
    lo que INEGI publicó de lo que el pipeline infirió. `period` es la clave
    de partición: dentro de cada partición es constante, así que un índice
    ahí serían 20 árboles para cero selectividad.
    """
    columnas_indexadas = {c.name for idx in tabla.indexes for c in idx.columns}
    assert columnas_indexadas == {"clee_resuelto", "origen"}


def test_los_metodos_no_comparten_tabla():
    """Radio y H3 son tablas distintas: ningún escritor toca la del otro."""
    assert clee.CleeRadio.__tablename__ != clee.CleeH3.__tablename__
    assert len({t for t in TABLAS}) == len(TABLAS)


def test_origen_cubre_las_tres_formas_de_resolver():
    """DIRECTO, PROPAGADO y NUEVO son las tres que el resolver produce.

    DIRECTO es el `clee` de INEGI copiado, PROPAGADO el heredado por
    similitud, y NUEVO el sintético `NEW_<pref>-<hex>` de `assign_new_clees`.
    """
    assert {o.value for o in OrigenClee} == {"DIRECTO", "PROPAGADO", "NUEVO"}


def test_la_columna_origen_admite_todo_el_enum(tabla):
    """El `String(10)` no puede truncar un valor de `OrigenClee`."""
    assert max(len(o.value) for o in OrigenClee) <= tabla.c.origen.type.length
