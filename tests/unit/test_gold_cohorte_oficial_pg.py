"""La cohorte oficial contra PostgreSQL de verdad.

Esto es lo que `test_gold_cohorte_oficial.py` no puede hacer: ese revisa el
SQL que se genera, y este lo **ejecuta**. Dos clases de error solo salen
aquí — uno de sintaxis o de tipos, y uno de lógica donde el SQL es válido
pero la respuesta está mal.

El escenario está armado para que cada regla tenga un caso que la obligue:

    oficial_vive   en los 3 cortes oficiales Y en uno anterior. Si el loader
                   contara el corte viejo, `cortes_observados` daría 4 y los
                   `idx` saldrían de la secuencia completa
    rebrand        dos nombres, uno en 2 cortes y otro en 1 -> la moda tiene
                   que elegir el frecuente, no el ultimo
    sospechoso     4 nombres en 3 cortes, que es IMPOSIBLE -> sin nombre
                   canonico y marcado
    misma_rama     dos SCIAN que comparten los 5 primeros digitos -> el
                   ancestro comun es determinista
    cruza_rama     dos SCIAN de ramas distintas -> NULL, no un valor
                   inventado
    solo_viejo     solo en el corte sin CLEE -> no entra a la cohorte
    sin_clee       una observacion con clee vacio en un corte oficial -> no
                   contribuye
    jitter         el mismo local oscilando ~89 m y volviendo al punto de
                   partida: neto ~0 con dispersion mayor. Con los umbrales de
                   default la regla NO dispara, y eso es lo que se afirma
"""

import json
from datetime import date

import polars as pl
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from src.etl.loaders.dominios import build_rows
from src.etl.loaders.estratos import build_rows as build_estrato_rows
from src.etl.loaders.gold_cohorte_oficial import (
    TABLA,
    TABLA_VARIANTES,
    CohorteOficial,
    construir_cohorte_oficial,
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
from src.shared.enums import JobStatus, TaskStatus

pytestmark = pytest.mark.postgres

VIEJO = date(2020, 11, 1)
"""Corte SIN CLEE: existe para probar que no entra a la ventana."""

OFICIALES = [date(2021, 5, 1), date(2021, 11, 1), date(2022, 5, 1)]
"""Los tres cortes con CLEE al 100%. Son la ventana que el loader debe medir."""

CORTES = [VIEJO, *OFICIALES]

MICRO = 2
PEQUENA = 4
FIJO = 2
COLONIA = 8

#: (id, corte, clee, nombre, codigo_act, cve_ent, cve_mun, lat, lon,
#:  telefono, correoelec, per_ocu_id)
OBSERVACIONES = [
    # --- oficial_vive: tambien esta en el corte viejo, CON clee, y aun asi
    #     ese corte no debe contar porque el corte no es oficial ---
    ("v0", VIEJO, "C_VIVE", "PANADERIA SOL", "311811", "14", "039", 20.67, -103.35, "", "", MICRO),
    # --- relleno sin clee que baja la cobertura del corte viejo por debajo
    #     del umbral: es lo que lo vuelve NO oficial ---
    ("p0", VIEJO, "", "SIN IDENTIDAD", "311811", "14", "039", 20.4, -103.6, "", "", MICRO),
    (
        "v1",
        OFICIALES[0],
        "C_VIVE",
        "PANADERIA SOL",
        "311811",
        "14",
        "039",
        20.67,
        -103.35,
        "3312345678",
        "",
        MICRO,
    ),
    (
        "v2",
        OFICIALES[1],
        "C_VIVE",
        "PANADERIA SOL",
        "311811",
        "14",
        "039",
        20.67,
        -103.35,
        "",
        "sol@x.mx",
        MICRO,
    ),
    (
        "v3",
        OFICIALES[2],
        "C_VIVE",
        "PANADERIA SOL",
        "311811",
        "14",
        "039",
        20.67,
        -103.35,
        "",
        "",
        PEQUENA,
    ),
    # --- rebrand: TACOS PACO x2, TAQUERIA DON PACO x1 ---
    (
        "r1",
        OFICIALES[0],
        "C_REBRAND",
        "TACOS PACO",
        "722513",
        "14",
        "039",
        20.60,
        -103.40,
        "",
        "",
        MICRO,
    ),
    (
        "r2",
        OFICIALES[1],
        "C_REBRAND",
        "TACOS PACO",
        "722513",
        "14",
        "039",
        20.60,
        -103.40,
        "",
        "",
        MICRO,
    ),
    (
        "r3",
        OFICIALES[2],
        "C_REBRAND",
        "TAQUERIA DON PACO",
        "722513",
        "14",
        "039",
        20.60,
        -103.40,
        "",
        "",
        MICRO,
    ),
    # --- sospechoso: 4 nombres en 3 cortes (dos en el mismo corte) ---
    ("s1", OFICIALES[0], "C_SOSP", "TACOS A", "722513", "14", "039", 20.5, -103.5, "", "", MICRO),
    (
        "s2",
        OFICIALES[0],
        "C_SOSP",
        "FARMACIA B",
        "722513",
        "14",
        "039",
        20.5,
        -103.5,
        "",
        "",
        MICRO,
    ),
    (
        "s3",
        OFICIALES[1],
        "C_SOSP",
        "FERRETERIA C",
        "722513",
        "14",
        "039",
        20.5,
        -103.5,
        "",
        "",
        MICRO,
    ),
    ("s4", OFICIALES[2], "C_SOSP", "TALLER D", "722513", "14", "039", 20.5, -103.5, "", "", MICRO),
    # --- misma_rama: 311811 y 311812 comparten los 5 primeros digitos ---
    ("m1", OFICIALES[0], "C_RAMA", "PAN UNO", "311811", "14", "039", 20.7, -103.3, "", "", MICRO),
    ("m2", OFICIALES[1], "C_RAMA", "PAN UNO", "311812", "14", "039", 20.7, -103.3, "", "", MICRO),
    # --- cruza_rama: panaderia y taqueria no comparten rama ---
    ("x1", OFICIALES[0], "C_CRUZA", "MIXTO", "311811", "14", "039", 20.8, -103.2, "", "", MICRO),
    ("x2", OFICIALES[1], "C_CRUZA", "MIXTO", "722513", "14", "039", 20.8, -103.2, "", "", MICRO),
    # --- solo_viejo: no tiene ninguna observacion oficial ---
    ("o1", VIEJO, "C_VIEJO", "EXTINTO", "311811", "14", "039", 20.9, -103.1, "", "", MICRO),
    # --- sin_clee: observacion con clee vacio dentro de un corte oficial ---
    ("n1", OFICIALES[0], "", "ANONIMO", "311811", "14", "039", 21.0, -103.0, "", "", MICRO),
    # --- jitter: oscila ~80 m y vuelve; neto ~0 con dispersion grande ---
    (
        "j1",
        OFICIALES[0],
        "C_JITTER",
        "LOCAL FIJO",
        "722513",
        "14",
        "039",
        20.6000,
        -103.3000,
        "",
        "",
        MICRO,
    ),
    (
        "j2",
        OFICIALES[1],
        "C_JITTER",
        "LOCAL FIJO",
        "722513",
        "14",
        "039",
        20.6008,
        -103.3000,
        "",
        "",
        MICRO,
    ),
    (
        "j3",
        OFICIALES[2],
        "C_JITTER",
        "LOCAL FIJO",
        "722513",
        "14",
        "039",
        20.6000,
        -103.3000,
        "",
        "",
        MICRO,
    ),
]

CODIGOS_ACT = sorted({o[4] for o in OBSERVACIONES})
GEOGRAFIA = sorted({(o[5], o[6]) for o in OBSERVACIONES})


@pytest.fixture(autouse=True)
def umbral_para_un_escenario_chico(monkeypatch):
    """Baja `GOLD_COBERTURA_CLEE_MINIMA` a 0.8 mientras corre este archivo.

    El umbral real es 99.9%, y con decenas de filas en vez de millones **una
    sola** observación sin `clee` descalificaría su corte entero. Eso haría
    irrepresentable justo el caso que la regla describe: el ≤0.1% sin `clee`
    dentro de un corte que sí es oficial.

    Lo que se prueba sigue siendo el comportamiento real --qué cortes entran y
    qué filas contribuyen--, solo con una escala donde los porcentajes son
    expresables. El umbral mismo tiene su propio test unitario.
    """
    from src.shared.config import get_settings

    monkeypatch.setenv("GOLD_COBERTURA_CLEE_MINIMA", "0.8")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def db(silver_engine):
    """Silver sembrado y `gold` recién creado desde el modelo.

    Los catálogos van primero porque `establecimientos` tiene FK hacia ellos
    y el `COPY` las hace valer. `pg_trgm` se instala porque el índice GIN de
    `valor` no se puede crear sin ella.
    """
    from src.shared.db.base import Base, tables_for

    with Session(silver_engine) as sesion:
        sesion.add_all(
            [EstratoPersonal(**r) for r in build_estrato_rows()]
            + [TipoUnidadEconomica(**r) for r in build_rows()["denue_tipos_unidad_economica"]]
            + [TipoAsentamiento(**r) for r in build_rows()["denue_tipos_asentamiento"]]
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
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        conn.execute(text("DROP SCHEMA IF EXISTS gold CASCADE"))
        conn.execute(text("CREATE SCHEMA gold"))
        for corte in CORTES:
            create_partition(conn, corte)
        copy_dataframe(
            conn,
            pl.DataFrame(
                [(*o, COLONIA, FIJO, date(2015, 1, 1)) for o in OBSERVACIONES],
                schema={
                    "id": pl.Utf8,
                    "period": pl.Date,
                    "clee": pl.Utf8,
                    "nom_estab": pl.Utf8,
                    "codigo_act": pl.Utf8,
                    "cve_ent": pl.Utf8,
                    "cve_mun": pl.Utf8,
                    "latitud": pl.Float64,
                    "longitud": pl.Float64,
                    "telefono": pl.Utf8,
                    "correoelec": pl.Utf8,
                    "per_ocu_id": pl.Int64,
                    "tipo_asent_id": pl.Int64,
                    "tipounieco_id": pl.Int64,
                    "fecha_alta_date": pl.Date,
                },
                orient="row",
            ),
            "establecimientos",
        )

    Base.metadata.create_all(silver_engine, tables=tables_for("gold"))
    yield silver_engine


def negocios(db) -> dict[str, dict]:
    """La cohorte construida, indexada por `clee`."""
    tabla = get_table(TABLA, schema="gold")
    with db.connect() as conn:
        return {f["clee"]: dict(f) for f in conn.execute(select(tabla)).mappings()}


def variantes(db, clee: str, campo: str) -> list[dict]:
    """Las variantes de un negocio en un campo, de la más usada a la menos."""
    tabla = get_table(TABLA_VARIANTES, schema="gold")
    with db.connect() as conn:
        filas = conn.execute(
            select(tabla).where(tabla.c.clee == clee, tabla.c.campo == campo)
        ).mappings()
        return sorted((dict(f) for f in filas), key=lambda f: -f["n_cortes"])


@pytest.mark.unit
class TestLaVentanaSeMide:
    def test_solo_los_cortes_con_clee_entran(self, db, tracker):
        """El corte de 2020-11 no tiene CLEE, asi que no es de la ventana."""
        construir_cohorte_oficial(db, tracker)
        ag = CohorteOficial()
        with db.connect() as conn:
            assert ag.cortes(conn) == OFICIALES

    def test_la_ventana_queda_en_cada_fila(self, db, tracker):
        construir_cohorte_oficial(db, tracker)
        assert negocios(db)["C_VIVE"]["ventana_cortes"] == OFICIALES

    def test_un_negocio_solo_pre_2021_no_entra(self, db, tracker):
        """Y C_VIEJO **si** trae clee: lo que lo excluye es el CORTE.

        Es la distincion que importa: no basta con filtrar filas sin `clee`,
        hay que filtrar cortes enteros. Si el loader solo mirara el `clee` de
        la fila, este negocio entraria con una identidad de un corte que
        INEGI no publico al 100%.
        """
        construir_cohorte_oficial(db, tracker)
        assert "C_VIEJO" not in negocios(db)

    def test_una_observacion_sin_clee_no_contribuye(self, db, tracker):
        """No tiene identidad observada, aunque este en un corte oficial."""
        construir_cohorte_oficial(db, tracker)
        assert "" not in negocios(db)
        assert "SIN IDENTIDAD" not in {f["nombre"] for f in negocios(db).values()}


@pytest.mark.unit
class TestLosIdxSeRenumeran:
    """El error que no produciria ningun mensaje: heredar los idx de Silver."""

    def test_el_corte_viejo_no_cuenta_como_observacion(self, db, tracker):
        """C_VIVE esta en 4 cortes pero solo 3 son oficiales."""
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_VIVE"]
        assert fila["cortes_observados"] == 3
        assert fila["primer_periodo"] == OFICIALES[0]

    def test_idx_primer_cero_es_el_primer_corte_OFICIAL(self, db, tracker):
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_VIVE"]
        assert fila["idx_primer"] == 0
        assert fila["idx_ultimo"] == 2
        assert fila["duracion_periodos"] == 2
        assert fila["presente_desde_el_inicio"] is True

    def test_la_censura_se_mide_en_la_ventana_oficial(self, db, tracker):
        """C_RAMA sale en el corte 1 de 3, asi que le queda uno despues."""
        construir_cohorte_oficial(db, tracker)
        assert negocios(db)["C_RAMA"]["cortes_tras_ultimo"] == 1
        assert negocios(db)["C_VIVE"]["cortes_tras_ultimo"] == 0


@pytest.mark.unit
class TestElNombreCanonico:
    def test_la_moda_gana_al_mas_reciente(self, db, tracker):
        """TACOS PACO x2 contra TAQUERIA DON PACO x1, que es el ultimo."""
        construir_cohorte_oficial(db, tracker)
        assert negocios(db)["C_REBRAND"]["nombre"] == "TACOS PACO"

    def test_las_variantes_guardan_las_dos_con_su_ventana(self, db, tracker):
        construir_cohorte_oficial(db, tracker)
        filas = variantes(db, "C_REBRAND", "nom_estab")
        assert [(f["valor"], f["n_cortes"]) for f in filas] == [
            ("TACOS PACO", 2),
            ("TAQUERIA DON PACO", 1),
        ]
        assert filas[0]["primer_periodo"] == OFICIALES[0]
        assert filas[0]["ultimo_periodo"] == OFICIALES[1]

    def test_exactamente_una_variante_es_canonica(self, db, tracker):
        construir_cohorte_oficial(db, tracker)
        filas = variantes(db, "C_REBRAND", "nom_estab")
        assert [f["es_canonico"] for f in filas] == [True, False]

    def test_un_cluster_sospechoso_no_recibe_nombre(self, db, tracker):
        """4 nombres en 3 cortes es imposible para un cluster correcto."""
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_SOSP"]
        assert fila["cluster_sospechoso"] is True
        assert fila["nombre"] is None
        assert fila["n_nombres"] == 4

    def test_pero_sus_variantes_siguen_ahi(self, db, tracker):
        """Es el mejor diagnostico de un mal cluster que existe."""
        construir_cohorte_oficial(db, tracker)
        filas = variantes(db, "C_SOSP", "nom_estab")
        assert len(filas) == 4
        # Ninguna es canonica: la madre no publico ninguna.
        assert not any(f["es_canonico"] for f in filas)

    def test_un_negocio_sano_no_queda_marcado(self, db, tracker):
        construir_cohorte_oficial(db, tracker)
        assert negocios(db)["C_VIVE"]["cluster_sospechoso"] is False


@pytest.mark.unit
class TestElScianCanonico:
    def test_misma_rama_da_el_ancestro_comun(self, db, tracker):
        """311811 y 311812 comparten los 5 primeros digitos."""
        construir_cohorte_oficial(db, tracker)
        assert negocios(db)["C_RAMA"]["codigo_act"] == "31181"

    def test_cruzar_rama_queda_en_null(self, db, tracker):
        """Ahi hay que juzgar; un valor inventado seria peor que la ausencia."""
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_CRUZA"]
        assert fila["codigo_act"] is None
        # Y se distingue de "nunca tuvo actividad":
        assert fila["tiene_actividad"] is True

    def test_una_sola_actividad_se_queda_completa(self, db, tracker):
        construir_cohorte_oficial(db, tracker)
        assert negocios(db)["C_VIVE"]["codigo_act"] == "311811"


@pytest.mark.unit
class TestLaMadreSoloGuardaAgregados:
    """La regla de `b0010`: el valor de UN corte no se materializa aqui."""

    def test_la_tabla_no_tiene_columnas_de_snapshot(self):
        """Su ausencia ES la decision, no un olvido."""
        columnas = {c.name for c in get_table(TABLA, schema="gold").columns}
        assert not columnas & {
            "razon_social",
            "per_ocu",
            "categoria_tamano",
            "empleados_min",
            "empleados_max",
            "tipounieco",
            "telefono",
            "correoelec",
            "www",
        }

    def test_pero_si_guarda_los_agregados_de_tamano(self, db, tracker):
        """C_VIVE pasa de MICRO a PEQUENA: eso SI es propiedad de la vida.

        "Cambio de tamano" y "cuanto crecio" no son el valor de un corte, son
        afirmaciones sobre la trayectoria completa.
        """
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_VIVE"]
        assert fila["cambio_tamano"] is True
        assert fila["delta_empleados_min"] == 11
        assert fila["tiene_estrato_personal"] is True

    def test_y_las_banderas_de_cobertura_del_contacto(self, db, tracker):
        """El telefono vino en el corte 1 y el correo en el 2.

        La madre no dice cuales fueron, pero si que existieron -- que es lo
        que decide si el negocio entra a un analisis que dependa de ellos.
        """
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_VIVE"]
        assert fila["tiene_telefono"] is True
        assert fila["tiene_correo"] is True
        assert fila["tiene_sitio"] is False

    def test_la_geografia_si_se_queda_con_su_fecha(self, db, tracker):
        """El unico snapshot que sobrevive, porque es la llave de los cruces.

        Y no finge ser atemporal: `corte_referencia` dice de que corte salio.
        """
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_VIVE"]
        assert fila["corte_referencia"] == OFICIALES[2]
        assert fila["cve_ent"] == "14"
        assert fila["cve_mun"] == "039"


@pytest.mark.unit
class TestLaInformacionNoSePierde:
    """Lo que salio de la madre esta en variantes, y con MAS informacion."""

    def test_el_estrato_queda_con_su_ventana(self, db, tracker):
        """Dos valores: el de los dos primeros cortes y el del ultimo.

        Eso es estrictamente mas que la columna que se quito, que solo decia
        el ultimo y sin indicar desde cuando.
        """
        construir_cohorte_oficial(db, tracker)
        filas = variantes(db, "C_VIVE", "per_ocu")
        assert len(filas) == 2

        frecuente, reciente = filas
        assert frecuente["n_cortes"] == 2
        assert frecuente["primer_periodo"] == OFICIALES[0]
        assert frecuente["ultimo_periodo"] == OFICIALES[1]
        assert reciente["n_cortes"] == 1
        assert reciente["primer_periodo"] == OFICIALES[2]

    def test_el_contacto_queda_fechado(self, db, tracker):
        """Antes era un valor sin fecha; ahora se sabe de que corte salio."""
        construir_cohorte_oficial(db, tracker)

        tel = variantes(db, "C_VIVE", "telefono")
        assert [(f["valor"], f["primer_periodo"]) for f in tel] == [("3312345678", OFICIALES[0])]
        correo = variantes(db, "C_VIVE", "correoelec")
        assert [(f["valor"], f["primer_periodo"]) for f in correo] == [("sol@x.mx", OFICIALES[1])]

    def test_ninguno_de_esos_campos_tiene_canonico(self, db, tracker):
        """La madre no publico ninguno, asi que no hay nada que marcar."""
        construir_cohorte_oficial(db, tracker)
        for campo in ("per_ocu", "telefono", "correoelec", "raz_social", "tipounieco"):
            for fila in variantes(db, "C_VIVE", campo):
                assert fila["es_canonico"] is False, f"{campo}={fila['valor']}"

    def test_la_categoria_de_tamano_se_deriva_del_catalogo(self, db, tracker):
        """Es la razon por la que no entra a variantes: seria repetirlo.

        Esta consulta es el camino de recuperacion documentado en AGENTS.md
        §N5, y probarla aqui es lo que garantiza que siga funcionando.
        """
        construir_cohorte_oficial(db, tracker)
        with db.connect() as conn:
            filas = (
                conn.execute(
                    text("""
                    SELECT va.primer_periodo, ep.categoria, ep.empleados_min
                    FROM gold.negocios_clee_oficial_variantes va
                    JOIN silver.denue_estratos_personal ep ON ep.per_ocu = va.valor
                    WHERE va.clee = 'C_VIVE' AND va.campo = 'per_ocu'
                    ORDER BY va.primer_periodo
                    """)
                )
                .mappings()
                .all()
            )
        assert [f["categoria"] for f in filas] == ["Micro", "Small"]
        assert [f["empleados_min"] for f in filas] == [0, 11]


@pytest.mark.unit
class TestJitterDeGPS:
    """La excepcion a "todo del corte de referencia", y su limite real.

    OJO: con tres cortes la regla casi no puede cambiar el resultado, y es
    aritmetica, no un defecto de esta implementacion. La regla pide neto ~0,
    o sea que el ULTIMO punto esta cerca del PRIMERO -- y entonces la mediana
    de tres valores cae practicamente encima del ultimo. El caso en que el
    ultimo punto SI es el malo produce un neto grande, asi que la regla no
    dispara. Esta anotado como hallazgo sobre `normalizacion_gold.md` §4.
    """

    def test_con_los_umbrales_de_default_el_ultimo_punto_se_queda(self, db, tracker):
        """89 m de oscilacion es ruido tolerable bajo el umbral de 200 m."""
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_JITTER"]
        assert fila["desplazamiento_neto_m"] == pytest.approx(0, abs=1)
        assert fila["dispersion_max_m"] > 0
        # Vuelve al punto de partida, asi que el ultimo y la mediana coinciden.
        assert fila["latitud"] == pytest.approx(20.6000, abs=1e-4)

    def test_la_dispersion_es_cota_superior_del_movimiento(self, db, tracker):
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_JITTER"]
        assert fila["dispersion_max_m"] > fila["desplazamiento_neto_m"]


@pytest.mark.unit
class TestLosInvariantesCorrenDeVerdad:
    def test_la_corrida_pasa_sus_propios_invariantes(self, db, tracker):
        """Si alguno fallara, `construir_cohorte_oficial` levantaria."""
        total = construir_cohorte_oficial(db, tracker)
        assert total > 0
        with db.connect() as conn:
            assert CohorteOficial().validar(conn) == []

    def test_un_canonico_inventado_a_mano_rompe_el_invariante(self, db, tracker):
        """Prueba que los invariantes DETECTAN, no solo que corren."""
        construir_cohorte_oficial(db, tracker)
        with db.begin() as conn:
            conn.execute(
                text(
                    f"UPDATE gold.{TABLA} SET nombre = 'NOMBRE QUE NADIE PUBLICO' "
                    "WHERE clee = 'C_VIVE'"
                )
            )
        with db.connect() as conn:
            violaciones = CohorteOficial().validar(conn)
        assert any("no esta marcado canonico" in v for v in violaciones)


@pytest.mark.unit
class TestLoQueQuedaEnMetaDeVerdad:
    def test_la_corrida_queda_exitosa_con_su_ventana(self, db, tracker):
        """`meta.job_runs.config` es `Text` con JSON, no `JSONB`.

        Asi que hay que deserializarlo. Importa mas alla del test: una
        consulta ad-hoc con `config->>'clave'` **falla** con "operator does
        not exist", porque los operadores de JSON no aplican a `text`. Hay
        que escribir `config::jsonb->>'clave'`.
        """
        construir_cohorte_oficial(db, tracker)
        with db.connect() as conn:
            corrida = (
                conn.execute(
                    text(
                        "SELECT status, config FROM meta.job_runs ORDER BY start_time DESC LIMIT 1"
                    )
                )
                .mappings()
                .one()
            )
        assert corrida["status"] == JobStatus.SUCCESS.value
        config = json.loads(corrida["config"])
        assert config["n_cortes_oficiales"] == 3
        assert config["ventana_cortes"] == [c.isoformat() for c in OFICIALES]

    def test_hay_una_tarea_por_tabla_destino(self, db, tracker):
        """La madre y la evidencia, cada una con su conteo."""
        construir_cohorte_oficial(db, tracker)
        with db.connect() as conn:
            tareas = {
                f["target_table"]: f
                for f in conn.execute(
                    text("SELECT target_table, status, rows_loaded FROM meta.process_tasks")
                ).mappings()
            }
        assert set(tareas) == {TABLA, TABLA_VARIANTES}
        assert tareas[TABLA]["status"] == TaskStatus.SUCCESS.value
        assert tareas[TABLA]["rows_loaded"] == len(negocios(db))
        assert tareas[TABLA_VARIANTES]["rows_loaded"] > 0


@pytest.mark.unit
class TestReanudarYRehacer:
    def test_una_segunda_corrida_se_salta_el_trabajo(self, db, tracker):
        """Con la ventana sin cambios no hay nada que hacer."""
        primera = construir_cohorte_oficial(db, tracker)
        segunda = construir_cohorte_oficial(db, tracker)
        assert segunda == primera
        with db.connect() as conn:
            saltadas = conn.execute(
                text(
                    "SELECT count(*) FROM meta.process_tasks "
                    "WHERE status = :s AND target_table = :t"
                ),
                {"s": TaskStatus.SKIPPED.value, "t": TABLA},
            ).scalar_one()
        assert saltadas == 1

    def test_force_la_reconstruye_igual(self, db, tracker):
        """Determinista: rehacerla da exactamente lo mismo."""
        antes = negocios(db) if construir_cohorte_oficial(db, tracker) else {}
        construir_cohorte_oficial(db, tracker, force=True)
        despues = negocios(db)
        assert set(antes) == set(despues)
        for clee in antes:
            assert antes[clee]["nombre"] == despues[clee]["nombre"]
            assert antes[clee]["codigo_act"] == despues[clee]["codigo_act"]

    def test_las_variantes_no_quedan_duplicadas_al_rehacer(self, db, tracker):
        """El TRUNCATE tiene que tomar las dos tablas o la FK lo rechaza."""
        construir_cohorte_oficial(db, tracker)
        con_una = len(variantes(db, "C_REBRAND", "nom_estab"))
        construir_cohorte_oficial(db, tracker, force=True)
        assert len(variantes(db, "C_REBRAND", "nom_estab")) == con_una
