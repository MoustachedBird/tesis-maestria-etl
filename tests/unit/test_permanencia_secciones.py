"""Selección de método en la construcción de permanencia.

Aparte de `test_permanencia.py` porque ese archivo está marcado
`postgres` entero --construye las tablas de verdad-- y esto solo lee la
tabla de secciones del CLI.
"""

from contextlib import contextmanager

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
    tablas: la segunda cuesta un GROUP BY sobre 82.9M filas.
    """
    import src.etl.main as main
    from src.etl.loaders import permanencia as loader

    construidos: list[str] = []

    class _EngineFalso:
        """Entrega una conexion inerte: aqui se prueba el despacho, no el SQL."""

        @contextmanager
        def begin(self):
            yield None

    monkeypatch.setattr("src.shared.db.session.get_engine", _EngineFalso)
    monkeypatch.setattr(
        loader,
        "construir_permanencia",
        lambda conn, metodo: (construidos.append(metodo), 1)[1],
    )

    main._run_permanencia(metodos=["h3"])
    assert construidos == ["h3"]


@pytest.mark.unit
def test_cada_metodo_va_en_su_propia_transaccion(monkeypatch):
    """Si h3 falla, lo que radio escribio no se revierte."""
    import src.etl.main as main
    from src.etl.loaders import permanencia as loader

    transacciones = []

    class _EngineFalso:
        @contextmanager
        def begin(self):
            transacciones.append(1)
            yield None

    monkeypatch.setattr("src.shared.db.session.get_engine", _EngineFalso)
    monkeypatch.setattr(loader, "construir_permanencia", lambda conn, metodo: 1)

    main._run_permanencia(metodos=["radio", "h3"])
    assert len(transacciones) == 2


@pytest.mark.unit
class TestPasosDelLlenado:
    """El llenado es una secuencia de pasos con nombre, no una sentencia opaca.

    Lo que se fija aquí es el contrato del log: un `INSERT ... SELECT` de
    horas que muere solo deja escrito que empezó, así que los pasos --y que
    sean varios-- son la única forma de saber dónde se quedó.
    """

    def _pasos(self):
        from datetime import date

        from src.etl.loaders.permanencia import _pasos

        return _pasos("radio", [date(2015, 1, 1), date(2026, 5, 1)])

    def test_son_varios_y_cada_uno_se_describe(self):
        pasos = self._pasos()
        assert len(pasos) > 1
        for paso in pasos:
            assert paso.nombre and paso.descripcion and paso.sql

    def test_el_ultimo_escribe_la_tabla_destino(self):
        """El rowcount del último es lo que `construir_permanencia` devuelve."""
        from src.etl.loaders.permanencia import tabla_destino

        ultimo = self._pasos()[-1]
        assert ultimo.nombre == tabla_destino("radio")
        assert ultimo.sql.lstrip().startswith("INSERT INTO")

    def test_las_temporales_mueren_con_la_transaccion(self):
        """`ON COMMIT DROP`: un fallo a la mitad no deja basura en la base."""
        for paso in self._pasos()[:-1]:
            assert "ON COMMIT DROP" in paso.sql, paso.nombre

    def test_el_insert_cuadra_columnas_con_valores(self):
        """53 columnas y 53 expresiones: un desfase lo rechaza PostgreSQL.

        Barato de verificar aquí y caro de descubrir cuatro pasos y varias
        horas más tarde, que es cuando el `INSERT` corre.
        """
        sql = self._pasos()[-1].sql
        columnas = sql[sql.index("(") + 1 : sql.index(")\nSELECT")]
        assert len([c for c in columnas.replace("\n", "").split(",") if c.strip()]) == 53

    def test_el_origen_sale_del_enum_no_de_una_cadena_suelta(self):
        from src.shared.enums import OrigenClee

        ventana = next(p for p in self._pasos() if p.nombre == "ventana")
        assert f"'{OrigenClee.DIRECTO.value}'" in ventana.sql
        assert f"'{OrigenClee.PROPAGADO.value}'" in ventana.sql
