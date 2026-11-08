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
        assert "CASE WHEN v.n_nombres > 9 THEN NULL ELSE nom.valor END AS nombre" in sql
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
        sql = next(p.sql for p in pasos if p.nombre == TABLA)
        assert "ORDER BY n_cortes DESC, ultimo_idx DESC, valor" in sql

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

    def test_la_variante_de_nom_estab_se_compara_contra_la_columna_nombre(self, pasos):
        """Es el unico campo donde la columna canonica se llama distinto."""
        sql = next(p.sql for p in pasos if p.nombre == TABLA_VARIANTES)
        assert "va.campo = 'nom_estab' THEN va.valor IS NOT DISTINCT FROM n.nombre" in sql
        assert "n.nom_estab" not in sql

    def test_se_listan_exactamente_los_campos_documentados(self, pasos):
        """Agregar uno es una decision de diseño (§6.1), no un descuido."""
        variantes = next(p for p in pasos if p.nombre == "variantes")
        for campo in CAMPOS_CON_VARIANTES:
            assert f"'{campo}' AS campo" in variantes.sql
        assert variantes.sql.count("UNION ALL") == len(CAMPOS_CON_VARIANTES) - 1


@pytest.mark.unit
class TestTroceoYEscritura:
    def test_ningun_paso_intermedio_lee_la_tabla_completa(self, pasos):
        """Si a uno se le olvidara el filtro, agregaria los 51.5M.

        Y peor que lento: los conteos saldrían del universo entero y no del
        bloque.
        """
        for paso in pasos:
            if paso.nombre in (TABLA, TABLA_VARIANTES):
                continue
            assert "bloque = 7" in paso.sql, paso.nombre

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
