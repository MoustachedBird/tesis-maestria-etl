"""El esquema de `gold`: lo que garantiza por construcción y lo que no tiene.

Dos clases de aserción, y las dos importan igual:

- **Lo que la base impide.** La procedencia obligatoria y los tipos no son
  convenciones documentadas: son restricciones que hacen fallar el `INSERT`.
- **Las columnas que NO existen**, que es donde vive media decisión de
  diseño. `es_sintetica`, `origen`, `cortes_propagado` y `cortes_directo` son
  constantes en esta cohorte; las quince `tiene_*`, el rectángulo envolvente
  y los conteos de variantes ya están en `silver.permanencia_<metodo>` y
  repetirlos aquí fue el error del primer diseño. Un test que los prohíbe
  evita que alguien los reponga por simetría.
"""

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.shared.db.base import get_table
from src.shared.db.models.gold import ORIGEN_NOMBRE_MODA, NegocioCleeOficial

pytestmark = pytest.mark.postgres

#: Ventana oficial medida el 2026-11-08: 9 cortes, 2021-05 .. 2026-05.
VENTANA = [
    date(2021, 5, 1),
    date(2021, 11, 1),
    date(2022, 5, 1),
    date(2022, 11, 1),
    date(2023, 11, 1),
    date(2024, 5, 1),
    date(2024, 11, 1),
    date(2025, 5, 1),
    date(2026, 5, 1),
]

TABLA = "negocios_clee_oficial"


@pytest.fixture
def gold_engine(pg_engine):
    """Engine con el schema `gold` recién creado desde el modelo.

    Mismo camino que la migración (`create_all` sobre los `Table` de la
    capa), así que lo que se prueba es el esquema real. **No instala
    `pg_trgm`**: la necesitaba el índice GIN de la tabla de variantes, y esa
    tabla ya no existe.

    Yields:
        Engine: con `gold` poblado de tablas vacías.
    """
    from src.shared.db.base import Base, tables_for

    with pg_engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS gold CASCADE"))
        conn.execute(text("CREATE SCHEMA gold"))
    Base.metadata.create_all(pg_engine, tables=tables_for("gold"))
    yield pg_engine


def _negocio(clee: str = "14TAC0001", **extra) -> NegocioCleeOficial:
    """Un negocio con lo mínimo no nulo, para no repetirlo en cada test."""
    campos = {
        "clee": clee,
        "nombre": "TACOS PACO",
        "nombre_origen": ORIGEN_NOMBRE_MODA,
        "n_nombres": 2,
        "periodos": VENTANA,
        "primer_periodo": VENTANA[0],
        "ultimo_periodo": VENTANA[-1],
        "cortes_observados": 9,
        "huecos": 0,
        "cortes_tras_ultimo": 0,
        "fecha_alta": date(2015, 3, 1),
        "edad_anios": 11.17,
        "dias_alta_a_primera": 2253,
        "cerrado": False,
        "empleados_inicial": 0,
        "empleados_final": 11,
        "tamano_inicial": "Micro",
        "tamano_final": "Small",
        "cambio_tamano": True,
        "cve_geo_inicial": "14039",
        "cve_geo_final": "14039",
        "lat_inicial": 20.67,
        "lon_inicial": -103.35,
        "lat_final": 20.67,
        "lon_final": -103.35,
        "h3_cell_9_final": 618287039762661375,
        "se_mudo": False,
        "distancia_mudanza_m": 0.0,
        "n_posiciones": 1,
        "codigo_act": "722513",
        "cambio_actividad": False,
        "cluster_sospechoso": False,
        "ventana_cortes": VENTANA,
        "metodo_base": "inegi",
        "version_reglas": "v1",
    }
    campos.update(extra)
    return NegocioCleeOficial(**campos)


@pytest.mark.unit
class TestLasColumnasQueNoExisten:
    """Su ausencia ES el diseño, no un olvido."""

    def test_sin_columnas_de_identidad_inferida(self):
        """Las cuatro son constantes aquí; tenerlas sería ancho inútil.

        `es_sintetica` siempre falsa, `origen` siempre DIRECTO,
        `cortes_propagado` siempre 0 y `cortes_directo` igual a
        `cortes_observados`. No tenerlas dice que toda identidad de esta
        tabla la asignó INEGI.
        """
        columnas = {c.name for c in get_table(TABLA, schema="gold").columns}
        assert not columnas & {
            "es_sintetica",
            "origen",
            "cortes_propagado",
            "cortes_directo",
        }

    def test_la_identidad_se_llama_clee_no_clee_resuelto(self):
        """En esta cohorte no se resolvió nada: es el id que publicó INEGI.

        La tabla de la cohorte propagada sí llevaría `clee_resuelto`, y esa
        diferencia de nombre es documentación.
        """
        columnas = {c.name for c in get_table(TABLA, schema="gold").columns}
        assert "clee" in columnas
        assert "clee_resuelto" not in columnas

    def test_sin_banderas_de_cobertura(self):
        """Quince columnas que `silver.permanencia_<metodo>` ya tiene.

        Con los valores concretos materializados --empleados y tamaño
        inicial y final, las dos posiciones, la llave geográfica-- un `NULL`
        dice lo mismo que decía la bandera.
        """
        columnas = {c.name for c in get_table(TABLA, schema="gold").columns}
        assert not {c for c in columnas if c.startswith("tiene_")}

    def test_sin_los_agregados_que_ya_estan_en_permanencia(self):
        """Repetirlos fue el error del primer diseño de Gold.

        El rectángulo envolvente, la dispersión y los conteos de variantes
        existen ya en Silver; copiarlos aquí hacía de Gold una vista
        filtrada en vez de una tabla con algo propio.
        """
        columnas = {c.name for c in get_table(TABLA, schema="gold").columns}
        assert not columnas & {
            "lat_min",
            "lat_max",
            "lon_min",
            "lon_max",
            "dispersion_max_m",
            "n_direcciones",
            "cortes_idx",
            "duracion_periodos",
            "presente_desde_el_inicio",
        }

    def test_no_existe_la_tabla_de_variantes(self):
        """Los valores distintos salen de `silver.establecimientos`."""
        from src.shared.db.base import tables_for

        nombres = {t.name for t in tables_for("gold")}
        assert nombres == {TABLA}


@pytest.mark.unit
class TestLoQueSiAporta:
    """Las tres cosas que justifican que la tabla exista."""

    def test_la_edad_y_el_veredicto(self):
        columnas = {c.name for c in get_table(TABLA, schema="gold").columns}
        assert {"edad_anios", "cerrado", "fecha_alta"} <= columnas

    def test_los_dos_extremos_de_la_trayectoria(self):
        columnas = {c.name for c in get_table(TABLA, schema="gold").columns}
        assert {
            "empleados_inicial",
            "empleados_final",
            "tamano_inicial",
            "tamano_final",
            "cve_geo_inicial",
            "cve_geo_final",
            "lat_inicial",
            "lat_final",
        } <= columnas

    def test_y_los_ingredientes_para_recalcular_el_cierre(self):
        """Cambiar el umbral no debe exigir reconstruir 9.58M filas."""
        columnas = {c.name for c in get_table(TABLA, schema="gold").columns}
        assert {"cortes_tras_ultimo", "huecos"} <= columnas


@pytest.mark.unit
class TestLaFilaEsCitable:
    def test_la_ventana_queda_en_la_fila(self, gold_engine):
        """Si mañana entra un corte, la N y las edades cambian; esto lo explica."""
        with Session(gold_engine) as s:
            s.add(_negocio())
            s.commit()
            guardado = s.get(NegocioCleeOficial, "14TAC0001")
            assert guardado.ventana_cortes == VENTANA
            assert len(guardado.ventana_cortes) == 9
            assert guardado.construida_en is not None

    def test_los_periodos_observados_se_guardan(self, gold_engine):
        """De ahí sale cualquier patrón de presencia, y los años."""
        with Session(gold_engine) as s:
            s.add(_negocio(periodos=[VENTANA[0], VENTANA[2]], cortes_observados=2))
            s.commit()
            guardado = s.get(NegocioCleeOficial, "14TAC0001")
            assert guardado.periodos == [VENTANA[0], VENTANA[2]]

    def test_la_procedencia_es_obligatoria(self, gold_engine):
        """Sin método y versión de reglas la fila no es reproducible."""
        with Session(gold_engine) as s:
            s.add(_negocio(metodo_base=None))
            with pytest.raises(IntegrityError):
                s.commit()

    def test_el_veredicto_es_obligatorio(self, gold_engine):
        """Un `cerrado` nulo haría irrepresentable la poblacion a muestrear."""
        with Session(gold_engine) as s:
            s.add(_negocio(cerrado=None))
            with pytest.raises(IntegrityError):
                s.commit()


@pytest.mark.unit
class TestLaEdadSoportaLosCasosReales:
    def test_sin_fecha_de_alta_la_edad_es_nula(self, gold_engine):
        """No se sustituye por el primer corte: seria otra cosa."""
        with Session(gold_engine) as s:
            s.add(_negocio(fecha_alta=None, edad_anios=None, dias_alta_a_primera=None))
            s.commit()
            assert s.get(NegocioCleeOficial, "14TAC0001").edad_anios is None

    def test_guarda_dos_decimales(self, gold_engine):
        """`Numeric(6, 2)`: la edad entra al modelo como variable continua."""
        with Session(gold_engine) as s:
            s.add(_negocio(edad_anios=11.17))
            s.commit()
            assert float(s.get(NegocioCleeOficial, "14TAC0001").edad_anios) == 11.17

    def test_admite_una_fecha_de_alta_inconsistente(self, gold_engine):
        """`dias_alta_a_primera` negativo es un diagnostico, no un error.

        El negocio aparecio en el DENUE antes de su fecha de alta. La fila se
        guarda --el dato es de INEGI-- y la columna permite excluirla de un
        analisis de antiguedad.
        """
        with Session(gold_engine) as s:
            s.add(_negocio(dias_alta_a_primera=-120))
            s.commit()
            assert s.get(NegocioCleeOficial, "14TAC0001").dias_alta_a_primera == -120


@pytest.mark.unit
def test_los_indices_sirven_al_muestreo(gold_engine):
    """Los tres que decide el proposito: cierre, edad y fragilidad.

    `huecos` esta indexado porque es por donde se estratifica la muestra:
    donde ya desaparecio y volvio, la regla de cierre es mas fragil.
    """
    indices = {i.name for i in get_table(TABLA, schema="gold").indexes}
    assert "ix_negocios_clee_oficial_cerrado" in indices
    assert "ix_negocios_clee_oficial_edad" in indices
    assert "ix_negocios_clee_oficial_huecos" in indices
