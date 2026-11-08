"""Selección de método en la construcción de permanencia.

Aparte de `test_permanencia.py` porque ese archivo está marcado
`postgres` entero --construye las tablas de verdad-- y esto solo lee la
tabla de secciones del CLI.
"""

import pytest


@pytest.mark.unit
class TestSeleccionPorMetodo:
    """Cada método se puede construir por separado, como `clee-radio`/`clee-h3`.

    Sin esto, pedir la permanencia de un método obligaría a reconstruir la
    del otro -- un `GROUP BY` de minutos sobre 82.9M filas que nadie pidió.
    """

    def test_cada_metodo_es_una_seccion_atomica(self):
        from src.shared.enums import DENUE_ATOMIC, Section

        assert Section.permanencia_radio in DENUE_ATOMIC
        assert Section.permanencia_h3 in DENUE_ATOMIC

    def test_el_grupo_expande_a_los_dos(self):
        from src.etl.main import _expand_only
        from src.shared.enums import Section

        assert _expand_only([Section.permanencia]) == {
            Section.permanencia_radio,
            Section.permanencia_h3,
        }

    def test_pedir_uno_no_arrastra_al_otro(self):
        from src.etl.main import _expand_only
        from src.shared.enums import Section

        assert _expand_only([Section.permanencia_radio]) == {Section.permanencia_radio}


@pytest.mark.unit
def test_solo_construye_los_metodos_pedidos(monkeypatch):
    """La seleccion tiene que llegar hasta el loader, no solo al enum.

    Es la diferencia entre `--no-permanencia-h3` y reconstruir las dos
    tablas: la segunda cuesta recorrer los 82.9M hechos otra vez.
    """
    import src.etl.main as main
    from src.etl.loaders import permanencia as loader

    construidos: list[str] = []
    monkeypatch.setattr("src.shared.db.session.get_engine", lambda: "engine")
    monkeypatch.setattr(
        loader,
        "construir_permanencia",
        lambda engine, metodo, tracker, force=False: (construidos.append(metodo), 1)[1],
    )

    main._run_permanencia(metodos=["h3"])
    assert construidos == ["h3"]


@pytest.mark.unit
def test_el_loader_recibe_el_engine_no_una_conexion(monkeypatch):
    """Cada metodo controla sus propias transacciones, una por vuelta.

    Antes el CLI abria UNA transaccion por metodo y se la pasaba al loader,
    asi que todo el trabajo era un solo commit: un fallo a la mitad lo
    revertia entero. Ahora el loader recibe el engine y commitea cada corte
    y cada bloque, que es lo que hace la corrida reanudable -- y por eso el
    CLI ya no puede ser quien abra la transaccion.
    """
    import src.etl.main as main
    from src.etl.loaders import permanencia as loader

    recibidos = []
    monkeypatch.setattr("src.shared.db.session.get_engine", lambda: "engine")
    monkeypatch.setattr(
        loader,
        "construir_permanencia",
        lambda engine, metodo, tracker, force=False: (recibidos.append((engine, metodo)), 1)[1],
    )

    main._run_permanencia(metodos=["radio", "h3"])
    assert recibidos == [("engine", "radio"), ("engine", "h3")]


@pytest.mark.unit
def test_force_llega_hasta_el_loader(monkeypatch):
    """Sin esto, `--force` no distinguiria rehacer de reanudar."""
    import src.etl.main as main
    from src.etl.loaders import permanencia as loader

    vistos = []
    monkeypatch.setattr("src.shared.db.session.get_engine", lambda: "engine")
    monkeypatch.setattr(
        loader,
        "construir_permanencia",
        lambda engine, metodo, tracker, force=False: (vistos.append(force), 1)[1],
    )

    main._run_permanencia(metodos=["radio"], force=True)
    assert vistos == [True]


@pytest.mark.unit
def test_los_dos_metodos_comparten_el_tracker(monkeypatch):
    """La construccion se registra en `meta`, no solo en el log del stage.

    Un tracker para los dos metodos, pero `construir_permanencia` abre una
    corrida por metodo: lo que se consulta despues es "como termino
    permanencia_h3", no un agregado de las dos.
    """
    import src.etl.main as main
    from src.etl.loaders import permanencia as loader
    from src.meta.tracker import MetadataTracker

    trackers = []
    monkeypatch.setattr("src.shared.db.session.get_engine", lambda: "engine")
    monkeypatch.setattr(
        loader,
        "construir_permanencia",
        lambda engine, metodo, tracker, force=False: (trackers.append(tracker), 1)[1],
    )

    main._run_permanencia(metodos=["radio", "h3"])
    assert all(isinstance(t, MetadataTracker) for t in trackers)
    assert trackers[0] is trackers[1]


@pytest.mark.unit
class TestTroceo:
    """El llenado va por partes para que un fallo no cueste todo el trabajo.

    Dos llaves de troceo distintas, una por fase, y la razon de que sean
    distintas es el grano: `obs` esta a nivel de observacion --cada fila
    pertenece a un solo corte, asi que partir por corte no parte nada-- y
    los agregados estan a nivel de identidad, que CRUZA los cortes.
    """

    def test_obs_es_unlogged_y_no_temporal(self):
        """Una temporal muere en el primer COMMIT; el troceo necesita commitear.

        Es la razon de ser de `UNLOGGED`: da lo que importaba de la
        temporal --no escribe WAL-- sin morir al commitear cada corte.
        """
        from src.etl.loaders.permanencia import Permanencia

        ddl = Permanencia("radio").sql_crear_obs()
        assert "UNLOGGED" in ddl
        assert "CREATE TEMP" not in ddl

    def test_la_primera_fase_se_trocea_por_corte(self):
        """Y poda las particiones de los DOS lados del join.

        Sin el `i.period` el resultado seria el mismo pero se recorreria la
        tabla de identidad entera en cada una de las 20 vueltas.
        """
        from datetime import date

        from src.etl.loaders.permanencia import Permanencia

        sql = Permanencia("radio").sql_llenar_obs(date(2019, 11, 1), 8, 32)
        assert "e.period = DATE '2019-11-01'" in sql
        assert "i.period = DATE '2019-11-01'" in sql

    def test_la_segunda_fase_se_trocea_por_identidad(self):
        """Ningun paso del bloque lee la tabla completa.

        Si a uno se le olvidara el filtro, agregaria las 82.9M filas y el
        troceo no serviria de nada -- y los conteos saldrian del universo
        entero, no del bloque.
        """
        from src.etl.loaders.permanencia import Permanencia

        pasos = Permanencia("radio").pasos_bloque(7, 19)
        for paso in pasos[:-1]:
            assert "WHERE bloque = 7" in paso.sql, paso.nombre

    def test_el_bloque_es_funcion_de_la_identidad(self):
        """Por eso ningun establecimiento se parte entre bloques.

        Trocear por año o por `cve_ent` no cumple esto: la identidad cruza
        los cortes, y los negocios se mudan de municipio.
        """
        from src.etl.utils.troceado import expr_bloque

        expr = expr_bloque("clee_resuelto", 32)
        assert "clee_resuelto" in expr
        assert "md5" in expr  # estable entre versiones, a diferencia de hashtext
        assert "% 32" in expr

    def test_las_temporales_mueren_con_la_transaccion_del_bloque(self):
        """`ON COMMIT DROP`: un bloque que falla no deja basura."""
        from src.etl.loaders.permanencia import Permanencia

        for paso in Permanencia("radio").pasos_bloque(0, 19)[:-1]:
            assert "ON COMMIT DROP" in paso.sql, paso.nombre

    def test_el_ultimo_paso_escribe_la_tabla_destino(self):
        from src.etl.loaders.permanencia import Permanencia, tabla_destino

        ultimo = Permanencia("radio").pasos_bloque(0, 19)[-1]
        assert ultimo.nombre == tabla_destino("radio")
        assert ultimo.sql.lstrip().startswith("INSERT INTO")

    def test_el_insert_cuadra_columnas_con_el_modelo(self):
        """Las columnas del INSERT son las del ORM menos su default.

        Se compara contra el modelo y no contra un numero escrito a mano:
        agregar una columna al ORM y olvidarla en el INSERT es el error que
        esto atrapa, y sin la comparacion solo se descubre veinte cortes y
        varias horas mas tarde, cuando corre el primer `INSERT`.
        """
        from src.etl.loaders.permanencia import Permanencia, tabla_destino
        from src.shared.db.base import get_table

        sql = Permanencia("radio").pasos_bloque(0, 19)[-1].sql
        bloque = sql[sql.index("(") + 1 : sql.index(")\nSELECT")]
        escritas = {c.strip() for c in bloque.replace("\n", "").split(",") if c.strip()}

        # `construida_en` la pone la base (server_default), asi que es la
        # unica del modelo que el INSERT no nombra.
        del_modelo = {c.name for c in get_table(tabla_destino("radio")).columns}
        assert escritas == del_modelo - {"construida_en"}

    def test_el_origen_sale_del_enum_no_de_una_cadena_suelta(self):
        from src.etl.loaders.permanencia import Permanencia
        from src.shared.enums import OrigenClee

        pasos = Permanencia("radio").pasos_bloque(0, 19)
        ventana = next(p for p in pasos if p.nombre == "ventana")
        assert f"'{OrigenClee.DIRECTO.value}'" in ventana.sql
        assert f"'{OrigenClee.PROPAGADO.value}'" in ventana.sql

    def test_cada_metodo_tiene_su_tabla_de_trabajo(self):
        """Construir radio no debe pisar la tabla de trabajo de h3."""
        from src.etl.loaders.permanencia import tabla_obs

        assert tabla_obs("radio") != tabla_obs("h3")


@pytest.mark.unit
class TestLoQueQuedaEnMeta:
    """El metodo tiene que ser consultable, no deducible del nombre."""

    def test_el_metodo_y_su_identidad_van_al_config_de_la_corrida(self):
        """Asi "¿que corridas usaron clee_h3?" es un WHERE sobre el JSON."""
        from src.etl.loaders.permanencia import Permanencia

        contexto = Permanencia("h3").contexto_meta(conn=None)
        assert contexto["metodo"] == "h3"
        assert contexto["identidad"].endswith("clee_h3")

    def test_el_contexto_es_serializable_a_json(self):
        """`meta.job_runs.config` es JSON."""
        import json

        from src.etl.loaders.permanencia import Permanencia

        json.dumps(Permanencia("radio").contexto_meta(conn=None))


@pytest.mark.unit
class TestRellenoH3Cell7:
    """`h3-cell-7` tiene que correr ANTES de la resolucion que la usa."""

    def test_es_una_seccion_atomica(self):
        from src.shared.enums import DENUE_ATOMIC, Section

        assert Section.h3_cell_7 in DENUE_ATOMIC

    def test_va_antes_de_clee_celda_h3_7(self):
        """`clee-celda-h3-7` usa la columna como criterio.

        Con la columna en NULL la resolucion no falla: corre sobre nada y
        produce una tabla vacia, que es peor que un error.
        """
        from src.shared.enums import DENUE_ATOMIC, Section

        assert DENUE_ATOMIC.index(Section.h3_cell_7) < DENUE_ATOMIC.index(Section.clee_celda_h3_7)

    def test_va_despues_del_ingest(self):
        """Rellena lo que el ingest acaba de escribir."""
        from src.shared.enums import DENUE_ATOMIC, Section

        assert DENUE_ATOMIC.index(Section.ingest) < DENUE_ATOMIC.index(Section.h3_cell_7)

    def test_pedirla_sola_no_arrastra_el_ingest(self):
        from src.etl.main import _expand_only
        from src.shared.enums import Section

        assert _expand_only([Section.h3_cell_7]) == {Section.h3_cell_7}

    def test_el_relleno_no_acepta_force(self):
        """Solo toca filas en NULL y el padre de una celda no cambia.

        Un `force` que reescribiera los 82.9M daria los mismos valores a
        cambio de un rewrite completo por MVCC.
        """
        import inspect

        from src.etl.loaders.relleno_h3_7 import rellenar_h3_cell_7

        assert "force" not in inspect.signature(rellenar_h3_cell_7).parameters


@pytest.mark.unit
class TestElPadreNoSeCalculaDelPunto:
    """La regla que mantiene consistente la jerarquia H3."""

    def test_el_relleno_da_lo_mismo_que_el_ingest(self):
        """Si difirieran, las filas viejas y nuevas tendrian criterios distintos.

        En los bordes, el padre de la celda res 9 y la celda res 7 del mismo
        punto pueden diferir, asi que esto no es una tautologia: es la
        aserción de que el relleno usa el PADRE y no el punto.
        """
        import h3

        from src.etl.loaders.relleno_h3_7 import _padres

        # Un punto cualquiera de Guadalajara, por el camino del ingest.
        celda9 = h3.latlng_to_cell(20.6736, -103.344, 9)
        esperado = h3.str_to_int(h3.cell_to_parent(celda9, 7))

        mapa = _padres([h3.str_to_int(celda9)])
        assert mapa["h3_cell_7"][0] == esperado

    def test_varias_celdas_res_9_comparten_padre(self):
        """Es lo que vuelve barato mapear celdas en vez de filas."""
        import h3

        from src.etl.loaders.relleno_h3_7 import _padres

        centro = h3.latlng_to_cell(20.6736, -103.344, 9)
        vecinas = [h3.str_to_int(c) for c in h3.grid_disk(centro, 1)]
        mapa = _padres(vecinas)
        assert mapa.height == len(vecinas)
        assert mapa["h3_cell_7"].n_unique() < len(vecinas)
