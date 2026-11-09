"""El SQL de la cohorte oficial: lo que afirma y lo que se niega a afirmar.

Sin base de datos. Lo que se revisa aquí son las decisiones que, si se
rompen, producen una tabla que **parece** correcta: un domicilio armado por
partes, una ventana heredada de la secuencia de 20 cortes, un nombre
canónico inventado para un cluster que agrupó dos negocios. Ninguna de esas
levanta un error en tiempo de ejecución — y por eso tienen test.
"""

from datetime import date

import pytest

from src.etl.loaders.gold_cohorte_oficial import (
    COLUMNA_CANONICA,
    METODO_BASE,
    TABLA,
    TABLA_VARIANTES,
    CohorteOficial,
)
from src.shared.db.base import get_table
from src.shared.db.models.gold import CAMPOS_CON_VARIANTES

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


@pytest.mark.unit
class TestLaCohorteNoEsSilverFiltrado:
    """La diferencia que justifica que Gold recalcule en vez de copiar."""

    def test_no_lee_ninguna_tabla_de_identidad(self, cohorte):
        """La identidad es el `clee` de los hechos, no un `clee_resuelto`.

        Si este SQL uniera `clee_radio`, Gold heredaría el error de
        propagación que la cohorte oficial existe para no tener.
        """
        sql = cohorte.sql_llenar_obs(VENTANA[0], 0, 32)
        assert "clee_radio" not in sql
        assert "clee_h3" not in sql
        assert "clee_resuelto" not in sql
        assert "e.clee IS NOT NULL" in sql

    def test_solo_entran_observaciones_con_clee(self, cohorte):
        """Una fila sin `clee` en un corte oficial no tiene identidad observada.

        Es de la cohorte y no una optimización: ese <=0.1% que el umbral
        tolera no puede entrar, y el negocio sí entra si aparece con `clee` en
        otro corte oficial (`gold_cohorte_2021.md` §2).
        """
        sql = cohorte.sql_llenar_obs(VENTANA[3], 3, 32)
        assert "e.clee <> ''" in sql

    def test_el_metodo_base_no_es_un_metodo_de_propagacion(self):
        """Decir `radio` seria falso justo en la columna que dice de donde vino."""
        assert METODO_BASE not in {"radio", "h3", "celda_h3_7"}

    def test_la_llave_del_destino_se_llama_clee(self, cohorte):
        """`clee_resuelto` seria mentira: aqui no se resolvio nada."""
        assert cohorte.llave_destino == "clee"


@pytest.mark.unit
class TestLaVentanaEsLaOficial:
    """Lo que hace que `idx_primer = 0` signifique algo distinto que en Silver."""

    def test_el_idx_es_la_posicion_en_la_ventana_oficial(self, cohorte):
        """El corte 2022-05 es el idx 2 de la ventana, no el 13 de los 20.

        Heredar los índices de la secuencia completa rompería
        `presente_desde_el_inicio` y `duracion_periodos` sin un solo error
        visible.
        """
        sql = cohorte.sql_llenar_obs(VENTANA[2], 2, 32)
        assert "    2,\n" in sql

    def test_la_ventana_queda_grabada_en_cada_fila(self, cohorte, pasos):
        """Sin esto la fila no es citable: la N cambia y nada lo explica."""
        sql = next(p.sql for p in pasos if p.nombre == TABLA)
        for corte in VENTANA:
            assert f"DATE '{corte.isoformat()}'" in sql
        assert "::date[]" in sql

    def test_la_censura_se_mide_contra_el_ultimo_corte_oficial(self, cohorte, pasos):
        """`cortes_tras_ultimo` con el idx 8, que es el noveno corte."""
        sql = next(p.sql for p in pasos if p.nombre == TABLA)
        assert "8 - v.idx_ultimo AS cortes_tras_ultimo" in sql

    def test_la_huella_es_la_ventana_y_no_una_fecha(self, cohorte):
        """Un corte puede ENTRAR a la ventana y tambien salir de ella.

        Con un `>` sobre timestamps solo se vería uno de los dos cambios, y
        una tabla construida con otra ventana se seguiría sirviendo.
        """
        huella = cohorte.huella_origen(conn=None)
        assert huella.startswith("ventana=")
        assert "2021-05-01" in huella and "2026-05-01" in huella
        assert "resuelto_en" not in huella


@pytest.mark.unit
class TestCoherenciaDelDomicilio:
    """`normalizacion_gold.md` §5: un domicilio no se arma por partes."""

    def test_el_bloque_de_ubicacion_sale_de_una_sola_observacion(self, pasos):
        """`DISTINCT ON` y no un agregado por campo.

        Tomar el CP del corte más reciente que lo tenga, la colonia de otro y
        las coordenadas del último produce un domicilio que nunca existió.
        """
        referencia = next(p for p in pasos if p.nombre == "referencia")
        assert "DISTINCT ON (clee)" in referencia.sql
        assert "ORDER BY clee, period DESC, id" in referencia.sql
        for campo in ("cve_ent", "cve_mun", "cve_loc", "ageb", "cod_postal", "tipo_asent"):
            assert campo in referencia.sql

    def test_las_llaves_geograficas_salen_del_corte_de_referencia(self, pasos):
        """De `r.`, nunca de un agregado: es lo que las mantiene coherentes."""
        sql = next(p.sql for p in pasos if p.nombre == TABLA)
        for campo in ("cve_ent", "cve_mun", "cve_loc", "ageb", "cod_postal", "tipo_asent"):
            assert f"r.{campo}" in sql

    def test_el_corte_de_referencia_se_materializa(self, pasos):
        """Es lo que vuelve verificable la coherencia desde fuera."""
        sql = next(p.sql for p in pasos if p.nombre == TABLA)
        assert "r.corte_referencia" in sql

    def test_el_contacto_es_la_excepcion_deliberada(self, pasos):
        """Ahi no se describe un lugar, se acumula una via de verificacion.

        Y es "el más reciente NO VACÍO", no "el del último corte": el `FILTER`
        es lo que hace la diferencia entre acumular cobertura y perderla.
        """
        ventana = next(p for p in pasos if p.nombre == "ventana")
        for campo in ("telefono", "correoelec", "www", "raz_social"):
            assert f"ARRAY_AGG({campo} ORDER BY idx DESC) FILTER (" in ventana.sql
        sql = next(p.sql for p in pasos if p.nombre == TABLA)
        for campo in ("telefono", "correoelec", "www"):
            assert f"v.{campo}" in sql


@pytest.mark.unit
class TestLoQueSeNiegaAAfirmar:
    """Donde el valor correcto es NULL, y no un valor plausible."""

    def test_un_cluster_sospechoso_no_recibe_nombre(self, pasos):
        """Con [TACOS PACO, FARMACIA PACO] cualquier canonico es falso.

        Más nombres que cortes es IMPOSIBLE para un cluster correcto --un
        establecimiento aparece a lo más una vez por corte-- así que ahí la
        identidad agrupó más de un negocio y no hay nombre que lo represente.
        """
        sql = next(p.sql for p in pasos if p.nombre == TABLA)
        assert "CASE WHEN v.n_nombres > 9 THEN NULL ELSE nom.nombre END AS nombre" in sql
        assert "v.n_nombres > 9 AS cluster_sospechoso" in sql

    def test_el_scian_queda_null_si_la_discrepancia_cruza_rama(self, pasos):
        """Ahi hay que juzgar, y un valor inventado es peor que la ausencia.

        La jerarquía ES el código (sector=2, subsector=3, rama=4), así que el
        ancestro común sale por prefijo sin unir el catálogo.
        """
        sql = next(p.sql for p in pasos if p.nombre == TABLA)
        assert "LEFT(v.act_min, 5) = LEFT(v.act_max, 5)" in sql
        assert "LEFT(v.act_min, 4) = LEFT(v.act_max, 4)" in sql
        # Sin un quinto nivel: por encima de rama ya no se decide aqui.
        assert "LEFT(v.act_min, 3)" not in sql
        assert "LEFT(v.act_min, 2)" not in sql

    def test_el_ancestro_se_calcula_con_los_extremos(self, pasos):
        """El prefijo comun de un conjunto ordenado es el del minimo y el maximo.

        Dos escalares en vez de materializar el conjunto por negocio.
        """
        ventana = next(p for p in pasos if p.nombre == "ventana")
        assert "MIN(codigo_act) FILTER (" in ventana.sql
        assert "MAX(codigo_act) FILTER (" in ventana.sql

    def test_no_se_materializa_el_veredicto_de_cierre(self):
        """La regla sigue siendo preliminar: la tabla da ingredientes."""
        columnas = {c.name for c in get_table(TABLA, schema="gold").columns}
        assert not columnas & {"cerrado", "estatus"}


@pytest.mark.unit
class TestElNombreCanonicoYSuEvidencia:
    """§6.4: el baseline obligatorio, y que la evidencia no pueda discrepar."""

    def test_el_nombre_es_la_moda_desempatada_por_el_mas_reciente(self, pasos):
        """Baseline 1 de §6.4, que el LLM solo reemplaza si GANA."""
        sql = next(p.sql for p in pasos if p.nombre == "canonico")
        assert "ORDER BY clee, n_cortes DESC, ultimo_idx DESC, valor" in sql

    def test_la_moda_sale_de_una_pasada_y_no_de_una_lateral(self, pasos):
        """Es una diferencia de complejidad, no de estilo.

        Un `LEFT JOIN LATERAL` con `LIMIT 1` fuerza un nested loop: una
        busqueda por identidad, cada una recorriendo la temporal completa
        porque no tiene indice. Medido: dejo el INSERT del bloque 1 corriendo
        21 minutos sin terminar (~11 horas proyectadas para los 32).
        """
        canonico = next(p.sql for p in pasos if p.nombre == "canonico")
        assert "DISTINCT ON (clee)" in canonico

        madre = next(p.sql for p in pasos if p.nombre == TABLA)
        assert "LATERAL" not in madre
        assert "LEFT JOIN canonico nom USING (clee)" in madre

    def test_el_desempate_final_hace_reproducible_la_eleccion(self, pasos):
        """Sin el `valor`, dos variantes empatadas dependerian del plan."""
        canonico = next(p.sql for p in pasos if p.nombre == "canonico")
        assert canonico.rstrip().endswith("valor")

    def test_la_moda_necesita_las_variantes_antes(self, pasos):
        """De ahi el orden de los pasos: el conteo por valor es su insumo."""
        orden = [p.nombre for p in pasos]
        assert orden.index("variantes") < orden.index(TABLA)

    def test_es_canonico_se_lee_de_la_madre_no_se_recalcula(self, pasos):
        """Recalcular la regla dejaria que las dos copias discreparan.

        Si la hija dedujera su propio canónico, un cambio en una de las dos
        dejaría la tabla afirmando que el canónico es un valor que la madre
        nunca publicó. Comparando contra la fila escrita, no pueden discrepar.
        """
        sql = next(p.sql for p in pasos if p.nombre == TABLA_VARIANTES)
        assert f"JOIN {'gold'}.{TABLA} n ON n.clee = va.clee" in sql
        assert "IS NOT DISTINCT FROM n.nombre" in sql

    def test_el_mapa_apunta_a_columnas_que_existen(self):
        """Cada columna canonica tiene que existir en la tabla madre.

        Esto es lo que faltaba: el mapa se derivaba con una comprension que
        trataba `nom_estab -> nombre` como la UNICA excepcion, y generaba
        `n.raz_social`, que no existe (la columna es `razon_social`). El SQL
        era valido como cadena y solo truena al ejecutarse, asi que ningun
        test de forma lo veia.
        """
        del_modelo = {c.name for c in get_table(TABLA, schema="gold").columns}
        faltan = {campo: col for campo, col in COLUMNA_CANONICA.items() if col not in del_modelo}
        assert not faltan, f"columnas canonicas inexistentes: {faltan}"

    def test_el_mapa_cubre_todos_los_campos_con_variantes(self):
        """Un campo sin entrada en el mapa levantaria KeyError al construir."""
        assert set(COLUMNA_CANONICA) == set(CAMPOS_CON_VARIANTES)

    def test_las_dos_excepciones_de_nombre_estan(self, pasos):
        """`nom_estab -> nombre` y `raz_social -> razon_social`.

        Son dos y no una: en la madre esas columnas guardan el valor
        CANONICO, no el campo crudo del DENUE, y por eso se llaman distinto.
        """
        sql = next(p.sql for p in pasos if p.nombre == TABLA_VARIANTES)
        assert "va.campo = 'nom_estab' THEN va.valor IS NOT DISTINCT FROM n.nombre" in sql
        assert "va.campo = 'raz_social' THEN va.valor IS NOT DISTINCT FROM n.razon_social" in sql
        assert "n.nom_estab" not in sql
        assert "n.raz_social " not in sql

    def test_se_listan_exactamente_los_campos_documentados(self, pasos):
        """Agregar uno es una decision de diseño (§6.1), no un descuido."""
        variantes = next(p for p in pasos if p.nombre == "variantes")
        for campo in CAMPOS_CON_VARIANTES:
            assert f"'{campo}' AS campo" in variantes.sql
        assert variantes.sql.count("UNION ALL") == len(CAMPOS_CON_VARIANTES) - 1


@pytest.mark.unit
class TestTroceoYEscritura:
    def test_ningun_paso_lee_la_tabla_de_trabajo_completa(self, pasos):
        """Si a uno se le olvidara el filtro, agregaria los 51.5M.

        Y peor que lento: los conteos saldrían del universo entero y no del
        bloque. El filtro se exige de los pasos que leen la tabla de trabajo;
        `canonico` no la toca --lee `variantes`, que ya viene acotada-- y los
        dos `INSERT` leen solo temporales.
        """
        for paso in pasos:
            if self.obs_nombre not in paso.sql:
                continue
            assert "bloque = 7" in paso.sql, paso.nombre

    #: La tabla de trabajo, para reconocer qué pasos la leen.
    obs_nombre = "negocios_clee_oficial_obs"

    def test_los_pasos_que_derivan_leen_solo_temporales(self, pasos):
        """`canonico` y los dos INSERT no vuelven a la tabla de trabajo.

        Es lo que vuelve baratos los pasos finales: la tabla de trabajo tiene
        51.5M filas y las temporales del bloque unas cientos de miles.
        """
        derivados = ("canonico", TABLA, TABLA_VARIANTES)
        for paso in pasos:
            if paso.nombre in derivados:
                assert self.obs_nombre not in paso.sql, paso.nombre

    def test_las_temporales_mueren_con_la_transaccion(self, pasos):
        """`ON COMMIT DROP`: un bloque que falla no deja basura."""
        for paso in pasos:
            if paso.nombre in (TABLA, TABLA_VARIANTES):
                continue
            assert "ON COMMIT DROP" in paso.sql, paso.nombre

    def test_la_madre_se_escribe_antes_que_la_hija(self, pasos):
        """La FK lo exige, y van en la misma transaccion del bloque."""
        orden = [p.nombre for p in pasos]
        assert orden.index(TABLA) < orden.index(TABLA_VARIANTES)
        assert orden[-1] == TABLA_VARIANTES

    def test_las_dos_tablas_se_truncan_juntas(self, cohorte):
        """PostgreSQL rechaza truncar una tabla referenciada por una FK sola."""
        assert cohorte.tablas_destino == (TABLA, TABLA_VARIANTES)

    def test_la_tabla_de_trabajo_es_unlogged(self, cohorte):
        """Una temporal moriria en el primer COMMIT, y el troceo commitea."""
        ddl = cohorte.sql_crear_obs()
        assert "UNLOGGED" in ddl
        assert "CREATE TEMP" not in ddl

    def test_la_tabla_de_trabajo_no_lleva_columna_origen(self, cohorte):
        """Seria una constante en 51.5M filas: aqui todo es DIRECTO."""
        assert "origen" not in cohorte.sql_crear_obs()

    @pytest.mark.parametrize("tabla", [TABLA, TABLA_VARIANTES])
    def test_el_insert_cuadra_columnas_con_el_modelo(self, pasos, tabla):
        """Agregar una columna al ORM y olvidarla en el INSERT.

        Es el error que, sin esto, se descubre nueve cortes y varias horas
        más tarde, cuando corre el primer `INSERT` de verdad.
        """
        sql = next(p.sql for p in pasos if p.nombre == tabla)
        bloque = sql[sql.index("(") + 1 : sql.index(")\nSELECT")]
        escritas = {c.strip() for c in bloque.replace("\n", "").split(",") if c.strip()}

        # `construida_en` la pone la base (server_default).
        del_modelo = {c.name for c in get_table(tabla, schema="gold").columns}
        assert escritas == del_modelo - {"construida_en"}


@pytest.mark.unit
class TestJitterDeGPS:
    """La unica excepcion a "todo del corte de referencia", y por que es segura."""

    def test_la_mediana_solo_entra_con_dispersion_alta_y_neto_bajo(self, pasos):
        """Dispersion grande SIN desplazamiento es jitter, no una mudanza.

        Las dos condiciones juntas son lo que separa un GPS ruidoso de un
        negocio que se movió; con solo la dispersión, una mudanza real
        acabaría con las coordenadas en el punto medio del camino.
        """
        sql = next(p.sql for p in pasos if p.nombre == TABLA)
        assert "THEN v.lat_mediana ELSE r.latitud END AS latitud" in sql
        assert "THEN v.lon_mediana ELSE r.longitud END AS longitud" in sql

    def test_las_llaves_geograficas_no_se_mueven_con_el_jitter(self, pasos):
        """Cambiarlas rompería la coherencia que la regla protege.

        La mediana corrige un punto malo; no autoriza a recombinar el
        domicilio.
        """
        sql = next(p.sql for p in pasos if p.nombre == TABLA)
        assert "mediana ELSE r.cve_mun" not in sql
        assert "r.h3_cell_9," in sql


@pytest.mark.unit
class TestLoQueQuedaEnMeta:
    """El detalle no puede vivir solo en el log: tiene que ser consultable.

    Un log se rota, se trunca y no se puede unir con nada. `meta` contesta
    "¿cuanto tardo?", "¿termino bien?" y --con esto-- "¿con que ventana se
    construyo?" con una consulta.
    """

    def test_la_ventana_queda_en_el_config_de_la_corrida(self, cohorte):
        """`gold_cohorte_2021.md` §1.1 lo pide: en el log Y en meta.

        Sin esto, "¿por qué la corrida de marzo dio 5.1M y la de abril 5.4M?"
        solo se contesta abriendo dos logs, cuando la respuesta es que entró
        un corte.
        """
        contexto = cohorte.contexto_meta(conn=None)
        assert contexto["ventana_cortes"] == [c.isoformat() for c in VENTANA]
        assert contexto["n_cortes_oficiales"] == 9

    def test_el_config_lleva_las_reglas_con_que_se_construyo(self, cohorte):
        """Cambiar una regla cambia los numeros: la corrida tiene que decir cuales."""
        contexto = cohorte.contexto_meta(conn=None)
        assert contexto["metodo_base"] == METODO_BASE
        assert "version_reglas" in contexto
        assert "cobertura_clee_minima" in contexto
        # Los umbrales del jitter deciden de que corte salen las coordenadas,
        # asi que son parte de la receta y no un detalle de ejecucion.
        assert "jitter_dispersion_m" in contexto
        assert "jitter_neto_m" in contexto

    def test_el_contexto_es_serializable_a_json(self, cohorte):
        """`meta.job_runs.config` es JSON: un `date` suelto lo rompe."""
        import json

        json.dumps(cohorte.contexto_meta(conn=None))

    def test_la_tabla_de_variantes_tambien_se_registra(self, cohorte):
        """Una tarea de `meta.process_tasks` por tabla destino, no solo la madre.

        Sin esto el conteo de la evidencia no queda en ningun lado
        consultable: habria que contar a mano para saber cuantas variantes
        produjo la corrida.
        """
        assert TABLA_VARIANTES in cohorte.tablas_destino


@pytest.mark.unit
class TestLaSeccionDelCLI:
    """`--only gold` tiene que aislar Gold sin arrastrar las horas de Silver."""

    def test_gold_es_una_seccion_atomica(self):
        from src.shared.enums import DENUE_ATOMIC, Section

        assert Section.gold in DENUE_ATOMIC

    def test_pedir_gold_no_arrastra_el_ingest_ni_la_resolucion(self):
        """Es la diferencia entre minutos y un dia de computo."""
        from src.etl.main import _expand_only
        from src.shared.enums import Section

        assert _expand_only([Section.gold]) == {Section.gold}

    def test_el_grupo_denue_la_incluye(self):
        """`--only denue` construye todo DENUE, Gold incluido."""
        from src.etl.main import _expand_only
        from src.shared.enums import Section

        assert Section.gold in _expand_only([Section.denue])

    def test_gold_va_despues_de_permanencia(self, monkeypatch):
        """Es la capa de consumo: se construye sobre lo que Silver ya escribio.

        El orden no es por dependencia de datos --Gold no lee permanencia--
        sino porque un fallo en Silver no debe dejar una Gold recien hecha
        sobre hechos a medio cargar.
        """
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

    def test_un_fallo_en_permanencia_no_construye_gold(self, monkeypatch):
        """Gold sobre Silver a medias seria una tabla citable que miente."""
        import src.etl.main as main

        corrio_gold = []
        monkeypatch.setattr(main, "_run_permanencia", lambda **kw: False)
        monkeypatch.setattr(main, "_run_gold", lambda **kw: corrio_gold.append(1) or True)

        ok = main._run_denue_transform(
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
        assert not ok
        assert corrio_gold == []

    def test_force_llega_hasta_el_loader(self, monkeypatch):
        import src.etl.main as main

        vistos = []
        monkeypatch.setattr(
            "src.etl.loaders.gold_cohorte_oficial.construir_cohorte_oficial",
            lambda engine, tracker, force=False: (vistos.append(force), 1)[1],
        )
        monkeypatch.setattr("src.shared.db.session.get_engine", lambda: "engine")
        monkeypatch.setattr("src.meta.tracker.MetadataTracker", lambda: "tracker")

        main._run_gold(force=True)
        assert vistos == [True]


@pytest.mark.unit
class TestLosInvariantes:
    """§5.1: lo que, si no se cumple, debe FALLAR la corrida.

    Aquí se revisa que los chequeos existan y qué miran; que detecten de
    verdad se prueba contra PostgreSQL, porque es SQL.
    """

    def _consultas(self, cohorte, monkeypatch):
        """Las consultas que `validar` corre, sin ejecutarlas.

        Se intercepta `conn.execute` y se devuelve "0 violaciones" para cada
        una, de modo que el método recorra su lista completa.
        """
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

    def test_los_siete_chequeos_estan(self, cohorte, monkeypatch):
        """Seis consultas; el septimo de §5.1 lo garantiza el esquema."""
        consultas = self._consultas(cohorte, monkeypatch)
        assert len(consultas) == 7

    def test_rechaza_identidades_sinteticas(self, cohorte, monkeypatch):
        """Redundante por construccion, y por eso atrapa un error de alcance.

        Solo la fase 2 de propagación crea `NEW_*`; si una apareciera aquí
        querría decir que alguien amplió la cohorte sin darse cuenta.
        """
        todo = " ".join(self._consultas(cohorte, monkeypatch))
        assert "STARTS_WITH(clee, 'NEW_')" in todo
        # Con LIKE, el `_` de NEW_ seria un comodin y el chequeo seria mas
        # laxo de lo que dice ser.
        assert "LIKE 'NEW" not in todo

    def test_rechaza_una_ventana_que_no_es_la_de_la_corrida(self, cohorte, monkeypatch):
        """Mezclar dos ventanas en la misma tabla hace incomparables sus filas."""
        todo = " ".join(self._consultas(cohorte, monkeypatch))
        assert "ventana_cortes <>" in todo

    def test_rechaza_dos_canonicos_para_el_mismo_campo(self, cohorte, monkeypatch):
        """El indice unico parcial ya lo impide; esto lo confirma sobre el dato."""
        todo = " ".join(self._consultas(cohorte, monkeypatch))
        assert "WHERE es_canonico" in todo
        assert "HAVING count(*) > 1" in todo

    def test_rechaza_un_nombre_sin_su_evidencia(self, cohorte, monkeypatch):
        """Es lo que vuelve verificable la canonicalizacion sin re-derivarla."""
        todo = " ".join(self._consultas(cohorte, monkeypatch))
        assert "va.valor = n.nombre" in todo

    def test_rechaza_variantes_huerfanas(self, cohorte, monkeypatch):
        todo = " ".join(self._consultas(cohorte, monkeypatch))
        assert "WHERE n.clee IS NULL" in todo

    def test_reporta_todas_las_violaciones_no_la_primera(self, cohorte):
        """Con una sola, cada arreglo cuesta otra corrida de horas."""

        class ConnRota:
            def execute(self, sentencia, *a, **kw):
                class R:
                    def one(self_inner):
                        return (3, "14ABC0001")

                return R()

        violaciones = cohorte.validar(ConnRota())
        assert len(violaciones) == 7
        assert all("14ABC0001" in v for v in violaciones)
