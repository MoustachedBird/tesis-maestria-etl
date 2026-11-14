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
                   ancestro comun es determinista. Y sale en el corte 2 de 3:
                   6 meses ausente, MENOS que el umbral -> NO esta cerrado
    cerro          solo en el primer corte: 12 meses ausente -> cerrado
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
ALTA_NORMAL = date(2015, 1, 1)
"""Fecha de alta de toda observacion que no esta en `FECHA_ALTA`."""
FECHA_ALTA: dict[str, date] = {
    # Posterior a su corte (2021-05): imposible, no cuenta.
    "f1": date(2030, 1, 1),
    "g1": date(2030, 1, 1),
    # Igual a su corte (2021-11): el limite admisible.
    "g2": OFICIALES[1],
}
"""Fechas de alta por `id` de observacion, para los casos de dato roto."""

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
    # --- cerro: una sola aparicion, 12 meses antes del ultimo corte ---
    (
        "c1",
        OFICIALES[0],
        "C_CERRO",
        "PAPELERIA FIN",
        "311811",
        "14",
        "039",
        20.75,
        -103.25,
        "",
        "",
        MICRO,
    ),
    # --- cruza_rama: panaderia y taqueria no comparten rama ---
    ("x1", OFICIALES[0], "C_CRUZA", "MIXTO", "311811", "14", "039", 20.8, -103.2, "", "", MICRO),
    ("x2", OFICIALES[1], "C_CRUZA", "MIXTO", "722513", "14", "039", 20.8, -103.2, "", "", MICRO),
    # --- solo_viejo: no tiene ninguna observacion oficial ---
    ("o1", VIEJO, "C_VIEJO", "EXTINTO", "311811", "14", "039", 20.9, -103.1, "", "", MICRO),
    # --- futuro: su unica fecha de alta es posterior al corte en que aparece ---
    (
        "f1",
        OFICIALES[0],
        "C_FUTURO",
        "IMPOSIBLE",
        "311811",
        "14",
        "039",
        20.9,
        -103.2,
        "",
        "",
        MICRO,
    ),
    # --- mixta: una fecha de alta imposible y una admisible ---
    ("g1", OFICIALES[0], "C_MIXTA", "A MEDIAS", "311811", "14", "039", 20.9, -103.3, "", "", MICRO),
    ("g2", OFICIALES[1], "C_MIXTA", "A MEDIAS", "311811", "14", "039", 20.9, -103.3, "", "", MICRO),
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
    y el `COPY` las hace valer.
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
        conn.execute(text("DROP SCHEMA IF EXISTS gold CASCADE"))
        conn.execute(text("CREATE SCHEMA gold"))
        for corte in CORTES:
            create_partition(conn, corte)
        copy_dataframe(
            conn,
            pl.DataFrame(
                [(*o, COLONIA, FIJO, FECHA_ALTA.get(o[0], ALTA_NORMAL)) for o in OBSERVACIONES],
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


def valores_en_silver(db, clee: str, campo: str) -> list[str]:
    """Los valores distintos de un campo, leidos de Silver por `clee`.

    Es el camino que reemplaza a la tabla de variantes: el insumo del LLM que
    unifica el nombre y de los agentes que buscan el negocio en linea. Probarlo
    aqui es lo que garantiza que siga funcionando.
    """
    with db.connect() as conn:
        return sorted(
            conn.execute(
                text(f"""
                SELECT DISTINCT {campo} FROM silver.establecimientos
                WHERE clee = :clee AND {campo} IS NOT NULL AND {campo} <> ''
                """),
                {"clee": clee},
            )
            .scalars()
            .all()
        )


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

    def test_los_periodos_son_los_oficiales_y_nada_mas(self, db, tracker):
        """C_VIVE tiene una observacion en 2020-11 que NO debe aparecer.

        `idx_primer`, `idx_ultimo`, `duracion_periodos` y
        `presente_desde_el_inicio` ya no son columnas: eran los indices de
        `permanencia` renumerados, y lo que el analisis necesita --los cortes
        exactos-- esta en `periodos`, de donde todo eso se deriva.
        """
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_VIVE"]
        assert fila["periodos"] == OFICIALES
        assert VIEJO not in fila["periodos"]
        assert len(fila["periodos"]) == fila["cortes_observados"]

    def test_la_edad_no_hereda_el_corte_viejo(self, db, tracker):
        """Es el efecto concreto de renumerar, y el que importa.

        Si el corte de 2020-11 contara, `ultimo_periodo` seria el mismo pero
        `primer_periodo` saldria seis meses antes, y con el la censura. La
        edad sale de `fecha_alta`, asi que no se mueve -- pero el diagnostico
        `dias_alta_a_primera` si, y es el que delata el error.
        """
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_VIVE"]
        esperado = (OFICIALES[0] - date(2015, 1, 1)).days
        assert fila["dias_alta_a_primera"] == esperado

    def test_la_censura_se_mide_en_la_ventana_oficial(self, db, tracker):
        """C_RAMA sale en el corte 1 de 3, asi que le queda uno despues."""
        construir_cohorte_oficial(db, tracker)
        assert negocios(db)["C_RAMA"]["cortes_tras_ultimo"] == 1
        assert negocios(db)["C_VIVE"]["cortes_tras_ultimo"] == 0


@pytest.mark.unit
class TestElNombre:
    def test_la_moda_gana_al_mas_reciente(self, db, tracker):
        """TACOS PACO x2 contra TAQUERIA DON PACO x1, que es el ultimo."""
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_REBRAND"]
        assert fila["nombre"] == "TACOS PACO"
        assert fila["nombre_origen"] == "moda"
        assert fila["n_nombres"] == 2

    def test_las_variantes_se_recuperan_de_silver(self, db, tracker):
        """El camino que reemplaza a la tabla de variantes.

        Es el insumo del LLM que unifica el nombre y de los agentes que
        buscan el negocio en linea, asi que tiene que seguir funcionando.
        """
        construir_cohorte_oficial(db, tracker)
        assert valores_en_silver(db, "C_REBRAND", "nom_estab") == [
            "TACOS PACO",
            "TAQUERIA DON PACO",
        ]

    def test_un_cluster_sospechoso_no_recibe_nombre(self, db, tracker):
        """4 nombres en 3 cortes es imposible para un cluster correcto."""
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_SOSP"]
        assert fila["cluster_sospechoso"] is True
        assert fila["nombre"] is None
        assert fila["nombre_origen"] is None
        assert fila["n_nombres"] == 4

    def test_pero_sus_nombres_siguen_en_silver(self, db, tracker):
        """Es el mejor diagnostico de un mal cluster que existe."""
        construir_cohorte_oficial(db, tracker)
        assert len(valores_en_silver(db, "C_SOSP", "nom_estab")) == 4

    def test_un_negocio_sano_no_queda_marcado(self, db, tracker):
        construir_cohorte_oficial(db, tracker)
        assert negocios(db)["C_VIVE"]["cluster_sospechoso"] is False


@pytest.mark.unit
class TestElScianCanonico:
    """El ancestro comun por prefijo, que sigue vigente."""

    def test_misma_rama_da_el_ancestro_comun(self, db, tracker):
        """311811 y 311812 comparten los 5 primeros digitos."""
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_RAMA"]
        assert fila["codigo_act"] == "31181"
        assert fila["cambio_actividad"] is True

    def test_cruzar_rama_queda_en_null(self, db, tracker):
        """Ahi hay que juzgar; un valor inventado seria peor que la ausencia."""
        construir_cohorte_oficial(db, tracker)
        assert negocios(db)["C_CRUZA"]["codigo_act"] is None

    def test_una_sola_actividad_se_queda_completa(self, db, tracker):
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_VIVE"]
        assert fila["codigo_act"] == "311811"
        assert fila["cambio_actividad"] is False


@pytest.mark.unit
class TestLaEdad:
    """La mitad de la razon de ser de la tabla."""

    def test_se_mide_de_la_fecha_de_alta_al_ultimo_corte(self, db, tracker):
        """El escenario pone fecha_alta 2015-01-01 y ultimo corte 2022-05-01.

        Son 2678 dias, o 7.33 anios julianos. Medido contra "hoy" el numero
        cambiaria cada dia y la tabla dejaria de ser reproducible.
        """
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_VIVE"]
        assert fila["fecha_alta"] == date(2015, 1, 1)
        assert fila["ultimo_periodo"] == OFICIALES[2]
        esperado = (OFICIALES[2] - date(2015, 1, 1)).days / 365.25
        assert float(fila["edad_anios"]) == pytest.approx(esperado, abs=0.01)

    def test_un_negocio_que_desaparecio_tiene_edad_mas_corta(self, db, tracker):
        """C_RAMA sale en el corte 1 de 3; C_VIVE llega al final.

        La edad de los dos arranca igual, asi que la diferencia es exactamente
        lo que el modelo de supervivencia mide.
        """
        construir_cohorte_oficial(db, tracker)
        filas = negocios(db)
        assert filas["C_RAMA"]["edad_anios"] < filas["C_VIVE"]["edad_anios"]

    def test_el_diagnostico_de_la_fecha_de_alta(self, db, tracker):
        """`dias_alta_a_primera` positivo: se dio de alta antes de aparecer."""
        construir_cohorte_oficial(db, tracker)
        assert negocios(db)["C_VIVE"]["dias_alta_a_primera"] > 0

    def test_una_fecha_de_alta_posterior_a_su_corte_no_cuenta(self, db, tracker):
        """C_FUTURO solo trae un alta de 2030 en el corte de 2021-05.

        Es un dato roto (el alta precede a la aparicion por definicion) y en
        la cohorte real hay 12 identidades asi: con un MIN a ciegas la edad
        sale negativa y la corrida entera truena en el invariante.
        """
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_FUTURO"]
        assert fila["fecha_alta"] is None
        assert fila["edad_anios"] is None
        assert fila["dias_alta_a_primera"] is None

    def test_la_fecha_admisible_gana_a_la_imposible(self, db, tracker):
        """C_MIXTA trae 2030 en 2021-05 y 2021-11-01 en 2021-11.

        El MIN ignora la imposible y toma la otra aunque sea mayor; y un alta
        igual a su corte es el limite que si se admite.
        """
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_MIXTA"]
        assert fila["fecha_alta"] == OFICIALES[1]
        assert float(fila["edad_anios"]) == 0


@pytest.mark.unit
class TestElVeredictoDeCierre:
    """La otra mitad, y es la HIPOTESIS que hay que validar con agentes."""

    def test_presente_en_el_ultimo_corte_es_activo(self, db, tracker):
        """Censurado por la derecha: no sabemos si cerro despues."""
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_VIVE"]
        assert fila["cortes_tras_ultimo"] == 0
        assert fila["cerrado"] is False

    def test_doce_meses_ausente_es_cerrado(self, db, tracker):
        """C_CERRO aparece solo en 2021-05 y la ventana termina en 2022-05."""
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_CERRO"]
        assert fila["meses_sin_aparecer"] == 12
        assert fila["cortes_tras_ultimo"] == 2
        assert fila["cerrado"] is True

    def test_seis_meses_ausente_todavia_no_se_afirma_nada(self, db, tracker):
        """C_RAMA no aparece en 2022-05, pero son 6 meses: menos que el umbral.

        Es la diferencia entre contar cortes y contar tiempo. Con "un corte
        ausente" C_RAMA y C_CERRO serian iguales; en meses uno lleva la
        mitad de evidencia que el otro, y la regla no lo declara cerrado.
        """
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_RAMA"]
        assert fila["cortes_tras_ultimo"] == 1
        assert fila["meses_sin_aparecer"] == 6
        assert fila["cerrado"] is False
        assert fila["local_reocupado"] is None

    def test_los_ingredientes_quedan_para_recalcular(self, db, tracker):
        """Con el umbral en 6, C_RAMA pasaria a cerrado sin reconstruir."""
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_RAMA"]
        assert fila["meses_sin_aparecer"] == 6
        assert fila["huecos"] == 0

    def test_huecos_marca_donde_la_regla_es_fragil(self, db, tracker):
        """Son los que ya desaparecieron y VOLVIERON.

        En el escenario ninguno lo hace, y ese 0 es la aserción correcta: si
        apareciera uno sin que el escenario lo monte, el calculo de huecos
        estaria mal.
        """
        construir_cohorte_oficial(db, tracker)
        assert all(f["huecos"] == 0 for f in negocios(db).values())


@pytest.mark.unit
class TestLosExtremosDeLaTrayectoria:
    """Lo que `permanencia` consume y tira; aqui se queda."""

    def test_el_empleo_y_el_tamano_en_los_dos_extremos(self, db, tracker):
        """C_VIVE pasa de MICRO (0 empleados) a PEQUENA (11)."""
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_VIVE"]
        assert fila["empleados_inicial"] == 0
        assert fila["empleados_final"] == 11
        assert fila["tamano_inicial"] == "Micro"
        assert fila["tamano_final"] == "Small"
        assert fila["cambio_tamano"] is True

    def test_la_llave_geografica_va_concatenada_y_jerarquica(self, db, tracker):
        """El escenario no tiene cve_loc ni ageb: la llave corta en municipio.

        Es la prueba de que el CASE no concatena a ciegas -- con un hueco en
        medio habria puesto el AGEB donde va la localidad.
        """
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_VIVE"]
        assert fila["cve_geo_inicial"] == "14039"
        assert fila["cve_geo_final"] == "14039"
        assert len(fila["cve_geo_final"]) == 5  # el largo dice el nivel

    def test_un_negocio_que_no_se_movio(self, db, tracker):
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_VIVE"]
        assert fila["se_mudo"] is False
        assert fila["distancia_mudanza_m"] == pytest.approx(0, abs=1)
        assert fila["n_posiciones"] == 1

    def test_el_jitter_no_cuenta_como_mudanza(self, db, tracker):
        """C_JITTER oscila ~89 m y vuelve, dentro del mismo municipio.

        La mudanza se mide a nivel municipio justamente para esto: comparar
        coordenadas marcaria como mudanza cualquier re-geocodificacion.
        """
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_JITTER"]
        assert fila["n_posiciones"] == 2
        assert fila["se_mudo"] is False
        assert fila["distancia_mudanza_m"] == pytest.approx(0, abs=1)

    def test_las_posiciones_de_los_dos_extremos(self, db, tracker):
        construir_cohorte_oficial(db, tracker)
        fila = negocios(db)["C_VIVE"]
        assert fila["lat_inicial"] == pytest.approx(20.67)
        assert fila["lat_final"] == pytest.approx(20.67)
        assert fila["h3_cell_9_final"] is None  # el escenario no calcula h3


@pytest.mark.unit
class TestLosInvariantesCorrenDeVerdad:
    def test_la_corrida_pasa_sus_propios_invariantes(self, db, tracker):
        """Si alguno fallara, `construir_cohorte_oficial` levantaria."""
        total = construir_cohorte_oficial(db, tracker)
        assert total > 0
        with db.connect() as conn:
            assert CohorteOficial().validar(conn) == []

    def test_un_veredicto_alterado_a_mano_rompe_el_invariante(self, db, tracker):
        """Prueba que los invariantes DETECTAN, no solo que corren.

        Se marca como cerrado un negocio presente en el ultimo corte, que es
        la contradiccion mas grave posible en esta tabla: haria que la muestra
        a verificar incluyera negocios que el propio DENUE sigue listando.
        """
        construir_cohorte_oficial(db, tracker)
        with db.begin() as conn:
            conn.execute(text(f"UPDATE gold.{TABLA} SET cerrado = TRUE WHERE clee = 'C_VIVE'"))
        with db.connect() as conn:
            violaciones = CohorteOficial().validar(conn)
        assert any("presentes en el ultimo corte marcados como cerrados" in v for v in violaciones)

    def test_una_edad_negativa_rompe_el_invariante(self, db, tracker):
        """Fecha de alta posterior al ultimo corte: imposible."""
        construir_cohorte_oficial(db, tracker)
        with db.begin() as conn:
            conn.execute(text(f"UPDATE gold.{TABLA} SET edad_anios = -1 WHERE clee = 'C_VIVE'"))
        with db.connect() as conn:
            violaciones = CohorteOficial().validar(conn)
        assert any("edades NEGATIVAS" in v for v in violaciones)


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
        """Una sola tabla, una sola tarea, con su conteo."""
        construir_cohorte_oficial(db, tracker)
        with db.connect() as conn:
            tareas = {
                f["target_table"]: f
                for f in conn.execute(
                    text("SELECT target_table, status, rows_loaded FROM meta.process_tasks")
                ).mappings()
            }
        assert set(tareas) == {TABLA}
        assert tareas[TABLA]["status"] == TaskStatus.SUCCESS.value
        assert tareas[TABLA]["rows_loaded"] == len(negocios(db))


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

    def test_rehacer_no_duplica_filas(self, db, tracker):
        """El TRUNCATE del `--force` deja la tabla limpia antes de reconstruir."""
        primera = construir_cohorte_oficial(db, tracker)
        construir_cohorte_oficial(db, tracker, force=True)
        assert len(negocios(db)) == primera
