"""El registro de métodos describe un método completo, no solo su índice.

`radio` y `h3` difieren solo en el generador de candidatos y por eso
producen la MISMA partición (0 de 82.9M observaciones en disputa, medido —
ver `alternativas_clee_propagation.md` §0). Un método *distinto* necesita
variar la **regla de decisión**, y eso es lo que estos tests fijan: que el
registro la pueda expresar y que el motor la respete.
"""

import pytest

from src.etl.transformers.clee_resolver import CLEEResolver
from src.etl.utils import entity_resolution as er
from src.etl.utils.matching import calculate_match_score

#: Claves que toda entrada del registro debe traer. Si se agrega una, este
#: test falla y obliga a declararla en TODOS los métodos en vez de dejar
#: uno a medias.
CLAVES = {"table", "propagate", "scorer", "ceiling", "prefix_suffix"}


@pytest.fixture(autouse=True)
def _restituir_scorer():
    """Deja el motor con la cascada por default al salir de cada test.

    `scorer` es global del módulo: un test que lo inyecta y no lo restituye
    contamina a los que siguen, y el síntoma sería un fallo en otro archivo.
    """
    yield
    er.set_matching_scorer()


@pytest.mark.unit
class TestRegistro:
    def test_todo_metodo_declara_las_mismas_claves(self):
        for metodo, cfg in CLEEResolver.METHODS.items():
            assert set(cfg) == CLAVES, f"{metodo} no declara {CLAVES - set(cfg)}"

    def test_cada_metodo_tiene_su_tabla_y_su_prefijo(self):
        """Dos métodos no pueden pisarse la tabla ni los `NEW_*` sintéticos."""
        tablas = [c["table"] for c in CLEEResolver.METHODS.values()]
        prefijos = [c["prefix_suffix"] for c in CLEEResolver.METHODS.values()]
        assert len(set(tablas)) == len(tablas)
        assert len(set(prefijos)) == len(prefijos)

    def test_radio_y_h3_comparten_la_regla_de_decision(self):
        """Es la razón de que no sean dos métodos sino uno con dos índices."""
        assert CLEEResolver.METHODS["radio"]["scorer"] is None
        assert CLEEResolver.METHODS["h3"]["scorer"] is None
        assert (
            CLEEResolver.METHODS["radio"]["propagate"]
            is not CLEEResolver.METHODS["h3"]["propagate"]
        )


@pytest.mark.unit
class TestInyeccionDeLaRegla:
    def test_el_default_es_la_cascada_con_su_techo(self):
        """Sin inyectar nada, el motor es el de siempre."""
        assert er.scorer is calculate_match_score
        assert er.scorer_ceiling is not None

    def test_un_scorer_nuevo_no_hereda_el_techo_de_la_cascada(self):
        """El techo es de la ESCALA del score, y la escala es del método.

        Heredar el techo de la cascada (100 puntos de fuzzy) en un modelo
        que devuelve log-odds cortaría el argmax en el primer candidato que
        superara 100 — o nunca. Sin techo declarado se evalúan todos: más
        lento y correcto.
        """

        def propio(*_args):
            return (False, "X", 0.0)

        er.set_matching_scorer(propio)
        assert er.scorer is propio
        assert er.scorer_ceiling is None

    def test_none_restituye_la_cascada_CON_techo(self):
        """`scorer: None` en el registro no es "sin corte temprano".

        Es el caso de `radio` y `h3`: usan la cascada y necesitan su techo.
        Pasarles un techo nulo les quitaría el corte y les costaría horas
        en una corrida de una noche.
        """

        def propio(*_args):
            return (False, "X", 0.0)

        er.set_matching_scorer(propio)
        er.set_matching_scorer(None, None)
        assert er.scorer is calculate_match_score
        assert er.scorer_ceiling is not None

    def test_el_motor_usa_la_regla_inyectada(self, monkeypatch):
        """`_best_candidate` resuelve `scorer` como global en cada llamada."""
        llamadas = []

        def siempre_el_segundo(t_name, *resto):
            llamadas.append(t_name)
            # Gana el candidato cuyo nombre de KB es "B"
            return (resto[6] == "B", "TEST", 10.0 if resto[6] == "B" else 1.0)

        er.set_matching_scorer(siempre_el_segundo)
        elegido = er._best_candidate(
            neighbors=[0, 1],
            t_name="objetivo",
            t_raz="",
            t_act="",
            t_tel="",
            t_email="",
            t_web="",
            t_cp="",
            kb_clees=["CLEE_A", "CLEE_B"],
            kb_names=["A", "B"],
            kb_razones=["", ""],
            kb_acts=["", ""],
            kb_tels=["", ""],
            kb_emails=["", ""],
            kb_wwws=["", ""],
            kb_cps=["", ""],
        )
        assert elegido == "CLEE_B"
        assert llamadas, "la regla inyectada no se invocó"

    def test_sin_techo_se_evaluan_todos_los_candidatos(self):
        """Un scorer sin techo no puede cortar: el argmax tiene que ver todo.

        Es lo que hace segura la inyección: el candidato bueno puede estar
        al final de la lista.
        """
        vistos = []

        def cuenta(t_name, *resto):
            vistos.append(resto[6])
            return (True, "TEST", float(len(vistos)))

        er.set_matching_scorer(cuenta)
        elegido = er._best_candidate(
            neighbors=[0, 1, 2],
            t_name="objetivo",
            t_raz="",
            t_act="",
            t_tel="",
            t_email="",
            t_web="",
            t_cp="",
            kb_clees=["C1", "C2", "C3"],
            kb_names=["A", "B", "C"],
            kb_razones=["", "", ""],
            kb_acts=["", "", ""],
            kb_tels=["", "", ""],
            kb_emails=["", "", ""],
            kb_wwws=["", "", ""],
            kb_cps=["", "", ""],
        )
        assert vistos == ["A", "B", "C"], "cortó antes de ver todos"
        assert elegido == "C3", "no se quedó con el de score más alto"
