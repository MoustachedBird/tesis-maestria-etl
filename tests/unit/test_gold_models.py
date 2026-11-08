"""El esquema de `gold`: lo que garantiza por construcción y lo que no tiene.

Dos clases de aserción, y las dos importan igual:

- **Lo que la base impide.** El índice único parcial y la FK no son
  convenciones documentadas: son restricciones que hacen fallar el `INSERT`.
  Si alguien las quita, estos tests lo dicen.
- **Las columnas que NO existen.** `es_sintetica`, `origen`,
  `cortes_propagado` y `cortes_directo` son constantes en esta cohorte, y su
  ausencia **es** el invariante "toda identidad la asignó INEGI". Un test
  que las prohíbe evita que alguien las reponga por simetría con
  `permanencia`.
"""

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.shared.db.base import get_table
from src.shared.db.models.gold import (
    CAMPOS_CON_VARIANTES,
    NegocioCleeOficial,
    NegocioCleeOficialVariante,
)

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


@pytest.fixture
def gold_engine(pg_engine):
    """Engine con el schema `gold` recién creado desde el modelo.

    Mismo camino que la migración (`create_all` sobre los `Table` de la
    capa), así que lo que se prueba es el esquema real. `pg_trgm` se instala
    aquí porque el índice GIN de `valor` no se puede crear sin ella; es
    *trusted* desde PG13, así que no hace falta superusuario.

    Yields:
        Engine: con `gold` poblado de tablas vacías.
    """
    from src.shared.db.base import Base, tables_for

    with pg_engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        conn.execute(text("DROP SCHEMA IF EXISTS gold CASCADE"))
        conn.execute(text("CREATE SCHEMA gold"))
    Base.metadata.create_all(pg_engine, tables=tables_for("gold"))
    yield pg_engine


def _negocio(clee: str = "14TAC0001", **extra) -> NegocioCleeOficial:
    """Un negocio con lo mínimo no nulo, para no repetirlo en cada test."""
    campos = {
        "clee": clee,
        "primer_periodo": VENTANA[0],
        "ultimo_periodo": VENTANA[-1],
        "idx_primer": 0,
        "idx_ultimo": 8,
        "cortes_observados": 9,
        "huecos": 0,
        "cortes_tras_ultimo": 0,
        "duracion_periodos": 8,
        "presente_desde_el_inicio": True,
        "cambio_tamano": False,
        "n_nombres": 2,
        "n_direcciones": 1,
        "cambio_actividad": False,
        "cambio_municipio": False,
        "n_posiciones": 1,
        "corte_referencia": VENTANA[-1],
        "cluster_sospechoso": False,
        "ventana_cortes": VENTANA,
        "metodo_base": "radio",
        "version_reglas": "v1",
        **{
            f"tiene_{c}": True
            for c in (
                "nombre",
                "razon_social",
                "actividad",
                "municipio",
                "localidad",
                "ageb",
                "cod_postal",
                "tipo_asent",
                "coordenadas",
                "estrato_personal",
                "tipounieco",
                "telefono",
                "correo",
                "sitio",
            )
        },
        "en_centro_comercial": False,
    }
    campos.update(extra)
    return NegocioCleeOficial(**campos)


def _variante(clee: str, campo: str, valor: str, canonico: bool) -> NegocioCleeOficialVariante:
    """Una variante con su ventana mínima."""
    return NegocioCleeOficialVariante(
        clee=clee,
        campo=campo,
        valor=valor,
        primer_periodo=VENTANA[0],
        ultimo_periodo=VENTANA[-1],
        n_cortes=1,
        es_canonico=canonico,
    )


@pytest.mark.unit
class TestLoQueLaBaseImpide:
    def test_un_solo_canonico_por_negocio_y_campo(self, gold_engine):
        """El índice único parcial, que es lo que vuelve estructural la regla.

        Con dos canónicos para el mismo campo, el valor de la tabla madre
        sería indefendible: no habría forma de saber de cuál salió.
        """
        with Session(gold_engine) as s:
            s.add(_negocio())
            s.add(_variante("14TAC0001", "nom_estab", "TACOS PACO", canonico=True))
            s.commit()

            s.add(_variante("14TAC0001", "nom_estab", "TAQUERIA DON PACO", canonico=True))
            with pytest.raises(IntegrityError):
                s.commit()

    def test_varias_variantes_no_canonicas_si_se_permiten(self, gold_engine):
        """El índice es PARCIAL: solo restringe las canónicas."""
        with Session(gold_engine) as s:
            s.add(_negocio())
            s.add(_variante("14TAC0001", "nom_estab", "TACOS PACO", canonico=True))
            s.add(_variante("14TAC0001", "nom_estab", "TAQUERIA DON PACO", canonico=False))
            s.add(_variante("14TAC0001", "nom_estab", "TAQUITOS PAQUITO", canonico=False))
            s.commit()

            total = s.query(NegocioCleeOficialVariante).count()
        assert total == 3

    def test_un_canonico_por_CAMPO_no_por_negocio(self, gold_engine):
        """Cada campo tiene el suyo: nombre, SCIAN, teléfono..."""
        with Session(gold_engine) as s:
            s.add(_negocio())
            s.add(_variante("14TAC0001", "nom_estab", "TACOS PACO", canonico=True))
            s.add(_variante("14TAC0001", "codigo_act", "722513", canonico=True))
            s.add(_variante("14TAC0001", "telefono", "3312345678", canonico=True))
            s.commit()
            assert s.query(NegocioCleeOficialVariante).count() == 3

    def test_una_variante_sin_negocio_no_entra(self, gold_engine):
        """La FK: la evidencia no puede existir sin la fila que justifica."""
        with Session(gold_engine) as s:
            s.add(_variante("NO_EXISTE", "nom_estab", "FANTASMA", canonico=True))
            with pytest.raises(IntegrityError):
                s.commit()

    def test_borrar_el_negocio_se_lleva_sus_variantes(self, gold_engine):
        """`ON DELETE CASCADE`: no quedan variantes huérfanas."""
        with Session(gold_engine) as s:
            s.add(_negocio())
            s.add(_variante("14TAC0001", "nom_estab", "TACOS PACO", canonico=True))
            s.commit()

            s.delete(s.get(NegocioCleeOficial, "14TAC0001"))
            s.commit()
            assert s.query(NegocioCleeOficialVariante).count() == 0


@pytest.mark.unit
class TestLasColumnasQueNoExisten:
    """Su ausencia ES el invariante de la cohorte, no un olvido."""

    def test_sin_columnas_de_identidad_inferida(self):
        """Las cuatro son constantes aquí; tenerlas sería ancho inútil.

        `es_sintetica` siempre falsa, `origen` siempre DIRECTO,
        `cortes_propagado` siempre 0 y `cortes_directo` igual a
        `cortes_observados`. No tenerlas dice que toda identidad de esta
        tabla la asignó INEGI.
        """
        columnas = {c.name for c in get_table("negocios_clee_oficial", schema="gold").columns}
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
        columnas = {c.name for c in get_table("negocios_clee_oficial", schema="gold").columns}
        assert "clee" in columnas
        assert "clee_resuelto" not in columnas

    def test_no_guarda_el_veredicto_de_cierre(self):
        """La regla es preliminar: la tabla da ingredientes, no conclusiones."""
        columnas = {c.name for c in get_table("negocios_clee_oficial", schema="gold").columns}
        assert not columnas & {"cerrado", "estatus"}
        assert {"cortes_tras_ultimo", "huecos"} <= columnas


@pytest.mark.unit
class TestProcedencia:
    """Lo que vuelve citable a una fila de Gold."""

    def test_la_ventana_queda_en_la_fila(self, gold_engine):
        """Si mañana entra un corte, la N cambia y esto lo explica."""
        with Session(gold_engine) as s:
            s.add(_negocio())
            s.commit()
            guardado = s.get(NegocioCleeOficial, "14TAC0001")
            assert guardado.ventana_cortes == VENTANA
            assert len(guardado.ventana_cortes) == 9
            assert guardado.construida_en is not None

    def test_la_procedencia_es_obligatoria(self, gold_engine):
        """Sin método y versión de reglas la fila no es reproducible."""
        with Session(gold_engine) as s:
            s.add(_negocio(metodo_base=None))
            with pytest.raises(IntegrityError):
                s.commit()


@pytest.mark.unit
class TestBusquedaDifusa:
    def test_encuentra_un_negocio_por_nombre_parecido(self, gold_engine):
        """Es la razón de que las variantes sean tabla y no un `text[]`.

        "¿Qué negocio es *tacos paco*?" es la pregunta que se hace al
        verificar un caso a mano, y con un array no tiene respuesta
        razonable.
        """
        with Session(gold_engine) as s:
            s.add(_negocio())
            s.add(_variante("14TAC0001", "nom_estab", "TAQUERIA DON PACO", canonico=True))
            s.commit()

        with gold_engine.connect() as conn:
            encontrado = (
                conn.execute(
                    text(
                        "SELECT clee, valor, similarity(valor, :q) AS sim "
                        "FROM gold.negocios_clee_oficial_variantes "
                        "WHERE campo = 'nom_estab' AND valor % :q "
                        "ORDER BY sim DESC LIMIT 1"
                    ),
                    {"q": "TACOS PACO"},
                )
                .mappings()
                .first()
            )

        assert encontrado is not None, "el índice trigram no encontró la variante"
        assert encontrado["clee"] == "14TAC0001"


@pytest.mark.unit
def test_los_campos_con_variantes_son_los_documentados():
    """Agregar uno es una decisión de diseño (§6.1), no un descuido."""
    assert CAMPOS_CON_VARIANTES == (
        "nom_estab",
        "raz_social",
        "codigo_act",
        "telefono",
        "correoelec",
        "www",
    )
