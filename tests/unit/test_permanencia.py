"""La tabla de permanencia: colapso por identidad, alias y censura.

Fija lo que el modelo de supervivencia consume: una fila por
establecimiento, su ventana en el directorio, y los ingredientes con que se
aplica la regla de cierre aguas abajo. Y los alias, que son control de
calidad de la resolución de identidad.
"""

from datetime import date

import polars as pl
import pytest
from sqlalchemy import func, select, text

from src.etl.loaders.permanencia import (
    METODOS,
    construir_permanencia,
    tabla_destino,
)
from src.shared.db.base import get_table
from src.shared.db.copy import copy_dataframe
from src.shared.db.partitions import create_partition
from src.shared.enums import OrigenClee

pytestmark = pytest.mark.postgres

#: Cuatro cortes consecutivos del escenario.
CORTES = [date(2019, 11, 1), date(2020, 4, 1), date(2020, 11, 1), date(2021, 5, 1)]

#: Cinco establecimientos que cubren los casos que importan:
#:
#: - `vive`: en los 4 cortes, sin huecos, presente al final -> censurado.
#: - `hueco`: en el 1, falta en el 2, vuelve en el 3 -> 1 hueco. Y cambia de
#:   nombre, que es el caso de alias legítimo (rebranding).
#: - `cerro`: solo en los dos primeros -> 2 cortes tras el último.
#: - `mudo`: cambia de municipio Y de actividad -> fusión indebida probable.
#: - `nuevo`: identidad sintética, un solo corte.
#: - `jitter`: el mismo local con coordenadas que oscilan ~80 m y vuelven al
#:   punto de partida. Neto ~0 pero dispersión grande: es ruido de GPS, no
#:   una mudanza. Es el caso que el rectángulo solo no distingue.
#:
#: (id, corte, identidad, nombre, codigo_act, cve_ent, cve_mun, lat, lon,
#:  nom_vial, numero_ext, nomb_asent, cod_postal, telefono, correoelec)
OBSERVACIONES = [
    (
        "a1",
        CORTES[0],
        "C_VIVE",
        "PANADERIA SOL",
        "311811",
        "14",
        "039",
        20.67,
        -103.35,
        "CALLE SOL",
        "5",
        "CENTRO",
        "44100",
        "",
        "",
    ),
    (
        "a2",
        CORTES[1],
        "C_VIVE",
        "PANADERIA SOL",
        "311811",
        "14",
        "039",
        20.67,
        -103.35,
        "CALLE SOL",
        "5",
        "CENTRO",
        "44100",
        "",
        "",
    ),
    (
        "a3",
        CORTES[2],
        "C_VIVE",
        "PANADERIA SOL",
        "311811",
        "14",
        "039",
        20.67,
        -103.35,
        "CALLE SOL",
        "5",
        "CENTRO",
        "44100",
        "",
        "",
    ),
    (
        "a4",
        CORTES[3],
        "C_VIVE",
        "PANADERIA SOL",
        "311811",
        "14",
        "039",
        20.67,
        -103.35,
        "CALLE SOL",
        "5",
        "CENTRO",
        "44100",
        "",
        "",
    ),
    (
        "b1",
        CORTES[0],
        "C_HUECO",
        "TACOS DON JUAN",
        "722513",
        "14",
        "039",
        20.70,
        -103.30,
        "AV JUAREZ",
        "123",
        "CENTRO",
        "44100",
        "3312345678",
        "hola@donjuan.mx",
    ),
    (
        "b3",
        CORTES[2],
        "C_HUECO",
        "TAQUERIA DON JUAN",
        "722513",
        "14",
        "039",
        20.70,
        -103.30,
        "AV JUAREZ",
        "125",
        "CENTRO",
        "44200",
        "",
        "",
    ),
    (
        "c1",
        CORTES[0],
        "C_CERRO",
        "FARMACIA LUZ",
        "464111",
        "14",
        "039",
        20.60,
        -103.40,
        "",
        "",
        "",
        "",
        "",
        "",
    ),
    (
        "c2",
        CORTES[1],
        "C_CERRO",
        "FARMACIA LUZ",
        "464111",
        "14",
        "039",
        20.60,
        -103.40,
        "",
        "",
        "",
        "",
        "",
        "",
    ),
    (
        "d1",
        CORTES[0],
        "C_MUDO",
        "ABARROTES MARY",
        "461110",
        "14",
        "039",
        20.65,
        -103.33,
        "",
        "",
        "",
        "",
        "",
        "",
    ),
    (
        "d2",
        CORTES[1],
        "C_MUDO",
        "ABARROTES MARY",
        "722513",
        "09",
        "015",
        19.43,
        -99.13,
        "",
        "",
        "",
        "",
        "",
        "",
    ),
    (
        "e1",
        CORTES[2],
        "NEW_REG_1234-abcdef",
        "MISCELANEA NUEVA",
        "461110",
        "14",
        "039",
        20.68,
        -103.36,
        "",
        "",
        "",
        "",
        "",
        "",
    ),
    (
        "f1",
        CORTES[0],
        "C_JITTER",
        "ESTETICA ANA",
        "812110",
        "14",
        "039",
        20.7000,
        -103.4000,
        "",
        "",
        "",
        "",
        "",
        "",
    ),
    (
        "f2",
        CORTES[1],
        "C_JITTER",
        "ESTETICA ANA",
        "812110",
        "14",
        "039",
        20.7007,
        -103.4000,
        "",
        "",
        "",
        "",
        "",
        "",
    ),
    (
        "f3",
        CORTES[2],
        "C_JITTER",
        "ESTETICA ANA",
        "812110",
        "14",
        "039",
        20.7000,
        -103.4000,
        "",
        "",
        "",
        "",
        "",
        "",
    ),
]


@pytest.fixture(params=sorted(METODOS), ids=sorted(METODOS))
def metodo(request):
    """Cada método: el escenario y las aserciones son los mismos."""
    return request.param


@pytest.fixture
def conn(silver_engine, metodo):
    """Hechos e identidad cargados, para el método bajo prueba."""
    identidad = METODOS[metodo]
    with silver_engine.begin() as conn:
        for corte in CORTES:
            create_partition(conn, corte)
            create_partition(conn, corte, table=identidad)

        copy_dataframe(
            conn,
            pl.DataFrame(
                [
                    (
                        o[0],
                        o[1],
                        o[3],
                        o[4],
                        o[5],
                        o[6],
                        o[7],
                        o[8],
                        o[9],
                        o[10],
                        o[11],
                        o[12],
                        o[13],
                        o[14],
                    )
                    for o in OBSERVACIONES
                ],
                schema={
                    "id": pl.Utf8,
                    "period": pl.Date,
                    "nom_estab": pl.Utf8,
                    "codigo_act": pl.Utf8,
                    "cve_ent": pl.Utf8,
                    "cve_mun": pl.Utf8,
                    "latitud": pl.Float64,
                    "longitud": pl.Float64,
                    "nom_vial": pl.Utf8,
                    "numero_ext": pl.Utf8,
                    "nomb_asent": pl.Utf8,
                    "cod_postal": pl.Utf8,
                    "telefono": pl.Utf8,
                    "correoelec": pl.Utf8,
                },
                orient="row",
            ),
            "establecimientos",
        )
        copy_dataframe(
            conn,
            pl.DataFrame(
                [
                    (
                        o[0],
                        o[1],
                        o[2],
                        OrigenClee.NUEVO.value
                        if o[2].startswith("NEW_")
                        else OrigenClee.PROPAGADO.value,
                    )
                    for o in OBSERVACIONES
                ],
                schema={
                    "id": pl.Utf8,
                    "period": pl.Date,
                    "clee_resuelto": pl.Utf8,
                    "origen": pl.Utf8,
                },
                orient="row",
            ),
            identidad,
        )
        yield conn


def permanencia(conn, metodo: str) -> dict[str, dict]:
    """La tabla construida, indexada por identidad."""
    tabla = get_table(tabla_destino(metodo))
    filas = conn.execute(select(tabla)).mappings()
    return {f["clee_resuelto"]: dict(f) for f in filas}


@pytest.mark.unit
class TestColapso:
    def test_una_fila_por_identidad(self, conn, metodo):
        """Catorce observaciones de seis establecimientos -> seis filas."""
        assert construir_permanencia(conn, metodo) == 6
        assert set(permanencia(conn, metodo)) == {
            "C_VIVE",
            "C_HUECO",
            "C_CERRO",
            "C_MUDO",
            "C_JITTER",
            "NEW_REG_1234-abcdef",
        }

    def test_reconstruir_no_duplica(self, conn, metodo):
        """Es reconstrucción total: los NEW_* no son estables entre corridas."""
        construir_permanencia(conn, metodo)
        assert construir_permanencia(conn, metodo) == 6

    def test_el_metodo_escribe_solo_su_tabla(self, conn, metodo):
        """La razón de que sea una tabla por método y no una columna."""
        otro = next(m for m in METODOS if m != metodo)
        construir_permanencia(conn, metodo)
        vacia = conn.execute(
            select(func.count()).select_from(get_table(tabla_destino(otro)))
        ).scalar_one()
        assert vacia == 0


@pytest.mark.unit
class TestVentana:
    def test_primer_y_ultimo_corte(self, conn, metodo):
        construir_permanencia(conn, metodo)
        vive = permanencia(conn, metodo)["C_VIVE"]
        assert vive["primer_periodo"] == CORTES[0]
        assert vive["ultimo_periodo"] == CORTES[3]
        assert vive["cortes_observados"] == 4

    def test_los_indices_son_posiciones_en_la_secuencia(self, conn, metodo):
        """Los cortes distan 4-12 meses: el tiempo se mide en periodos."""
        construir_permanencia(conn, metodo)
        p = permanencia(conn, metodo)
        assert (p["C_VIVE"]["idx_primer"], p["C_VIVE"]["idx_ultimo"]) == (0, 3)
        assert (p["C_CERRO"]["idx_primer"], p["C_CERRO"]["idx_ultimo"]) == (0, 1)
        assert p["NEW_REG_1234-abcdef"]["idx_primer"] == 2


@pytest.mark.unit
class TestCensura:
    def test_sin_huecos_cuando_aparece_en_todos(self, conn, metodo):
        construir_permanencia(conn, metodo)
        assert permanencia(conn, metodo)["C_VIVE"]["huecos"] == 0

    def test_detecta_el_hueco_de_quien_desaparecio_y_volvio(self, conn, metodo):
        """Es la señal de que una ausencia no implica cierre."""
        construir_permanencia(conn, metodo)
        hueco = permanencia(conn, metodo)["C_HUECO"]
        assert hueco["idx_primer"] == 0
        assert hueco["idx_ultimo"] == 2
        assert hueco["cortes_observados"] == 2
        assert hueco["huecos"] == 1

    def test_presente_en_el_ultimo_corte_no_tiene_cortes_despues(self, conn, metodo):
        """`cortes_tras_ultimo = 0` es censura por la derecha."""
        construir_permanencia(conn, metodo)
        assert permanencia(conn, metodo)["C_VIVE"]["cortes_tras_ultimo"] == 0

    def test_cuenta_los_cortes_posteriores_al_ultimo(self, conn, metodo):
        """Es el campo sobre el que se aplica la regla de cierre."""
        construir_permanencia(conn, metodo)
        p = permanencia(conn, metodo)
        assert p["C_CERRO"]["cortes_tras_ultimo"] == 2
        assert p["C_HUECO"]["cortes_tras_ultimo"] == 1

    def test_no_guarda_veredicto_de_cierre(self, conn, metodo):
        """La regla es preliminar: la tabla da ingredientes, no conclusiones."""
        columnas = {c.name for c in get_table(tabla_destino(metodo)).columns}
        assert "cerrado" not in columnas
        assert {"huecos", "cortes_tras_ultimo"} <= columnas


@pytest.mark.unit
class TestAliasYVariantes:
    def test_recoge_los_nombres_distintos(self, conn, metodo):
        construir_permanencia(conn, metodo)
        hueco = permanencia(conn, metodo)["C_HUECO"]
        assert sorted(hueco["nombres"]) == ["TACOS DON JUAN", "TAQUERIA DON JUAN"]
        assert hueco["n_nombres"] == 2

    def test_un_solo_nombre_no_es_variante(self, conn, metodo):
        construir_permanencia(conn, metodo)
        assert permanencia(conn, metodo)["C_VIVE"]["n_nombres"] == 1

    def test_marca_el_cambio_de_municipio_y_actividad(self, conn, metodo):
        """Las dos juntas son la firma de una fusión indebida."""
        construir_permanencia(conn, metodo)
        mudo = permanencia(conn, metodo)["C_MUDO"]
        assert mudo["cambio_municipio"] is True
        assert mudo["cambio_actividad"] is True
        assert sorted(mudo["municipios"]) == ["09015", "14039"]

    def test_la_direccion_va_concatenada_no_en_fragmentos(self, conn, metodo):
        """Un array por componente perdería qué número va con qué calle."""
        construir_permanencia(conn, metodo)
        hueco = permanencia(conn, metodo)["C_HUECO"]
        assert hueco["n_direcciones"] == 2
        assert sorted(hueco["direcciones"]) == [
            "AV JUAREZ | 123 | CENTRO | 44100",
            "AV JUAREZ | 125 | CENTRO | 44200",
        ]

    def test_el_contacto_es_la_union_no_el_mas_reciente(self, conn, metodo):
        """Para verificar un cierre sirve cualquier teléfono que publicó.

        `C_HUECO` trae teléfono solo en el primer corte; si se tomara el del
        representante se perdería.
        """
        construir_permanencia(conn, metodo)
        hueco = permanencia(conn, metodo)["C_HUECO"]
        assert hueco["telefonos"] == ["3312345678"]
        assert hueco["correos"] == ["hola@donjuan.mx"]

    def test_sin_contacto_el_array_queda_nulo_no_con_vacios(self, conn, metodo):
        """Una cadena vacía no es una variante, es un dato ausente."""
        construir_permanencia(conn, metodo)
        assert permanencia(conn, metodo)["C_VIVE"]["telefonos"] is None

    def test_no_marca_cambio_cuando_no_se_movio(self, conn, metodo):
        construir_permanencia(conn, metodo)
        vive = permanencia(conn, metodo)["C_VIVE"]
        assert vive["cambio_municipio"] is False
        assert vive["cambio_actividad"] is False

    def test_el_neto_detecta_la_mudanza_real(self, conn, metodo):
        """Guadalajara a CDMX son ~460 km entre dos puntos REALES."""
        construir_permanencia(conn, metodo)
        p = permanencia(conn, metodo)
        assert p["C_MUDO"]["desplazamiento_neto_m"] > 400_000
        assert p["C_VIVE"]["desplazamiento_neto_m"] == pytest.approx(0, abs=1)

    def test_el_neto_distingue_jitter_de_mudanza(self, conn, metodo):
        """El par neto/dispersión es lo que el rectángulo solo no puede.

        `C_JITTER` oscila ~80 m y vuelve al origen: neto ~0 pero dispersión
        grande. Con solo la diagonal del rectángulo se leería como si se
        hubiera movido.
        """
        construir_permanencia(conn, metodo)
        jitter = permanencia(conn, metodo)["C_JITTER"]
        assert jitter["desplazamiento_neto_m"] == pytest.approx(0, abs=1)
        assert jitter["dispersion_max_m"] > 50
        assert jitter["n_posiciones"] == 2

    def test_la_dispersion_expone_el_rectangulo(self, conn, metodo):
        """Las cuatro esquinas quedan accesibles, no solo su diagonal."""
        construir_permanencia(conn, metodo)
        mudo = permanencia(conn, metodo)["C_MUDO"]
        assert mudo["lat_min"] == pytest.approx(19.43)
        assert mudo["lat_max"] == pytest.approx(20.65)
        assert mudo["dispersion_max_m"] > 400_000

    def test_guarda_la_primera_posicion(self, conn, metodo):
        """El otro extremo real del recorrido, para calcular el neto."""
        construir_permanencia(conn, metodo)
        mudo = permanencia(conn, metodo)["C_MUDO"]
        assert mudo["lat_primera"] == pytest.approx(20.65)
        assert mudo["latitud"] == pytest.approx(19.43)  # el representante


@pytest.mark.unit
class TestRepresentante:
    def test_los_atributos_son_del_corte_mas_reciente(self, conn, metodo):
        """El estado con que el establecimiento terminó."""
        construir_permanencia(conn, metodo)
        hueco = permanencia(conn, metodo)["C_HUECO"]
        assert hueco["nom_estab"] == "TAQUERIA DON JUAN"

    def test_marca_las_identidades_sinteticas(self, conn, metodo):
        construir_permanencia(conn, metodo)
        p = permanencia(conn, metodo)
        assert p["NEW_REG_1234-abcdef"]["es_sintetica"] is True
        assert p["C_VIVE"]["es_sintetica"] is False


@pytest.mark.unit
class TestSinIdentidad:
    def test_sin_filas_de_identidad_la_tabla_queda_vacia(self, conn, metodo):
        """Correr permanencia antes de la resolución no inventa nada."""
        conn.execute(text(f"DELETE FROM silver.{METODOS[metodo]}"))
        assert construir_permanencia(conn, metodo) == 0

    def test_metodo_desconocido_falla_explicito(self, conn):
        with pytest.raises(ValueError, match="Metodo desconocido"):
            construir_permanencia(conn, "inexistente")
