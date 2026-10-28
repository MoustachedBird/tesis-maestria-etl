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
