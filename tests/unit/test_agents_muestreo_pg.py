"""El muestreo, el insumo, la persistencia y las métricas contra Postgres real.

El escenario es un gold chico con los cuatro estratos poblados y tres
negocios que el filtro tiene que excluir (sospechoso, sin nombre, sin
municipio). Lo que se afirma es lo que `verificacion_cierre.md` §4 promete:
cuotas por estrato, hipótesis congelada, reproducible por semilla, nombre
único, y que el agente recibe todo lo del DENUE y nada de la hipótesis.
"""

import json
from datetime import date

import polars as pl
import pytest
from sqlalchemy import insert, text
from sqlalchemy.orm import Session

from src.agents.survival import insumo, metricas, muestreo
from src.agents.survival.verificador import (
    EvidenciaDecidida,
    Resultado,
    guardar,
    verificar_muestra,
)
from src.shared.db.base import Base, get_table, tables_for
from src.shared.db.copy import copy_dataframe
from src.shared.db.models.silver import ActividadEconomica, Entidad, Municipio
from src.shared.db.partitions import create_partition
from src.shared.enums import JobStatus

pytestmark = pytest.mark.postgres

VENTANA = [date(2021, 5, 1), date(2021, 11, 1), date(2022, 5, 1)]
CUOTA = 4
POR_ESTRATO = 6


def fila_gold(
    clee: str, *, cerrado: bool, reocupado: bool | None = None, huecos: int = 0, **extra
) -> dict:
    """Una fila de gold con lo mínimo no nulo; el estrato lo deciden los tres primeros parámetros."""
    fila = dict(
        clee=clee,
        nombre=f"NEGOCIO {clee}",
        nombre_origen="moda",
        n_nombres=1,
        periodos=VENTANA,
        primer_periodo=VENTANA[0],
        ultimo_periodo=VENTANA[0] if cerrado else VENTANA[-1],
        cortes_observados=1 if cerrado else 3 - huecos,
        huecos=huecos,
        cortes_tras_ultimo=2 if cerrado else 0,
        meses_sin_aparecer=12 if cerrado else 0,
        local_reocupado=reocupado,
        cerrado=cerrado,
        cambio_tamano=False,
        cve_geo_inicial="14039",
        cve_geo_final="14039",
        lat_inicial=20.67,
        lon_inicial=-103.35,
        lat_final=20.67,
        lon_final=-103.35,
        se_mudo=False,
        distancia_mudanza_m=0.0,
        n_posiciones=1,
        codigo_act="722513",
        cambio_actividad=False,
        cluster_sospechoso=False,
        ventana_cortes=VENTANA,
        metodo_base="inegi",
        version_reglas="v1",
    )
    fila.update(extra)
    return fila


def poblacion() -> list[dict]:
    filas = []
    for i in range(POR_ESTRATO):
        filas.append(fila_gold(f"CR{i}", cerrado=True, reocupado=True))
        filas.append(fila_gold(f"CN{i}", cerrado=True, reocupado=False))
        filas.append(fila_gold(f"AI{i}", cerrado=False, huecos=1))
        filas.append(fila_gold(f"AC{i}", cerrado=False))
    filas.append(
        fila_gold(
            "SOSP",
            cerrado=True,
            reocupado=True,
            cluster_sospechoso=True,
            nombre=None,
            nombre_origen=None,
        )
    )
    filas.append(
        fila_gold("SINNOMBRE", cerrado=True, reocupado=True, nombre=None, nombre_origen=None)
    )
    filas.append(fila_gold("SINGEO", cerrado=True, reocupado=True, cve_geo_final=None))
    return filas


EXCLUIDOS = {"SOSP", "SINNOMBRE", "SINGEO"}


@pytest.fixture
def db(silver_engine, tracker):
    """Silver con un negocio observado tres veces, gold poblado y `verificacion` vacío."""
    with Session(silver_engine) as s:
        s.add_all(
            [
                Entidad(cve_ent="14", entidad="Jalisco"),
                Municipio(cve_ent="14", cve_mun="039", municipio="Guadalajara"),
                ActividadEconomica(
                    codigo_act="722513",
                    nombre_act="Taquerias",
                    sector="72",
                    subsector="722",
                    rama="7225",
                ),
            ]
        )
        s.commit()
    with silver_engine.begin() as conn:
        for schema in ("gold", "verificacion"):
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
            conn.execute(text(f"CREATE SCHEMA {schema}"))
        for corte in VENTANA:
            create_partition(conn, corte)
        # Las observaciones de UN negocio de la muestra, para el insumo.
        copy_dataframe(
            conn,
            pl.DataFrame(
                [
                    (
                        "o1",
                        VENTANA[0],
                        "AC0",
                        "TACOS PACO",
                        "PACO SA DE CV",
                        "3312345678",
                        "",
                        "tacospaco.mx",
                        "AV VALLARTA",
                        "1234",
                        "AMERICANA",
                        "44160",
                    ),
                    (
                        "o2",
                        VENTANA[1],
                        "AC0",
                        "TAQUERIA DON PACO",
                        "",
                        "3312345678",
                        "paco@x.mx",
                        "",
                        "AV VALLARTA",
                        "1234",
                        "AMERICANA",
                        "44160",
                    ),
                    ("o3", VENTANA[2], "AC0", "TACOS PACO", "", "", "", "", "", "", "", ""),
                ],
                schema={
                    "id": pl.Utf8,
                    "period": pl.Date,
                    "clee": pl.Utf8,
                    "nom_estab": pl.Utf8,
                    "raz_social": pl.Utf8,
                    "telefono": pl.Utf8,
                    "correoelec": pl.Utf8,
                    "www": pl.Utf8,
                    "nom_vial": pl.Utf8,
                    "numero_ext": pl.Utf8,
                    "nomb_asent": pl.Utf8,
                    "cod_postal": pl.Utf8,
                },
                orient="row",
            ),
            "establecimientos",
        )
    Base.metadata.create_all(silver_engine, tables=tables_for("gold") + tables_for("verificacion"))
    with silver_engine.begin() as conn:
        conn.execute(insert(get_table("negocios_clee_oficial", schema="gold")), poblacion())
    yield silver_engine


def muestra(db, tracker, nombre="piloto", semilla=0.42, cuota=CUOTA) -> dict[str, int]:
    return muestreo.crear_muestra(db, tracker, nombre, semilla, cuota)


def negocios_de(db, nombre: str) -> list[tuple[int, str, str, bool]]:
    with db.connect() as conn:
        return conn.execute(
            text("""
            SELECT mn.orden, mn.clee, mn.estrato, mn.cerrado_gold
            FROM verificacion.muestra_negocios mn JOIN verificacion.muestras m USING (muestra_id)
            WHERE m.nombre = :n ORDER BY mn.orden
            """),
            {"n": nombre},
        ).all()


@pytest.mark.unit
class TestLaTirada:
    def test_cuota_por_estrato_y_los_excluidos_fuera(self, db, tracker):
        conteos = muestra(db, tracker)
        assert conteos == {e: CUOTA for e, _ in muestreo.ESTRATOS}
        clees = {f.clee for f in negocios_de(db, "piloto")}
        assert len(clees) == CUOTA * 4
        assert not clees & EXCLUIDOS

    def test_el_estrato_y_la_hipotesis_quedan_congelados(self, db, tracker):
        muestra(db, tracker)
        for _, clee, estrato, cerrado_gold in negocios_de(db, "piloto"):
            assert (
                estrato
                == {
                    "CR": "cerrado|reocupado",
                    "CN": "cerrado|no_reocupado",
                    "AI": "activo|intermitente",
                    "AC": "activo|continuo",
                }[clee[:2]]
            )
            assert cerrado_gold is clee.startswith("C")

    def test_la_cabecera_dice_con_que_reglas_se_saco(self, db, tracker):
        from src.shared.config import get_settings

        muestra(db, tracker)
        with db.connect() as conn:
            m = conn.execute(text("SELECT * FROM verificacion.muestras")).mappings().one()
        assert m["n_negocios"] == CUOTA * 4 and m["tamano_pedido"] == CUOTA * 4
        assert m["estratos"] == {e: CUOTA for e, _ in muestreo.ESTRATOS}
        assert m["filtro"] == muestreo.FILTRO_POBLACION
        assert (m["version_reglas"], list(m["ventana_cortes"])) == ("v1", VENTANA)
        assert m["cierre_meses_ausente"] == get_settings().CIERRE_MESES_AUSENTE
        assert m["semilla"] == 0.42

    def test_la_misma_semilla_da_la_misma_muestra(self, db, tracker):
        muestra(db, tracker, "a", semilla=0.7)
        muestra(db, tracker, "b", semilla=0.7)
        assert [f[1:] for f in negocios_de(db, "a")] == [f[1:] for f in negocios_de(db, "b")]

    def test_el_nombre_es_unico_y_no_deja_basura(self, db, tracker):
        muestra(db, tracker)
        with pytest.raises(ValueError, match="ya existe"):
            muestra(db, tracker)
        with db.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM verificacion.muestras")).scalar() == 1

    def test_una_cuota_mayor_que_la_poblacion_da_lo_que_hay(self, db, tracker):
        conteos = muestra(db, tracker, cuota=10)
        assert conteos == {e: POR_ESTRATO for e, _ in muestreo.ESTRATOS}

    def test_gold_vacio_se_niega(self, db, tracker):
        with db.begin() as conn:
            conn.execute(text("TRUNCATE gold.negocios_clee_oficial"))
        with pytest.raises(ValueError, match="vacia"):
            muestra(db, tracker)

    def test_la_semilla_fuera_de_rango_se_niega_antes_de_escribir(self, db, tracker):
        with pytest.raises(ValueError, match=r"\[-1, 1\]"):
            muestra(db, tracker, semilla=3.0)

    def test_queda_en_meta(self, db, tracker):
        muestra(db, tracker)
        with db.connect() as conn:
            corrida = (
                conn.execute(
                    text(
                        "SELECT job_type, status, config FROM meta.job_runs ORDER BY start_time DESC LIMIT 1"
                    )
                )
                .mappings()
                .one()
            )
        assert (corrida["job_type"], corrida["status"]) == (
            muestreo.JOB_TYPE,
            JobStatus.SUCCESS.value,
        )
        assert json.loads(corrida["config"])["muestra"] == "piloto"

    def test_tamanos_poblacion(self, db):
        with db.connect() as conn:
            assert muestreo.tamanos_poblacion(conn) == {
                e: POR_ESTRATO for e, _ in muestreo.ESTRATOS
            }


@pytest.mark.unit
class TestElInsumo:
    def test_todo_lo_del_denue_y_nada_de_la_hipotesis(self, db, tracker):
        muestra(db, tracker, cuota=10)  # con 10 entra AC0 seguro
        with db.connect() as conn:
            muestra_id, ventana = insumo.muestra_por_nombre(conn, "piloto")
            n = insumo.cargar(conn, muestra_id, "AC0")
        assert n.nombres == ["TACOS PACO", "TAQUERIA DON PACO"]
        assert n.razones_sociales == ["PACO SA DE CV"]
        assert (
            n.telefonos == ["3312345678"]
            and n.correos == ["paco@x.mx"]
            and n.sitios == ["tacospaco.mx"]
        )
        assert n.domicilios == ["AV VALLARTA 1234 AMERICANA 44160"]
        assert (n.municipio, n.entidad, n.actividad) == ("Guadalajara", "Jalisco", "Taquerias")
        assert n.ultimo_corte_ventana == ventana[-1] == VENTANA[-1]
        assert n.estrato == "activo|continuo"
        for campo in ("cerrado", "cerrado_gold", "meses_sin_aparecer", "huecos"):
            assert not hasattr(n, campo)

    def test_un_negocio_fuera_de_la_muestra_es_lookup_error(self, db, tracker):
        muestra(db, tracker)
        with db.connect() as conn:
            muestra_id, _ = insumo.muestra_por_nombre(conn, "piloto")
            with pytest.raises(LookupError):
                insumo.cargar(conn, muestra_id, "SOSP")

    def test_una_muestra_inexistente_dice_como_crearla(self, db):
        with db.connect() as conn, pytest.raises(ValueError, match="muestrear --nombre nada"):
            insumo.muestra_por_nombre(conn, "nada")


class VerificadorFalso:
    """Decide por el prefijo del clee, y falla a propósito en uno."""

    agente = "falso:1b/v1"
    max_busquedas = max_paginas = max_vueltas = 1

    def __init__(self, falla: str | None = None):
        self.falla = falla
        self.vistos: list[str] = []

    def verificar(self, negocio: insumo.Negocio) -> Resultado:
        self.vistos.append(negocio.clee)
        if negocio.clee == self.falla:
            raise RuntimeError("se cayo la red")
        if negocio.clee.startswith("AI"):
            estado, evidencias = "indeterminado", []
        else:
            estado = "cerrado" if negocio.clee.startswith("C") else "activo"
            evidencias = [
                EvidenciaDecidida("https://maps.google.com/x", "cita", estado, "google_maps")
            ]
        return Resultado(
            muestra_id=negocio.muestra_id,
            clee=negocio.clee,
            estado=estado,
            confianza="alta",
            razonamiento="porque si, con fuentes",
            agente=self.agente,
            evidencias=evidencias,
            n_busquedas=2,
            vueltas=3,
            duracion_s=1.5,
        )


@pytest.mark.unit
class TestVerificarYGuardar:
    def test_escribe_veredictos_y_evidencias_y_se_reanuda(self, db, tracker):
        muestra(db, tracker)
        v = VerificadorFalso()
        conteos = verificar_muestra(db, tracker, v, "piloto")
        assert conteos["verificados"] == CUOTA * 4 and conteos["fallidos"] == 0
        assert conteos["indeterminado"] == CUOTA and conteos["cerrado"] == 2 * CUOTA
        with db.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM verificacion.veredictos")).scalar()
                == CUOTA * 4
            )
            assert (
                conn.execute(text("SELECT count(*) FROM verificacion.evidencias")).scalar()
                == 3 * CUOTA
            )
            muestra_id, _ = insumo.muestra_por_nombre(conn, "piloto")
            assert insumo.pendientes(conn, muestra_id, v.agente) == []
            # Otro agente tiene su propia lista: sus veredictos son otras filas.
            assert len(insumo.pendientes(conn, muestra_id, "otro:7b/v1")) == CUOTA * 4
        # Segunda corrida: nada que hacer, y no duplica.
        assert verificar_muestra(db, tracker, v, "piloto")["verificados"] == 0

    def test_limite_y_clee(self, db, tracker):
        muestra(db, tracker)
        v = VerificadorFalso()
        assert verificar_muestra(db, tracker, v, "piloto", limite=3)["verificados"] == 3
        assert len(v.vistos) == 3
        primero = negocios_de(db, "piloto")[5].clee
        verificar_muestra(db, tracker, v, "piloto", solo_clee=primero)
        assert v.vistos[-1] == primero

    def test_un_negocio_que_falla_no_tumba_la_muestra(self, db, tracker):
        muestra(db, tracker)
        caido = negocios_de(db, "piloto")[0].clee
        conteos = verificar_muestra(db, tracker, VerificadorFalso(falla=caido), "piloto")
        assert conteos == {**conteos, "verificados": CUOTA * 4 - 1, "fallidos": 1}
        with db.connect() as conn:
            corrida = conn.execute(
                text(
                    "SELECT status FROM meta.job_runs WHERE job_name = :j ORDER BY start_time DESC LIMIT 1"
                ),
                {"j": "agente_supervivencia_verificar"},
            ).scalar_one()
            muestra_id, _ = insumo.muestra_por_nombre(conn, "piloto")
            assert insumo.pendientes(conn, muestra_id, "falso:1b/v1") == [(1, caido)]
        assert corrida == JobStatus.PARTIAL.value

    def test_guardar_respeta_el_vocabulario_de_la_base(self, db, tracker):
        """La ultima red: si el bucle dejara pasar un estado invalido, la base lo rechaza."""
        from sqlalchemy.exc import IntegrityError

        muestra(db, tracker)
        clee = negocios_de(db, "piloto")[0].clee
        with db.connect() as conn:
            muestra_id, _ = insumo.muestra_por_nombre(conn, "piloto")
        malo = Resultado(
            muestra_id=muestra_id,
            clee=clee,
            estado="closed",
            confianza="alta",
            razonamiento="x",
            agente="a/v1",
        )
        with pytest.raises(IntegrityError), db.begin() as conn:
            guardar(conn, malo)


@pytest.mark.unit
class TestLasMetricas:
    def test_la_tabla_de_confusion_por_estrato(self, db, tracker):
        muestra(db, tracker)
        verificar_muestra(db, tracker, VerificadorFalso(), "piloto")
        with db.connect() as conn:
            filas = {f.estrato: f for f in metricas.calcular(conn, "piloto")}
        cr, cn, ai, ac = (filas[e] for e, _ in muestreo.ESTRATOS)
        assert (cr.vp, cr.fp, cr.precision_cerrado) == (CUOTA, 0, 1.0)
        assert (cn.vp, cn.precision_cerrado) == (CUOTA, 1.0)
        assert (ai.indeterminados, ai.determinados, ai.precision_cerrado) == (CUOTA, 0, None)
        assert (ac.vn, ac.fn, ac.tasa_falsos_negativos) == (CUOTA, 0, 0.0)
        assert all(f.poblacion == POR_ESTRATO and f.sin_veredicto == 0 for f in filas.values())
        assert metricas.precision_cerrado_reponderada(list(filas.values())) == 1.0
        assert metricas.tasa_falsos_negativos_reponderada(list(filas.values())) == 0.0

    def test_sin_veredictos_todo_es_sin_veredicto(self, db, tracker):
        muestra(db, tracker)
        with db.connect() as conn:
            filas = metricas.calcular(conn, "piloto")
        assert all(f.sin_veredicto == f.n == CUOTA for f in filas)
        assert metricas.precision_cerrado_reponderada(filas) is None

    def test_filtrar_por_agente(self, db, tracker):
        muestra(db, tracker)
        verificar_muestra(db, tracker, VerificadorFalso(), "piloto")
        with db.connect() as conn:
            assert all(
                f.sin_veredicto == CUOTA
                for f in metricas.calcular(conn, "piloto", agente="otro/v1")
            )
            assert all(
                f.sin_veredicto == 0
                for f in metricas.calcular(conn, "piloto", agente="falso:1b/v1")
            )
