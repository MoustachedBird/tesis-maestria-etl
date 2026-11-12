"""El SQL de la cohorte oficial: lo que afirma y lo que se niega a afirmar.

Sin base de datos. Lo que se revisa aquí son las decisiones que, si se
rompen, producen una tabla que **parece** correcta: una edad medida contra
"hoy" en vez de contra el último corte, un veredicto de cierre que no
corresponde al umbral de la corrida, una ventana heredada de los 20 cortes,
un nombre inventado para un cluster que agrupó dos negocios. Ninguna de esas
levanta un error en tiempo de ejecución — y por eso tienen test.
"""

from datetime import date

import pytest

from src.etl.loaders.gold_cohorte_oficial import (
    DIAS_POR_ANIO,
    METODO_BASE,
    SQL_CVE_GEO,
    TABLA,
    CohorteOficial,
)
from src.shared.db.base import get_table
from src.shared.db.models.gold import ORIGEN_NOMBRE_LLM, ORIGEN_NOMBRE_MODA

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
"""La ventana oficial medida el 2026-11-08: 9 cortes, 2021-05 .. 2026-05."""


@pytest.fixture
def cohorte():
    """El agregado con la ventana ya resuelta, como lo deja `cortes()`.

    Se inyecta el cache en vez de consultar: la ventana sale de un recorrido
    de los 82.9M hechos, y lo que estos tests revisan es el SQL que se genera
    A PARTIR de ella.
    """
    ag = CohorteOficial()
    ag._ventana = list(VENTANA)
    return ag


@pytest.fixture
def pasos(cohorte):
    """Los pasos de un bloque, con la censura en el último corte oficial."""
    return cohorte.pasos_bloque(bloque=7, ultimo_idx=len(VENTANA) - 1)


@pytest.fixture
def madre(pasos):
    """El SQL que escribe la tabla."""
    return next(p.sql for p in pasos if p.nombre == TABLA)


@pytest.mark.unit
class TestLaEdad:
    """Es la mitad de la razón de ser de la tabla; nadie la calculaba antes."""

    def test_se_mide_al_ultimo_corte_y_no_a_hoy(self, madre):
        """Con "hoy" la tabla dejaría de ser reproducible.

        La misma fila daría una edad distinta cada vez que se consultara, y
        dos corridas del mismo día ya no serían comparables.
        """
        assert "v.ultimo_periodo - v.fecha_alta_date" in madre
        for prohibido in ("now()", "CURRENT_DATE", "localtimestamp"):
            assert prohibido not in madre

    def test_sale_de_la_fecha_de_alta_no_del_primer_corte(self, madre):
        """La fecha de alta es el único proxy INDEPENDIENTE de la ventana.

        Medir desde el primer corte observado daría la antigüedad *en el
        directorio*, que es una propiedad de cuándo INEGI empezó a mirar, no
        del negocio.
        """
        assert "fecha_alta_date" in madre
        assert "v.primer_periodo - v.fecha_alta_date" in madre  # solo el diagnostico

    def test_usa_el_anio_juliano(self, madre):
        """365 inflaría las edades ~0.07% por año.

        Importa porque la edad entra al modelo como variable continua.
        """
        assert str(DIAS_POR_ANIO) in madre
        assert DIAS_POR_ANIO == 365.25

    def test_sin_fecha_de_alta_la_edad_es_NULL(self, madre):
        """No se inventa una edad con el primer corte como sustituto."""
        assert "CASE WHEN v.fecha_alta_date IS NULL THEN NULL" in madre


@pytest.mark.unit
class TestElVeredictoDeCierre:
    """La otra mitad: es la hipótesis que la tabla existe para validar."""

    def test_el_umbral_sale_de_la_config(self, madre):
        """No es un literal escondido en el SQL: es calibración.

        Que viva en `.env` y quede en `version_reglas` es lo que permite
        cambiarlo y saber con qué umbral se construyó cada versión.
        """
        from src.shared.config import get_settings

        umbral = get_settings().CIERRE_CORTES_AUSENTE
        assert f">= {umbral} AS cerrado" in madre

    def test_se_mide_contra_el_ultimo_corte_oficial(self, madre):
        """`cortes_tras_ultimo` con el idx 8, que es el noveno corte."""
        assert "8 - v.idx_ultimo AS cortes_tras_ultimo" in madre

    def test_los_ingredientes_se_conservan(self, madre):
        """Con ellos, cambiar el umbral no exige reconstruir 9.58M filas.

        `huecos` además mide el error de la propia regla: son los negocios
        que ya desaparecieron y volvieron, o sea casos probados de que
        ausencia no implica cierre.
        """
        assert "AS cortes_tras_ultimo" in madre
        assert "AS huecos" in madre

    def test_el_umbral_es_configurable_sin_tocar_codigo(self, monkeypatch):
        from src.shared.config import get_settings

        monkeypatch.setenv("CIERRE_CORTES_AUSENTE", "3")
        get_settings.cache_clear()
        try:
            ag = CohorteOficial()
            ag._ventana = list(VENTANA)
            sql = next(p.sql for p in ag.pasos_bloque(0, 8) if p.nombre == TABLA)
            assert ">= 3 AS cerrado" in sql
        finally:
            get_settings.cache_clear()


@pytest.mark.unit
class TestLosExtremosDeLaTrayectoria:
    """Lo que `permanencia` consume y tira; aquí se queda.

    No contradice `b0007`: el primer y el último estado no son "el valor de
    un corte cualquiera", son los dos extremos de la vida.
    """

    def test_los_dos_extremos_vienen_de_una_sola_observacion_cada_uno(self, pasos):
        """`DISTINCT ON` y no agregados por campo: así son coherentes.

        El estado inicial sale todo de la MISMA primera observación, no el
        empleo de una y la geografía de otra.
        """
        for nombre in ("primera", "ultima"):
            sql = next(p.sql for p in pasos if p.nombre == nombre)
            assert "DISTINCT ON (clee)" in sql
        primera = next(p.sql for p in pasos if p.nombre == "primera")
        ultima = next(p.sql for p in pasos if p.nombre == "ultima")
        assert "period ASC" in primera
        assert "period DESC" in ultima

    def test_el_empleo_y_el_tamano_en_los_dos_extremos(self, madre):
        for columna in (
            "empleados_inicial",
            "empleados_final",
            "tamano_inicial",
            "tamano_final",
        ):
            assert columna in madre

    def test_la_geografia_en_los_dos_extremos(self, madre):
        for columna in ("cve_geo_inicial", "cve_geo_final", "lat_inicial", "lat_final"):
            assert columna in madre

    def test_la_distancia_respeta_el_orden_temporal(self, madre):
        """Es entre el primer y el último punto, no la diagonal del rectángulo.

        La diagonal contestaría "cuánto deambuló"; esto contesta "terminó en
        otro lado", que es lo que `se_mudo` necesita como respaldo.
        """
        assert "u.latitud - p.latitud" in madre
        assert "lat_min" not in madre  # el rectangulo se queda en permanencia


@pytest.mark.unit
class TestLaLlaveGeografica:
    def test_va_concatenada_y_jerarquica(self):
        """`cve_mun` no es único a nivel nacional sin `cve_ent`.

        Con los componentes sueltos, comparar la geografía inicial contra la
        final podría emparejar el municipio 039 de un estado con el de otro.
        """
        assert "cve_ent || cve_mun || cve_loc || ageb" in SQL_CVE_GEO

    def test_se_corta_en_el_primer_nivel_ausente(self):
        """Concatenar a ciegas con un hueco produce una llave FALSA.

        Entidad + municipio + AGEB, sin localidad, pondría el AGEB en la
        posición de la localidad. El `CASE` garantiza que el valor sea
        siempre un prefijo válido, y su largo dice hasta dónde llega.
        """
        assert SQL_CVE_GEO.count("WHEN") == 4

    def test_la_mudanza_se_mide_a_nivel_municipio(self, madre):
        """El ruido de GPS mueve el punto sin cambiar el municipio.

        Comparar coordenadas marcaría como mudanza cualquier
        re-geocodificación de INEGI.
        """
        assert "LEFT(p.cve_geo, 5) <> LEFT(u.cve_geo, 5)" in madre

    def test_no_se_afirma_una_mudanza_con_la_llave_ausente(self, madre):
        """Con una nula, `IS DISTINCT FROM` diría que se mudó.

        Y lo que pasa es que falta el dato, no que se movió.
        """
        assert "p.cve_geo IS NOT NULL AND u.cve_geo IS NOT NULL" in madre


@pytest.mark.unit
class TestNoEsUnaCopiaDePermanencia:
    """El error del primer diseño, que este rediseño corrige."""

    def test_no_repite_las_banderas_tiene(self, madre):
        """Quince columnas que `permanencia` ya tenía.

        Con los valores concretos materializados, un NULL dice lo mismo.
        """
        assert "tiene_" not in madre

    def test_no_repite_el_rectangulo_ni_la_dispersion(self, madre):
        for columna in ("lat_min", "lat_max", "dispersion_max_m", "n_direcciones"):
            assert columna not in madre

    def test_no_hay_tabla_de_variantes(self, cohorte, pasos):
        """Los valores distintos salen de silver filtrando por `clee`."""
        assert cohorte.tablas_destino == (TABLA,)
        assert [p.nombre for p in pasos].count(TABLA) == 1

    def test_la_tabla_no_tiene_columnas_de_cobertura(self):
        columnas = {c.name for c in get_table(TABLA, schema="gold").columns}
        assert not {c for c in columnas if c.startswith("tiene_")}

    def test_lo_que_si_aporta_sobre_permanencia(self):
        """Edad, veredicto y extremos: lo que justifica la tabla."""
        columnas = {c.name for c in get_table(TABLA, schema="gold").columns}
        assert {
            "edad_anios",
            "cerrado",
            "empleados_inicial",
            "empleados_final",
            "tamano_inicial",
            "tamano_final",
            "cve_geo_inicial",
            "cve_geo_final",
        } <= columnas


@pytest.mark.unit
class TestElNombre:
    def test_la_moda_sale_de_una_pasada_y_no_de_una_lateral(self, pasos, madre):
        """Es una diferencia de complejidad, no de estilo.

        Un `LEFT JOIN LATERAL` con `LIMIT 1` fuerza un nested loop: una
        búsqueda por identidad, cada una recorriendo la temporal completa
        porque no tiene índice. Medido: dejó el `INSERT` del bloque 1
        corriendo 21 minutos sin terminar (~11 horas para los 32).
        """
        canonico = next(p.sql for p in pasos if p.nombre == "canonico")
        assert "DISTINCT ON (clee)" in canonico
        assert "LATERAL" not in madre
        assert "LEFT JOIN canonico nom USING (clee)" in madre

    def test_los_conteos_del_nombre_van_antes(self, pasos):
        """La moda necesita saber en cuántos cortes se usó cada nombre."""
        orden = [p.nombre for p in pasos]
        assert orden.index("nombres") < orden.index("canonico")

    def test_el_desempate_hace_reproducible_la_eleccion(self, pasos):
        """Sin él, dos nombres empatados dependerían del plan."""
        canonico = next(p.sql for p in pasos if p.nombre == "canonico")
        assert canonico.rstrip().endswith("nom_estab")

    def test_la_fila_dice_quien_eligio_el_nombre(self, madre):
        """Sin esto, un nombre generado y uno observado son indistinguibles.

        Es lo que vuelve auditable el resultado del LLM cuando esa etapa
        exista.
        """
        assert f"'{ORIGEN_NOMBRE_MODA}' END AS nombre_origen" in madre
        assert ORIGEN_NOMBRE_LLM != ORIGEN_NOMBRE_MODA

    def test_un_cluster_sospechoso_no_recibe_nombre(self, madre):
        """Con [TACOS PACO, FARMACIA PACO] cualquier nombre es falso.

        Más nombres que cortes es IMPOSIBLE para un cluster correcto --un
        establecimiento aparece a lo más una vez por corte-- así que ahí la
        identidad agrupó más de un negocio.
        """
        assert "CASE WHEN v.n_nombres > 9 THEN NULL ELSE nom.nombre END AS nombre" in madre
        assert "v.n_nombres > 9 AS cluster_sospechoso" in madre

    def test_y_tampoco_recibe_origen(self, madre):
        """Un `nombre_origen` sin nombre sería basura."""
        assert "v.n_nombres > 9 OR nom.nombre IS NULL THEN NULL" in madre


@pytest.mark.unit
class TestLaVentanaEsLaOficial:
    def test_el_idx_es_la_posicion_en_la_ventana_oficial(self, cohorte):
        """El corte 2022-05 es el idx 2 de la ventana, no el 13 de los 20.

        Heredar los índices de la secuencia completa rompería la edad y la
        censura sin un solo error visible.
        """
        sql = cohorte.sql_llenar_obs(VENTANA[2], 2, 32)
        assert "    2,\n" in sql

    def test_la_ventana_queda_grabada_en_cada_fila(self, madre):
        """Si mañana entra un corte, la N y todas las edades cambian."""
        for corte in VENTANA:
            assert f"DATE '{corte.isoformat()}'" in madre
        assert "::date[]" in madre

    def test_los_periodos_exactos_se_publican(self, pasos, madre):
        """De ahí sale cualquier patrón de presencia, y los años."""
        ventana = next(p.sql for p in pasos if p.nombre == "ventana")
        assert "ARRAY_AGG(DISTINCT period ORDER BY period) AS periodos" in ventana
        assert "v.periodos" in madre

    def test_no_lee_ninguna_tabla_de_identidad(self, cohorte):
        """La identidad es el `clee` de los hechos, no un `clee_resuelto`."""
        sql = cohorte.sql_llenar_obs(VENTANA[0], 0, 32)
        assert "clee_radio" not in sql
        assert "clee_resuelto" not in sql
        assert "e.clee IS NOT NULL" in sql

    def test_el_metodo_base_no_es_un_metodo_de_propagacion(self):
        assert METODO_BASE not in {"radio", "h3", "celda_h3_7"}


@pytest.mark.unit
class TestLosInvariantes:
    """Lo que, si no se cumple, debe FALLAR la corrida."""

    def _consultas(self, cohorte):
        """Las consultas que `validar` corre, sin ejecutarlas."""
        vistas: list[str] = []

        class ConnFalsa:
            def execute(self, sentencia, *a, **kw):
                vistas.append(str(sentencia))

                class R:
                    def one(self_inner):
                        return (0, None)

                return R()

        assert cohorte.validar(ConnFalsa()) == []
        return vistas

    def test_valida_la_edad_y_el_veredicto(self, cohorte):
        """Si la edad o el cierre están mal, la muestra no significa nada."""
        todo = " ".join(self._consultas(cohorte))
        assert "edad_anios < 0" in todo
        assert "cerrado <> (cortes_tras_ultimo >=" in todo
        assert "cortes_tras_ultimo = 0 AND cerrado" in todo

    def test_valida_la_coherencia_de_la_ventana(self, cohorte):
        todo = " ".join(self._consultas(cohorte))
        assert "ventana_cortes <>" in todo
        assert "cardinality(periodos) <> cortes_observados" in todo

    def test_rechaza_identidades_sinteticas(self, cohorte):
        """Redundante por construcción, y por eso atrapa un error de alcance.

        Solo la fase 2 de propagación crea `NEW_*`; una aquí querría decir
        que alguien amplió la cohorte sin darse cuenta.
        """
        todo = " ".join(self._consultas(cohorte))
        assert "STARTS_WITH(clee, 'NEW_')" in todo
        # Con LIKE, el `_` de NEW_ seria un comodin.
        assert "LIKE 'NEW" not in todo

    def test_valida_que_el_nombre_tenga_origen(self, cohorte):
        todo = " ".join(self._consultas(cohorte))
        assert "nombre IS NOT NULL AND nombre_origen IS NULL" in todo
        assert "cluster_sospechoso AND nombre IS NOT NULL" in todo

    def test_reporta_todas_las_violaciones_no_la_primera(self, cohorte):
        """Con una sola, cada arreglo cuesta otra corrida de horas."""

        class ConnRota:
            def execute(self, sentencia, *a, **kw):
                class R:
                    def one(self_inner):
                        return (3, "14ABC0001")

                return R()

        violaciones = cohorte.validar(ConnRota())
        assert len(violaciones) == 10
        assert all("14ABC0001" in v for v in violaciones)


@pytest.mark.unit
class TestTroceoYMeta:
    def test_ningun_paso_lee_la_tabla_de_trabajo_completa(self, pasos, cohorte):
        """Si a uno se le olvidara el filtro, agregaría los 51.5M."""
        for paso in pasos:
            if cohorte.obs in paso.sql:
                assert "bloque = 7" in paso.sql, paso.nombre

    def test_las_temporales_mueren_con_la_transaccion(self, pasos):
        for paso in pasos:
            if paso.nombre == TABLA:
                continue
            assert "ON COMMIT DROP" in paso.sql, paso.nombre

    def test_la_tabla_de_trabajo_es_unlogged(self, cohorte):
        """Una temporal moriría en el primer COMMIT, y el troceo commitea."""
        ddl = cohorte.sql_crear_obs()
        assert "UNLOGGED" in ddl
        assert "CREATE TEMP" not in ddl

    def test_el_insert_cuadra_columnas_con_el_modelo(self, madre):
        """Agregar una columna al ORM y olvidarla en el INSERT.

        Es el error que, sin esto, se descubre nueve cortes y varias horas
        más tarde.
        """
        bloque = madre[madre.index("(") + 1 : madre.index(")\nSELECT")]
        escritas = {c.strip() for c in bloque.replace("\n", "").split(",") if c.strip()}
        del_modelo = {c.name for c in get_table(TABLA, schema="gold").columns}
        assert escritas == del_modelo - {"construida_en"}

    def test_el_contexto_de_meta_lleva_la_receta(self, cohorte):
        """ "¿Por qué marzo dio otra tasa de cierre?" se contesta con SQL."""
        contexto = cohorte.contexto_meta(conn=None)
        assert contexto["n_cortes_oficiales"] == 9
        assert contexto["ventana_cortes"] == [c.isoformat() for c in VENTANA]
        assert "cierre_cortes_ausente" in contexto
        assert "version_reglas" in contexto

    def test_el_contexto_es_serializable_a_json(self, cohorte):
        import json

        json.dumps(cohorte.contexto_meta(conn=None))


@pytest.mark.unit
class TestLaSeccionDelCLI:
    def test_gold_es_una_seccion_atomica(self):
        from src.shared.enums import DENUE_ATOMIC, Section

        assert Section.gold in DENUE_ATOMIC

    def test_pedir_gold_no_arrastra_el_ingest_ni_la_resolucion(self):
        from src.etl.main import _expand_only
        from src.shared.enums import Section

        assert _expand_only([Section.gold]) == {Section.gold}

    def test_gold_va_despues_de_permanencia(self, monkeypatch):
        """Una Gold recién hecha sobre Silver a medias mentiría."""
        import src.etl.main as main

        orden: list[str] = []
        monkeypatch.setattr(main, "_run_permanencia", lambda **kw: orden.append("perm") or True)
        monkeypatch.setattr(main, "_run_gold", lambda **kw: orden.append("gold") or True)

        main._run_denue_transform(
            ingest=False,
            h3_cell_7=False,
            clee_resolution_radio=False,
            clee_resolution_h3=False,
            clee_resolution_celda_h3_7=False,
            permanencia_radio=True,
            permanencia_h3=False,
            gold=True,
            force=False,
        )
        assert orden == ["perm", "gold"]


@pytest.mark.unit
class TestLaAusenciaEnMeses:
    """Contar cortes no es contar tiempo, y la ventana lo demuestra."""

    def test_los_intervalos_de_la_ventana_son_irregulares(self):
        """6, 6, 6, 12, 6, 6, 6, 12 meses: faltan 2023-05 y 2025-11.

        Por eso `cortes_tras_ultimo = 1` significa 12 meses de evidencia para
        quien desapareció en 2022-11 y solo 6 para quien desapareció en
        2024-05. El test está aquí para que si la ventana se vuelve regular
        alguien note que esta columna dejó de hacer falta.
        """
        meses = [
            (b.year - a.year) * 12 + (b.month - a.month)
            # Los largos difieren a proposito: son pares consecutivos.
            for a, b in zip(VENTANA, VENTANA[1:], strict=False)
        ]
        assert meses == [6, 6, 6, 12, 6, 6, 6, 12]
        assert len(set(meses)) > 1

    def test_se_mide_contra_el_ultimo_corte_de_la_ventana(self, madre):
        assert f"DATE '{VENTANA[-1].isoformat()}'" in madre
        assert "AS meses_sin_aparecer" in madre

    def test_es_una_diferencia_de_calendario_no_de_dias(self, madre):
        """Los cortes caen el día 1, así que la diferencia en meses es exacta.

        Dividir días entre 30 daría 5.9 o 6.1 meses según el mes, y eso
        volvería ruidoso un eje que debería ser limpio.
        """
        assert "EXTRACT(YEAR FROM" in madre
        assert "EXTRACT(MONTH FROM" in madre
        assert "/ 30" not in madre


@pytest.mark.unit
class TestElLocalReocupado:
    """Evidencia corroborante del cierre, no prueba."""

    def test_solo_se_evalua_para_los_cerrados(self, pasos, madre):
        """Para un activo la pregunta no significa nada.

        El vecino existe y los dos siguen abiertos; evaluarlo costaría una
        búsqueda por cada uno de los 9.58M.
        """
        reocupado = next(p.sql for p in pasos if p.nombre == "reocupado")
        assert "v.idx_ultimo) >=" in reocupado
        assert "ELSE NULL END AS local_reocupado" in madre

    def test_busca_otro_clee_en_un_corte_posterior(self, pasos):
        reocupado = next(p.sql for p in pasos if p.nombre == "reocupado")
        assert "e.period > v.ultimo_periodo" in reocupado
        assert "e.clee <> u.clee" in reocupado

    def test_es_un_semijoin_y_no_un_conteo(self, pasos):
        """En una celda urbana densa hay cientos de vecinos.

        `EXISTS` se detiene en el primero; contar los recorrería todos sin
        que el resultado cambie.
        """
        reocupado = next(p.sql for p in pasos if p.nombre == "reocupado")
        assert "EXISTS (" in reocupado
        assert "count(" not in reocupado.lower()

    def test_entra_por_la_celda_h3_que_si_esta_indexada(self, pasos):
        """`ix_estab_h3_9` existe en silver; sin él esto sería un scan."""
        from src.shared.db.base import get_table

        reocupado = next(p.sql for p in pasos if p.nombre == "reocupado")
        assert "e.h3_cell_9 = u.h3_cell_9" in reocupado
        indices = {i.name for i in get_table("establecimientos").indexes}
        assert "ix_estab_h3_9" in indices

    def test_es_el_unico_paso_que_sale_de_la_tabla_de_trabajo(self, pasos, cohorte):
        """Y por eso es el candidato a sorprender en rendimiento.

        Si el bloque 1 tarda mucho más que antes, este paso es el sospechoso.
        """
        # `FROM` y no el nombre a secas: el paso `ventana` MENCIONA la tabla
        # en un comentario, y matchear eso daba un falso positivo.
        fuera = [p.nombre for p in pasos if "FROM silver.establecimientos" in p.sql]
        assert fuera == ["reocupado"]
