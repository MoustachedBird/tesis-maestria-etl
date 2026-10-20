"""Validacion contra catalogos y traduccion de etiquetas a llaves.

Lo que protege este modulo no es un error ruidoso sino uno silencioso: una
etiqueta que ningun catalogo conoce no falla al traducirse, se vuelve NULL.
Y el NULL es legitimo --hay establecimientos sin tipo de vialidad-- asi que
un valor nuevo de INEGI se perderia en millones de filas sin que nada avise.
"""

import polars as pl
import pytest
from sqlalchemy import insert

from src.etl.transformers.catalogs import (
    CatalogoDesactualizado,
    map_labels_to_ids,
    resolve,
    validate_codigo_act,
    validate_domain_labels,
    validate_geografia,
)
from src.shared.db.base import get_table

pytestmark = pytest.mark.postgres


@pytest.fixture
def conn(silver_engine):
    """Conexion con unos catalogos minimos sembrados.

    No se usan los seeds reales: con dos valores por catalogo se prueba lo
    mismo y el test dice exactamente contra que se valida.
    """
    with silver_engine.begin() as conn:
        conn.execute(
            insert(get_table("denue_tipos_vialidad")),
            [
                {"id": 1, "tipo_vial": "CALLE", "descripcion": "via urbana"},
                {"id": 2, "tipo_vial": "AVENIDA", "descripcion": "via amplia"},
            ],
        )
        conn.execute(
            insert(get_table("denue_tipos_unidad_economica")),
            [{"id": 1, "tipounieco": "FIJO", "descripcion": "establecimiento fijo"}],
        )
        conn.execute(
            insert(get_table("denue_estratos_personal")),
            [
                {
                    "id": 3,
                    "per_ocu": "11 A 30 PERSONAS",
                    "descripcion": "mediana",
                    "empleados_min": 11,
                    "empleados_max": 30,
                    "categoria": "Small",
                }
            ],
        )
        conn.execute(
            insert(get_table("denue_actividades_economicas")),
            [
                {
                    "codigo_act": "722515",
                    "nombre_act": "Cafeterias",
                    "sector": "72",
                    "subsector": "722",
                    "rama": "7225",
                }
            ],
        )
        conn.execute(
            insert(get_table("denue_entidades")),
            [{"cve_ent": "09", "entidad": "Ciudad de Mexico"}],
        )
        conn.execute(
            insert(get_table("denue_municipios")),
            [{"cve_ent": "09", "cve_mun": "015", "municipio": "Cuauhtemoc"}],
        )
        conn.execute(
            insert(get_table("denue_localidades")),
            [
                {
                    "cve_ent": "09",
                    "cve_mun": "015",
                    "cve_loc": "0001",
                    "localidad": "Centro",
                }
            ],
        )
        yield conn


class TestEtiquetasDeDominio:
    def test_las_conocidas_pasan(self, conn):
        validate_domain_labels(conn, pl.DataFrame({"tipo_vial": ["CALLE", "AVENIDA"]}))

    def test_una_desconocida_se_detiene(self, conn):
        with pytest.raises(CatalogoDesactualizado, match="tipo_vial"):
            validate_domain_labels(conn, pl.DataFrame({"tipo_vial": ["CALLE", "TELEFERICO"]}))

    def test_el_error_nombra_el_valor_y_el_catalogo(self, conn):
        """Para que la accion sea directa: agregarlo al seed."""
        with pytest.raises(CatalogoDesactualizado) as exc:
            validate_domain_labels(conn, pl.DataFrame({"tipo_vial": ["TELEFERICO"]}))
        assert "TELEFERICO" in str(exc.value)
        assert "denue_tipos_vialidad" in str(exc.value)
        assert "seed" in str(exc.value)

    def test_los_nulos_son_validos(self, conn):
        """Un establecimiento sin tipo de vialidad no es un error."""
        validate_domain_labels(conn, pl.DataFrame({"tipo_vial": ["CALLE", None]}))

    def test_una_columna_ausente_no_se_valida(self, conn):
        """Los CSV de encuesta vienen incompletos y eso es esperable."""
        validate_domain_labels(conn, pl.DataFrame({"otra": ["x"]}))

    def test_un_valor_nuevo_se_habilita_sembrandolo(self, conn):
        """El punto de leer el dominio de la tabla y no de una lista.

        Cuando INEGI publica un valor nuevo, habilitarlo es agregar una fila
        al seed. No se toca codigo Python.
        """
        df = pl.DataFrame({"tipo_vial": ["TELEFERICO"]})
        with pytest.raises(CatalogoDesactualizado):
            validate_domain_labels(conn, df)

        conn.execute(
            insert(get_table("denue_tipos_vialidad")),
            [{"id": 99, "tipo_vial": "TELEFERICO", "descripcion": "corte nuevo"}],
        )
        validate_domain_labels(conn, df)  # ahora pasa, sin cambiar codigo

    def test_valida_los_cuatro_dominios(self, conn):
        df = pl.DataFrame(
            {
                "tipo_vial": ["CALLE"],
                "tipounieco": ["FIJO"],
                "per_ocu": ["11 A 30 PERSONAS"],
                "tipo_asent": [None],
            }
        )
        validate_domain_labels(conn, df)

        with pytest.raises(CatalogoDesactualizado, match="per_ocu"):
            validate_domain_labels(conn, df.with_columns(per_ocu=pl.lit("1 A 5 MILLONES")))


class TestCodigoAct:
    def test_el_conocido_pasa(self, conn):
        validate_codigo_act(conn, pl.DataFrame({"codigo_act": ["722515"]}))

    def test_uno_nuevo_se_detiene(self, conn):
        """SCIAN evoluciona entre cortes: un codigo nuevo es esperable."""
        with pytest.raises(CatalogoDesactualizado, match="SCIAN"):
            validate_codigo_act(conn, pl.DataFrame({"codigo_act": ["722515", "999999"]}))

    def test_los_nulos_son_validos(self, conn):
        validate_codigo_act(conn, pl.DataFrame({"codigo_act": [None]}))


class TestGeografia:
    def test_las_claves_conocidas_pasan(self, conn):
        df = pl.DataFrame({"cve_ent": ["09"], "cve_mun": ["015"], "cve_loc": ["0001"]})
        validate_geografia(conn, df)

    def test_el_error_dice_que_nivel_falto(self, conn):
        """Lo que una violacion de FK no distingue."""
        df = pl.DataFrame({"cve_ent": ["09"], "cve_mun": ["999"]})
        with pytest.raises(CatalogoDesactualizado, match="municipio"):
            validate_geografia(conn, df)

        with pytest.raises(CatalogoDesactualizado, match="entidad"):
            validate_geografia(conn, pl.DataFrame({"cve_ent": ["99"]}))

    def test_un_nivel_incompleto_no_se_valida(self, conn):
        """Sin `cve_loc`, el nivel de localidad se omite."""
        validate_geografia(conn, pl.DataFrame({"cve_ent": ["09"], "cve_mun": ["015"]}))


class TestTraduccion:
    def test_la_etiqueta_se_vuelve_llave(self, conn):
        salida = map_labels_to_ids(conn, pl.DataFrame({"tipo_vial": ["CALLE", "AVENIDA"]}))
        assert salida["tipo_vial_id"].to_list() == [1, 2]

    def test_la_columna_de_etiqueta_desaparece(self, conn):
        """La tabla guarda la llave, no el texto."""
        salida = map_labels_to_ids(conn, pl.DataFrame({"tipo_vial": ["CALLE"]}))
        assert "tipo_vial" not in salida.columns

    def test_un_nulo_da_llave_nula(self, conn):
        salida = map_labels_to_ids(conn, pl.DataFrame({"tipo_vial": ["CALLE", None]}))
        assert salida["tipo_vial_id"].to_list() == [1, None]

    def test_traduce_los_cuatro_a_la_vez(self, conn):
        df = pl.DataFrame(
            {
                "id": ["a"],
                "tipo_vial": ["CALLE"],
                "tipounieco": ["FIJO"],
                "per_ocu": ["11 A 30 PERSONAS"],
            }
        )
        salida = map_labels_to_ids(conn, df)
        assert salida["tipo_vial_id"][0] == 1
        assert salida["tipounieco_id"][0] == 1
        assert salida["per_ocu_id"][0] == 3
        assert salida["id"][0] == "a", "las columnas ajenas deben sobrevivir"

    def test_sin_validar_una_desconocida_se_volveria_null(self, conn):
        """El motivo de que validar vaya ANTES de traducir.

        Si se tradujera primero, una etiqueta que INEGI acaba de inventar
        quedaria indistinguible de un dato ausente -- y nadie se enteraria.
        """
        salida = map_labels_to_ids(conn, pl.DataFrame({"tipo_vial": ["TELEFERICO"]}))
        assert salida["tipo_vial_id"].to_list() == [None]


class TestResolveCompleto:
    def test_valida_y_traduce(self, conn):
        df = pl.DataFrame(
            {
                "id": ["a"],
                "codigo_act": ["722515"],
                "cve_ent": ["09"],
                "tipo_vial": ["CALLE"],
            }
        )
        salida = resolve(conn, df)
        assert salida["tipo_vial_id"][0] == 1
        assert salida["codigo_act"][0] == "722515", "codigo_act no se traduce, es la llave"

    def test_se_detiene_antes_de_traducir(self, conn):
        with pytest.raises(CatalogoDesactualizado):
            resolve(conn, pl.DataFrame({"tipo_vial": ["TELEFERICO"]}))
