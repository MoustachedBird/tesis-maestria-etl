"""La tabla de permanencia: colapso por identidad, trayectoria y censura.

Fija lo que el modelo de supervivencia consume: una fila por
establecimiento, su ventana en el directorio, y los ingredientes con que se
aplica la regla de cierre aguas abajo.

La tabla guarda tres clases de columna, y las tres son propiedades de la
vida completa: la ventana, los agregados de trayectoria (cuánto varió algo)
y las banderas de cobertura (`tiene_*`: el campo vino lleno al menos una
vez). **Ningún valor de un corte concreto.** `TestSinValoresDeUnCorte` fija
esa frontera y `TestValoresRecuperables` prueba que no se perdió nada: el
join a los hechos devuelve los valores de todos los cortes.
"""

from datetime import date

import polars as pl
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from src.etl.loaders import permanencia as loader
from src.etl.loaders.dominios import build_rows
from src.etl.loaders.estratos import build_rows as build_estrato_rows
from src.etl.loaders.permanencia import (
    JOB_TYPE,
    METODOS,
    construir_permanencia,
    tabla_destino,
)
from src.shared.db.base import get_table
from src.shared.db.copy import copy_dataframe
from src.shared.db.models.silver import (
    ActividadEconomica,
    Entidad,
    EstratoPersonal,
    Municipio,
    TipoAsentamiento,
    TipoUnidadEconomica,
)
from src.shared.db.partitions import create_partition
from src.shared.enums import JobStatus, OrigenClee, TaskStatus

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
#:  nom_vial, numero_ext, nomb_asent, cod_postal, telefono, correoelec,
#:  fecha_alta_date)
#: Ids de catálogo, por nombre: los números sueltos en las tuplas de abajo
#: serían ilegibles. Salen de `data/seeds/seed_estratos_personal.parquet` y
#: de `build_rows()["denue_tipos_unidad_economica"]`.
MICRO = 2  # "0 A 5 PERSONAS", categoria Micro, empleados_min = 0
PEQUENA = 4  # "11 A 30 PERSONAS", categoria Small, empleados_min = 11
FIJO = 2  # tipo de unidad economica
COLONIA = 8  # tipo de asentamiento, igual para todo el escenario

OBSERVACIONES = [
    # id, corte, identidad, nombre, act, ent, mun, lat, lon,
    #   vialidad, numero, asentamiento, CP, telefono, correo, fecha_alta,
    #   per_ocu_id, tipounieco_id
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
        date(2010, 7, 1),
        MICRO,
        FIJO,
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
        date(2010, 7, 1),
        MICRO,
        FIJO,
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
        date(2010, 7, 1),
        MICRO,
        FIJO,
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
        date(2010, 7, 1),
        MICRO,
        FIJO,
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
        None,
        MICRO,
        FIJO,
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
        None,
        MICRO,
        FIJO,
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
        None,
        MICRO,
        FIJO,
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
        None,
        MICRO,
        FIJO,
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
        None,
        MICRO,
        FIJO,
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
        None,
        MICRO,
        FIJO,
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
        None,
        MICRO,
        FIJO,
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
        None,
        MICRO,
        FIJO,
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
        None,
        MICRO,
        FIJO,
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
        None,
        MICRO,
        FIJO,
    ),
    # Crece de Micro a Small entre el primer y el ultimo corte.
    (
        "g1",
        CORTES[0],
        "C_CRECIO",
        "TALLER PEPE",
        "811111",
        "14",
        "039",
        20.66,
        -103.34,
        "",
        "",
        "",
        "",
        "",
        "",
        None,
        MICRO,
        FIJO,
    ),
    (
        "g2",
        CORTES[3],
        "C_CRECIO",
        "TALLER PEPE",
        "811111",
        "14",
        "039",
        20.66,
        -103.34,
        "",
        "",
        "",
        "",
        "",
        "",
        None,
        PEQUENA,
        FIJO,
    ),
]


CODIGOS_ACT: list[str] = sorted({o[4] for o in OBSERVACIONES})
"""Los SCIAN que el escenario usa, para sembrar su catálogo.

Se DERIVA del escenario en vez de listarse a mano: agregar una observación
con una actividad nueva siembra su fila sola, en vez de fallar con una
violación de llave foránea a trescientas líneas de distancia.
"""

GEOGRAFIA: list[tuple[str, str]] = sorted({(o[5], o[6]) for o in OBSERVACIONES})
"""Los pares (entidad, municipio) del escenario, por el mismo motivo.

`C_MUDO` cambia de municipio, así que son dos, y las dos FKs --la de
entidad y la compuesta de municipio-- exigen ambos catálogos.
"""


@pytest.fixture(params=sorted(METODOS), ids=sorted(METODOS))
def metodo(request):
    """Cada método: el escenario y las aserciones son los mismos."""
    return request.param


@pytest.fixture
def db(silver_engine, metodo):
    """Hechos e identidad cargados, para el método bajo prueba.

    Los catálogos se siembran primero porque `establecimientos` tiene FK
    hacia ellos y el `COPY` las hace valer: sin esto el escenario no entra.
    Se siembra lo que el escenario usa y nada más, igual que
    `test_ingest.py` --el catálogo SCIAN real son 1,127 filas y aquí hacen
    falta seis.

    Dos FKs no se siembran a propósito: `cve_loc` y `tipo_vial_id` quedan
    nulos en el escenario, y una FK compuesta con cualquier columna nula se
    satisface sola (MATCH SIMPLE).
    """
    identidad = METODOS[metodo]
    with Session(silver_engine) as sesion:
        sesion.add_all(
            [EstratoPersonal(**r) for r in build_estrato_rows()]
            + [TipoUnidadEconomica(**r) for r in build_rows()["denue_tipos_unidad_economica"]]
            + [TipoAsentamiento(**r) for r in build_rows()["denue_tipos_asentamiento"]]
            # Jerarquia derivada del prefijo, como `_with_scian_hierarchy`.
            + [
                ActividadEconomica(
                    codigo_act=c,
                    nombre_act=f"actividad {c}",
                    sector=c[:2],
                    subsector=c[:3],
                    rama=c[:4],
                )
                for c in CODIGOS_ACT
            ]
            + [
                Entidad(cve_ent=e, entidad=f"entidad {e}")
                for e in sorted({e for e, _ in GEOGRAFIA})
            ]
            + [Municipio(cve_ent=e, cve_mun=m, municipio=f"municipio {e}{m}") for e, m in GEOGRAFIA]
        )
        sesion.commit()

    with silver_engine.begin() as conn:
        for corte in CORTES:
            create_partition(conn, corte)
            create_partition(conn, corte, table=identidad)

        copy_dataframe(
            conn,
            pl.DataFrame(
                # Todo menos `identidad` (indice 2), que va en la otra tabla.
                # El asentamiento es constante: solo se prueba que la tabla y
                # la vista lo resuelvan a su etiqueta, no que varie.
                [(o[0], o[1], *o[3:], COLONIA) for o in OBSERVACIONES],
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
                    "fecha_alta_date": pl.Date,
                    "per_ocu_id": pl.Int64,
                    "tipounieco_id": pl.Int64,
                    "tipo_asent_id": pl.Int64,
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

    # El `yield` va FUERA del `with`: al cerrarse, la carga queda commiteada.
    # El loader abre sus propias conexiones --una por corte y por bloque--
    # asi que no veria datos sin commitear.
    yield silver_engine


def permanencia(db, metodo: str) -> dict[str, dict]:
    """La tabla construida, indexada por identidad."""
    tabla = get_table(tabla_destino(metodo))
    with db.connect() as conn:
        filas = conn.execute(select(tabla)).mappings()
        return {f["clee_resuelto"]: dict(f) for f in filas}


@pytest.mark.unit
class TestColapso:
    def test_una_fila_por_identidad(self, db, metodo, tracker):
        """Dieciséis observaciones de siete establecimientos -> siete filas."""
        assert construir_permanencia(db, metodo, tracker) == 7
        assert set(permanencia(db, metodo)) == {
            "C_VIVE",
            "C_HUECO",
            "C_CERRO",
            "C_MUDO",
            "C_JITTER",
            "C_CRECIO",
            "NEW_REG_1234-abcdef",
        }

    def test_reconstruir_no_duplica(self, db, metodo, tracker):
        """Es reconstrucción total: los NEW_* no son estables entre corridas."""
        construir_permanencia(db, metodo, tracker)
        assert construir_permanencia(db, metodo, tracker, force=True) == 7

    def test_el_metodo_escribe_solo_su_tabla(self, db, metodo, tracker):
        """La razón de que sea una tabla por método y no una columna."""
        otro = next(m for m in METODOS if m != metodo)
        construir_permanencia(db, metodo, tracker)
        with db.connect() as conn:
            vacia = conn.execute(
                select(func.count()).select_from(get_table(tabla_destino(otro)))
            ).scalar_one()
        assert vacia == 0


@pytest.mark.unit
class TestVentana:
    def test_primer_y_ultimo_corte(self, db, metodo, tracker):
        construir_permanencia(db, metodo, tracker)
        vive = permanencia(db, metodo)["C_VIVE"]
        assert vive["primer_periodo"] == CORTES[0]
        assert vive["ultimo_periodo"] == CORTES[3]
        assert vive["cortes_observados"] == 4

    def test_los_indices_son_posiciones_en_la_secuencia(self, db, metodo, tracker):
        """Los cortes distan 4-12 meses: el tiempo se mide en periodos."""
        construir_permanencia(db, metodo, tracker)
        p = permanencia(db, metodo)
        assert (p["C_VIVE"]["idx_primer"], p["C_VIVE"]["idx_ultimo"]) == (0, 3)
        assert (p["C_CERRO"]["idx_primer"], p["C_CERRO"]["idx_ultimo"]) == (0, 1)
        assert p["NEW_REG_1234-abcdef"]["idx_primer"] == 2


@pytest.mark.unit
class TestCensura:
    def test_sin_huecos_cuando_aparece_en_todos(self, db, metodo, tracker):
        construir_permanencia(db, metodo, tracker)
        assert permanencia(db, metodo)["C_VIVE"]["huecos"] == 0

    def test_detecta_el_hueco_de_quien_desaparecio_y_volvio(self, db, metodo, tracker):
        """Es la señal de que una ausencia no implica cierre."""
        construir_permanencia(db, metodo, tracker)
        hueco = permanencia(db, metodo)["C_HUECO"]
        assert hueco["idx_primer"] == 0
        assert hueco["idx_ultimo"] == 2
        assert hueco["cortes_observados"] == 2
        assert hueco["huecos"] == 1

    def test_presente_en_el_ultimo_corte_no_tiene_cortes_despues(self, db, metodo, tracker):
        """`cortes_tras_ultimo = 0` es censura por la derecha."""
        construir_permanencia(db, metodo, tracker)
        assert permanencia(db, metodo)["C_VIVE"]["cortes_tras_ultimo"] == 0

    def test_cuenta_los_cortes_posteriores_al_ultimo(self, db, metodo, tracker):
        """Es el campo sobre el que se aplica la regla de cierre."""
        construir_permanencia(db, metodo, tracker)
        p = permanencia(db, metodo)
        assert p["C_CERRO"]["cortes_tras_ultimo"] == 2
        assert p["C_HUECO"]["cortes_tras_ultimo"] == 1

    def test_no_guarda_veredicto_de_cierre(self, db, metodo):
        """La regla es preliminar: la tabla da ingredientes, no conclusiones."""
        columnas = {c.name for c in get_table(tabla_destino(metodo)).columns}
        assert "cerrado" not in columnas
        assert {"huecos", "cortes_tras_ultimo"} <= columnas


def variantes(db, metodo: str, clee: str) -> list[dict]:
    """Los valores que tuvo una identidad, corte por corte.

    Es la consulta que sustituyó a la vista `variantes_<metodo>`: parte de
    la identidad, filtra la tabla del método por ella y regresa a los hechos
    por su llave primaria `(id, period)`. Vive aquí --y no en el código-- a
    propósito: es la referencia ejecutable de lo que `AGENTS.md` §N3
    documenta, y lo que prueba que no se perdió nada al no materializarla.
    """
    sql = text(f"""
        SELECT i.period, e.nom_estab, e.raz_social, e.codigo_act,
               e.cve_ent, e.cve_mun, e.cod_postal,
               e.nom_vial, e.numero_ext, e.nomb_asent,
               e.telefono, e.correoelec, e.www,
               ep.categoria AS categoria_tamano, ta.tipo_asent
        FROM silver.{METODOS[metodo]} i
        JOIN silver.establecimientos e ON e.id = i.id AND e.period = i.period
        LEFT JOIN silver.denue_estratos_personal ep ON ep.id = e.per_ocu_id
        LEFT JOIN silver.denue_tipos_asentamiento ta ON ta.id = e.tipo_asent_id
        WHERE i.clee_resuelto = :c
        ORDER BY i.period
    """)
    with db.connect() as conn:
        return [dict(f) for f in conn.execute(sql, {"c": clee}).mappings()]


def estatus(db, metodo: str, cortes_para_cierre: int = 2) -> dict[str, dict]:
    """La regla de cierre aplicada sobre la tabla, en la consulta.

    El umbral es un parámetro de la consulta y no una constante del
    esquema: el protocolo llama preliminar a la regla, así que moverlo no
    debe costar ni una columna ni un `CREATE OR REPLACE`.
    """
    sql = text(f"""
        SELECT clee_resuelto,
               CASE
                   WHEN cortes_tras_ultimo = 0 THEN 'ACTIVO'
                   WHEN cortes_tras_ultimo >= :umbral THEN 'CERRADO'
                   ELSE 'INCIERTO'
               END AS estatus,
               huecos > 0 AS reaparecio
        FROM silver.{tabla_destino(metodo)}
    """)
    with db.connect() as conn:
        filas = conn.execute(sql, {"umbral": cortes_para_cierre}).mappings()
        return {f["clee_resuelto"]: dict(f) for f in filas}


@pytest.mark.unit
class TestResumenDeVariantes:
    """La tabla guarda CUÁNTAS variantes hubo, no cuáles."""

    def test_cuenta_los_nombres_distintos(self, db, metodo, tracker):
        construir_permanencia(db, metodo, tracker)
        assert permanencia(db, metodo)["C_HUECO"]["n_nombres"] == 2

    def test_un_solo_nombre_no_es_variante(self, db, metodo, tracker):
        construir_permanencia(db, metodo, tracker)
        assert permanencia(db, metodo)["C_VIVE"]["n_nombres"] == 1

    def test_marca_el_cambio_de_municipio_y_actividad(self, db, metodo, tracker):
        """Las dos juntas son la firma de una fusión indebida."""
        construir_permanencia(db, metodo, tracker)
        mudo = permanencia(db, metodo)["C_MUDO"]
        assert mudo["cambio_municipio"] is True
        assert mudo["cambio_actividad"] is True

    def test_cuenta_los_domicilios_distintos(self, db, metodo, tracker):
        construir_permanencia(db, metodo, tracker)
        assert permanencia(db, metodo)["C_HUECO"]["n_direcciones"] == 2

    def test_el_contacto_es_la_union_no_el_mas_reciente(self, db, metodo, tracker):
        """Para verificar un cierre sirve cualquier teléfono que publicó.

        `C_HUECO` trae teléfono solo en el primer corte; si la bandera se
        tomara del representante quedaría en falso.
        """
        construir_permanencia(db, metodo, tracker)
        hueco = permanencia(db, metodo)["C_HUECO"]
        assert hueco["tiene_telefono"] is True
        assert hueco["tiene_correo"] is True

    def test_sin_contacto_la_bandera_es_falsa_no_nula(self, db, metodo, tracker):
        """Una cadena vacía no es un contacto, es un dato ausente."""
        construir_permanencia(db, metodo, tracker)
        assert permanencia(db, metodo)["C_VIVE"]["tiene_telefono"] is False

    def test_no_guarda_los_valores_solo_los_conteos(self, db, metodo):
        """Materializarlos duplicaría lo que el join ya entrega."""
        columnas = {c.name for c in get_table(tabla_destino(metodo)).columns}
        assert not columnas & {
            "nombres",
            "razones_sociales",
            "codigos_act",
            "municipios",
            "direcciones",
            "telefonos",
            "correos",
            "sitios",
            "estratos",
            "tipos_unieco",
            "tipos_asent",
            "cod_postales",
        }

    def test_no_marca_cambio_cuando_no_se_movio(self, db, metodo, tracker):
        construir_permanencia(db, metodo, tracker)
        vive = permanencia(db, metodo)["C_VIVE"]
        assert vive["cambio_municipio"] is False
        assert vive["cambio_actividad"] is False

    def test_el_neto_detecta_la_mudanza_real(self, db, metodo, tracker):
        """Guadalajara a CDMX son ~460 km entre dos puntos REALES."""
        construir_permanencia(db, metodo, tracker)
        p = permanencia(db, metodo)
        assert p["C_MUDO"]["desplazamiento_neto_m"] > 400_000
        assert p["C_VIVE"]["desplazamiento_neto_m"] == pytest.approx(0, abs=1)

    def test_el_neto_distingue_jitter_de_mudanza(self, db, metodo, tracker):
        """El par neto/dispersión es lo que el rectángulo solo no puede.

        `C_JITTER` oscila ~80 m y vuelve al origen: neto ~0 pero dispersión
        grande. Con solo la diagonal del rectángulo se leería como si se
        hubiera movido.
        """
        construir_permanencia(db, metodo, tracker)
        jitter = permanencia(db, metodo)["C_JITTER"]
        assert jitter["desplazamiento_neto_m"] == pytest.approx(0, abs=1)
        assert jitter["dispersion_max_m"] > 50
        assert jitter["n_posiciones"] == 2

    def test_la_dispersion_expone_el_rectangulo(self, db, metodo, tracker):
        """Las cuatro esquinas quedan accesibles, no solo su diagonal."""
        construir_permanencia(db, metodo, tracker)
        mudo = permanencia(db, metodo)["C_MUDO"]
        assert mudo["lat_min"] == pytest.approx(19.43)
        assert mudo["lat_max"] == pytest.approx(20.65)
        assert mudo["dispersion_max_m"] > 400_000

    def test_no_guarda_coordenadas_de_un_corte(self, db, metodo, tracker):
        """Ni la primera ni la última: las dos salen del join.

        Lo que persiste son las medidas sobre toda la vida, que son las que
        no se pueden recomputar sin barrer los hechos.
        """
        columnas = {c.name for c in get_table(tabla_destino(metodo)).columns}
        assert not columnas & {"latitud", "longitud", "lat_primera", "lon_primera"}
        assert {"lat_min", "lat_max", "desplazamiento_neto_m", "dispersion_max_m"} <= columnas


@pytest.mark.unit
class TestSinValoresDeUnCorte:
    """La tabla no congela el valor de ningún campo oficial.

    Guardar "el valor del corte más reciente" costaba ancho en 10M filas,
    congelaba un instante de algo que cambia entre cortes, y de todos modos
    no contestaba "¿qué valores tuvo?" — que es lo que el join sí contesta,
    y para todos los cortes. `TestValoresRecuperables` es la mitad que lo
    prueba.
    """

    def test_ninguna_columna_de_valor_oficial(self, db, metodo):
        columnas = {c.name for c in get_table(tabla_destino(metodo)).columns}
        assert not columnas & {
            "nom_estab",
            "raz_social",
            "codigo_act",
            "cve_ent",
            "cve_mun",
            "cod_postal",
            "tipo_asent",
            "cve_loc",
            "ageb",
            "per_ocu",
            "categoria_tamano",
            "empleados_min",
            "empleados_max",
            "tipounieco",
            "h3_cell_9",
            "h3_cell_5",
        }

    def test_una_bandera_de_cobertura_por_campo(self, db, metodo, tracker):
        """True si el campo vino lleno en al menos un corte."""
        construir_permanencia(db, metodo, tracker)
        vive = permanencia(db, metodo)["C_VIVE"]
        assert vive["tiene_nombre"] is True
        assert vive["tiene_actividad"] is True
        assert vive["tiene_municipio"] is True
        assert vive["tiene_coordenadas"] is True
        assert vive["tiene_estrato_personal"] is True
        assert vive["tiene_tipounieco"] is True
        assert vive["tiene_tipo_asent"] is True
        assert vive["tiene_cod_postal"] is True

    def test_un_campo_nunca_lleno_queda_en_falso(self, db, metodo, tracker):
        """Es el dato que importa: de ese negocio no se puede afirmar nada.

        El escenario no pone razón social a nadie, y `C_CERRO` tampoco trae
        código postal en ninguno de sus dos cortes.
        """
        construir_permanencia(db, metodo, tracker)
        p = permanencia(db, metodo)
        assert p["C_VIVE"]["tiene_razon_social"] is False
        assert p["C_CERRO"]["tiene_cod_postal"] is False

    def test_la_bandera_es_union_no_del_ultimo_corte(self, db, metodo, tracker):
        """`C_HUECO` trae CP solo en sus dos cortes, con valores distintos.

        Lo que la bandera afirma es que el campo se llenó alguna vez, no que
        siga lleno: para verificar un cierre sirve cualquier dato publicado.
        """
        construir_permanencia(db, metodo, tracker)
        assert permanencia(db, metodo)["C_HUECO"]["tiene_cod_postal"] is True

    def test_sin_coordenadas_las_medidas_geograficas_son_nulas(self, db, metodo, tracker):
        """`tiene_coordenadas` en False explica por qué la fila viene vacía ahí."""
        with db.begin() as conn:
            conn.execute(text("UPDATE silver.establecimientos SET latitud = NULL, longitud = NULL"))
        construir_permanencia(db, metodo, tracker)
        vive = permanencia(db, metodo)["C_VIVE"]
        assert vive["tiene_coordenadas"] is False
        assert vive["desplazamiento_neto_m"] is None
        assert vive["dispersion_max_m"] is None
        assert vive["n_posiciones"] == 0

    def test_marca_las_identidades_sinteticas(self, db, metodo, tracker):
        construir_permanencia(db, metodo, tracker)
        p = permanencia(db, metodo)
        assert p["NEW_REG_1234-abcdef"]["es_sintetica"] is True
        assert p["C_VIVE"]["es_sintetica"] is False


@pytest.mark.unit
class TestAntiguedadYCensuraIzquierda:
    """Lo que el protocolo exige para tratar la censura por la izquierda."""

    def test_duracion_es_el_span_no_los_observados(self, db, metodo, tracker):
        """Difieren cuando hubo huecos, y la duración es la variable de tiempo."""
        construir_permanencia(db, metodo, tracker)
        hueco = permanencia(db, metodo)["C_HUECO"]
        assert hueco["duracion_periodos"] == 2  # del corte 0 al 2
        assert hueco["cortes_observados"] == 2  # pero solo se vio dos veces

    def test_marca_a_los_presentes_en_la_primera_edicion(self, db, metodo, tracker):
        """Su antigüedad real es DESCONOCIDA: el protocolo exige el indicador."""
        construir_permanencia(db, metodo, tracker)
        p = permanencia(db, metodo)
        assert p["C_VIVE"]["presente_desde_el_inicio"] is True
        assert p["NEW_REG_1234-abcdef"]["presente_desde_el_inicio"] is False

    def test_dias_entre_el_alta_y_la_primera_aparicion(self, db, metodo, tracker):
        """Diagnóstico de la fecha de alta, que es de registro y no de operación."""
        construir_permanencia(db, metodo, tracker)
        vive = permanencia(db, metodo)["C_VIVE"]
        # alta 2010-07-01, primera aparición 2019-11-01
        assert vive["dias_alta_a_primera"] > 3000

    def test_sin_fecha_de_alta_el_diagnostico_queda_nulo(self, db, metodo, tracker):
        construir_permanencia(db, metodo, tracker)
        assert permanencia(db, metodo)["C_HUECO"]["dias_alta_a_primera"] is None


@pytest.mark.unit
class TestTamanoYTipo:
    """Del tamaño se materializa la TRAYECTORIA, no el nivel.

    El nivel (en qué estrato está, cuántos empleados) es un valor de un
    corte: sale del join. Lo que no se puede recomputar sin barrer los
    hechos es cómo se movió entre el primer corte y el último.
    """

    def test_el_delta_distingue_crecer_de_encogerse(self, db, metodo, tracker):
        """Un conteo de estratos distintos no tiene orden; el delta sí.

        `C_CRECIO` pasa de 0-5 a 11-30 empleados: delta positivo.
        """
        construir_permanencia(db, metodo, tracker)
        p = permanencia(db, metodo)
        assert p["C_CRECIO"]["cambio_tamano"] is True
        assert p["C_CRECIO"]["delta_empleados_min"] == 11  # 11 - 0
        assert p["C_VIVE"]["delta_empleados_min"] == 0

    def test_el_nivel_se_recupera_del_join(self, db, metodo, tracker):
        """La categoría MIPYME es el sujeto de la tesis y sigue alcanzable.

        Y con más información que antes: el estrato de CADA corte, no el del
        último. `C_CRECIO` lo demuestra — pasó de Micro a Small.
        """
        construir_permanencia(db, metodo, tracker)
        crecio = variantes(db, metodo, "C_CRECIO")
        assert [v["categoria_tamano"] for v in crecio] == ["Micro", "Small"]


@pytest.mark.unit
class TestPatronDePresencia:
    def test_guarda_los_indices_exactos(self, db, metodo, tracker):
        """De aquí se deriva cualquier patrón de ausencia sin volver a los hechos."""
        construir_permanencia(db, metodo, tracker)
        p = permanencia(db, metodo)
        assert p["C_VIVE"]["cortes_idx"] == [0, 1, 2, 3]
        assert p["C_HUECO"]["cortes_idx"] == [0, 2]  # el hueco es visible

    def test_mide_cuanto_de_la_identidad_es_observado(self, db, metodo, tracker):
        """10 cortes DIRECTO son más confiables que 1 directo y 9 propagados."""
        construir_permanencia(db, metodo, tracker)
        p = permanencia(db, metodo)
        # el escenario los marca todos PROPAGADO salvo los sintéticos
        assert p["C_VIVE"]["cortes_propagado"] == 4
        assert p["C_VIVE"]["cortes_directo"] == 0


@pytest.mark.unit
class TestReglaDeCierre:
    """La regla se aplica en la consulta: la tabla da los ingredientes."""

    def test_presente_en_el_ultimo_corte_es_activo(self, db, metodo, tracker):
        """ACTIVO no es "sigue abierto": es censura por la derecha."""
        construir_permanencia(db, metodo, tracker)
        assert estatus(db, metodo)["C_VIVE"]["estatus"] == "ACTIVO"

    def test_dos_ausencias_son_cierre(self, db, metodo, tracker):
        construir_permanencia(db, metodo, tracker)
        assert estatus(db, metodo)["C_CERRO"]["estatus"] == "CERRADO"

    def test_una_sola_ausencia_es_incierto(self, db, metodo, tracker):
        """Una ausencia no distingue un cierre de una omisión del directorio."""
        construir_permanencia(db, metodo, tracker)
        assert estatus(db, metodo)["C_HUECO"]["estatus"] == "INCIERTO"

    def test_mover_el_umbral_no_toca_la_tabla(self, db, metodo, tracker):
        """El protocolo llama preliminar a la regla: cambiarla es gratis.

        Con umbral 1, la ausencia única de `C_HUECO` ya cuenta como cierre
        sin reconstruir una sola fila.
        """
        construir_permanencia(db, metodo, tracker)
        assert estatus(db, metodo, cortes_para_cierre=1)["C_HUECO"]["estatus"] == "CERRADO"

    def test_marca_a_quien_ya_reaparecio(self, db, metodo, tracker):
        """Demostró, para sí mismo, que una ausencia no implica cierre."""
        construir_permanencia(db, metodo, tracker)
        e = estatus(db, metodo)
        assert e["C_HUECO"]["reaparecio"] is True
        assert e["C_VIVE"]["reaparecio"] is False

    def test_el_estatus_no_es_una_columna_de_la_tabla(self, db, metodo):
        """Si alguien lo materializa, cambiar la regla cuesta 10M de filas."""
        columnas = {c.name for c in get_table(tabla_destino(metodo)).columns}
        assert "estatus" not in columnas
        assert "cerrado" not in columnas


@pytest.mark.unit
class TestValoresRecuperables:
    """Lo que la tabla no materializa, el join a los hechos lo recupera.

    Es la mitad que justifica no guardar ningún valor de un corte: si el
    join no devolviera los valores, la información se habría perdido de
    verdad. Y devuelve MÁS que antes — todos los cortes, no solo el último.
    """

    def test_el_camino_arranca_en_permanencia_sin_tabla_puente(self, db, metodo, tracker):
        """`clee_<metodo>` ES la tabla puente: no hace falta otra.

        La relación es many:1 (N observaciones -> 1 fila de permanencia), y
        es la que convierte una fila de permanencia en la historia completa
        del establecimiento.
        """
        construir_permanencia(db, metodo, tracker)
        sql = text(f"""
            SELECT i.period, e.nom_estab
            FROM silver.{tabla_destino(metodo)} p
            JOIN silver.{METODOS[metodo]} i ON i.clee_resuelto = p.clee_resuelto
            JOIN silver.establecimientos e ON e.id = i.id AND e.period = i.period
            WHERE p.clee_resuelto = 'C_HUECO'
            ORDER BY i.period
        """)
        with db.connect() as conn:
            filas = [dict(f) for f in conn.execute(sql).mappings()]
        assert [f["nom_estab"] for f in filas] == ["TACOS DON JUAN", "TAQUERIA DON JUAN"]

    def test_devuelve_los_nombres_que_tuvo(self, db, metodo):
        nombres = {v["nom_estab"] for v in variantes(db, metodo, "C_HUECO")}
        assert nombres == {"TACOS DON JUAN", "TAQUERIA DON JUAN"}

    def test_devuelve_los_municipios_que_tuvo(self, db, metodo):
        mudo = variantes(db, metodo, "C_MUDO")
        assert {v["cve_ent"] + v["cve_mun"] for v in mudo} == {"09015", "14039"}

    def test_devuelve_el_domicilio_por_componentes_y_por_corte(self, db, metodo):
        """Una fila por corte: qué número va con qué calle no se pierde."""
        hueco = variantes(db, metodo, "C_HUECO")
        domicilios = {(v["nom_vial"], v["numero_ext"], v["cod_postal"]) for v in hueco}
        assert domicilios == {
            ("AV JUAREZ", "123", "44100"),
            ("AV JUAREZ", "125", "44200"),
        }

    def test_devuelve_el_contacto_de_cualquier_corte(self, db, metodo):
        """El teléfono de 2019 sigue sirviendo para verificar un cierre."""
        telefonos = {v["telefono"] for v in variantes(db, metodo, "C_HUECO")}
        assert "3312345678" in telefonos

    def test_una_fila_por_corte_observado(self, db, metodo, tracker):
        """El grano del join es la observación, no el establecimiento.

        Y coincide con el conteo que la tabla guarda: `cortes_observados`
        es el número de filas que el join devuelve.
        """
        construir_permanencia(db, metodo, tracker)
        observados = permanencia(db, metodo)["C_HUECO"]["cortes_observados"]
        assert len(variantes(db, metodo, "C_HUECO")) == observados

    def test_los_catalogos_se_resuelven_a_sus_etiquetas(self, db, metodo):
        """Los catálogos están a un LEFT JOIN por id, no a una reconstrucción."""
        vive = variantes(db, metodo, "C_VIVE")[0]
        assert vive["categoria_tamano"] == "Micro"
        assert vive["tipo_asent"] == "COLONIA"

    def test_sin_identidad_no_hay_valores_que_recuperar(self, db, metodo):
        """El join parte de la tabla del método: sin CLEE no hay fila."""
        with db.begin() as conn:
            conn.execute(text(f"DELETE FROM silver.{METODOS[metodo]}"))
        assert variantes(db, metodo, "C_VIVE") == []


@pytest.mark.unit
class TestSinIdentidad:
    def test_sin_filas_de_identidad_la_tabla_queda_vacia(self, db, metodo, tracker):
        """Correr permanencia antes de la resolución no inventa nada."""
        with db.begin() as conn:
            conn.execute(text(f"DELETE FROM silver.{METODOS[metodo]}"))
        assert construir_permanencia(db, metodo, tracker) == 0

    def test_metodo_desconocido_falla_explicito(self, db, tracker):
        """Antes de abrir corrida en `meta`: no deja basura por un typo."""
        with pytest.raises(ValueError, match="Metodo desconocido"):
            construir_permanencia(db, "inexistente", tracker)
        assert tracker.get_recent_runs(limit=1) == []


@pytest.mark.unit
class TestRegistroEnMeta:
    """Cada construcción deja su corrida y su tarea en `meta`.

    Es lo que hace consultable con SQL --sin abrir
    `data/etl_permanencia.log`-- cuánto tardó la última corrida, cuántas
    filas escribió y si terminó bien, igual que el ingest y la resolución
    CLEE.
    """

    def tarea(self, db, metodo: str) -> dict:
        """La última tarea de `meta.process_tasks` para la tabla del método.

        Se lee con SQL y no con el tracker porque `get_tasks_by_run` solo
        devuelve descargas, y lo que aquí importa son las tareas de proceso.
        """
        with db.connect() as conn:
            fila = (
                conn.execute(
                    text("""
                    SELECT * FROM meta.process_tasks
                    WHERE target_table = :tabla
                    ORDER BY created_at DESC
                    LIMIT 1
                    """),
                    {"tabla": tabla_destino(metodo)},
                )
                .mappings()
                .first()
            )
        assert fila is not None, f"sin tarea en meta para {tabla_destino(metodo)}"
        return dict(fila)

    def test_la_corrida_queda_en_job_runs(self, db, metodo, tracker):
        """Con su propio `job_type`: `/ops/health/pipeline` reporta por tipo."""
        construir_permanencia(db, metodo, tracker)
        run = tracker.get_recent_runs(limit=1)[0]
        assert run["job_type"] == JOB_TYPE
        assert run["job_name"] == f"denue_permanencia_{metodo}"
        assert run["status"] == JobStatus.SUCCESS.value
        assert run["duration_seconds"] is not None

    def test_la_tarea_guarda_filas_leidas_y_escritas(self, db, metodo, tracker):
        """16 observaciones leídas -> 7 establecimientos escritos."""
        construir_permanencia(db, metodo, tracker)
        tarea = self.tarea(db, metodo)
        assert tarea["status"] == TaskStatus.SUCCESS.value
        assert tarea["target_table"] == tabla_destino(metodo)
        assert tarea["rows_extracted"] == len(OBSERVACIONES)
        assert tarea["rows_loaded"] == 7

    def test_el_origen_son_las_dos_tablas_de_silver(self, db, metodo, tracker):
        """No hay archivo que registrar: el linaje son los hechos y la identidad."""
        construir_permanencia(db, metodo, tracker)
        origen = self.tarea(db, metodo)["source_path"]
        assert "silver.establecimientos" in origen
        assert f"silver.{METODOS[metodo]}" in origen

    def test_la_segunda_corrida_se_salta(self, db, metodo, tracker):
        """Sin esto, volver a pedirla recarga los 20 cortes para nada.

        La tabla de trabajo se borra al terminar, así que una segunda
        corrida la rellenaría entera --horas-- solo para descubrir al final
        que todos los bloques ya estaban escritos.
        """
        construir_permanencia(db, metodo, tracker)
        assert construir_permanencia(db, metodo, tracker) == 7

        tarea = self.tarea(db, metodo)
        assert tarea["status"] == TaskStatus.SKIPPED.value
        assert tarea["rows_extracted"] is None  # no leyó ni una observación
        assert tarea["rows_loaded"] == 7

    def test_una_tabla_vacia_se_construye_aunque_meta_diga_exito(self, db, metodo, tracker):
        """Es el caso de una migración que recrea la tabla.

        `b0007` tira y recrea `permanencia_*`, pero el `SUCCESS` de la
        construcción anterior sigue en `meta.process_tasks`. Sin esta
        guarda, "ya construida" salía verdadero sobre una tabla vacía y la
        corrida se saltaba el trabajo reportando 0 filas.
        """
        construir_permanencia(db, metodo, tracker)
        with db.begin() as conn:
            conn.execute(text(f"TRUNCATE silver.{tabla_destino(metodo)}"))

        # Sin --force: no hay avance que descartar, solo una tabla vacía.
        assert construir_permanencia(db, metodo, tracker) == 7
        assert self.tarea(db, metodo)["status"] == TaskStatus.SUCCESS.value

    def test_si_la_identidad_se_re_resolvio_reconstruye(self, db, metodo, tracker):
        """La tabla vigente quedó stale: sus agregados son de otras identidades.

        El reloj es `end_time` de la última tarea exitosa contra
        `max(resuelto_en)`. Sin esto, el pipeline semanal serviría la
        permanencia del corte anterior hasta que alguien se acordara de
        `--force`.
        """
        construir_permanencia(db, metodo, tracker)
        with db.begin() as conn:
            conn.execute(text(f"UPDATE silver.{METODOS[metodo]} SET resuelto_en = now()"))

        assert construir_permanencia(db, metodo, tracker) == 7
        tarea = self.tarea(db, metodo)
        assert tarea["status"] == TaskStatus.SUCCESS.value
        assert tarea["rows_extracted"] == len(OBSERVACIONES)  # releyó los cortes

    def test_un_fallo_queda_registrado_y_se_propaga(self, db, metodo, tracker, monkeypatch):
        """La corrida fallida es justo la que se quiere poder consultar."""
        monkeypatch.setattr(loader, "SET_WORK_MEM", "SET LOCAL work_mem = 'no_es_memoria'")

        with pytest.raises(Exception, match="work_mem"):
            construir_permanencia(db, metodo, tracker)

        run = tracker.get_recent_runs(limit=1)[0]
        assert run["status"] == JobStatus.FAILED.value
        tarea = self.tarea(db, metodo)
        assert tarea["status"] == TaskStatus.FAILED.value
        assert "work_mem" in tarea["error_message"]
