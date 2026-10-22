"""Tests de `_best_candidate`: elegir el mejor candidato, no el primero.

Fijan la correccion de 2026-10-06 (`docs/findings.md`): el loop de
candidatos hacia `break` en el primer match directo y, como los vecinos
llegan ordenados por distancia, un vecino cercano con nombre apenas
parecido le ganaba al registro verdadero un poco mas lejos con nombre
identico.

Las KB de estos tests son listas columnares paralelas (mismo contrato que
las globales `_shared_kb_*`): el indice i describe al candidato i, y
`neighbors` es el orden en que el generador espacial los entrega.
"""

import numpy as np
import pytest

from src.etl.utils.entity_resolution import _best_candidate
from src.etl.utils.matching import (
    NAME_TIER,
    PERFECT_NAME_SCORE,
    PERFECT_RAZON_SCORE,
    RAZON_TIER,
    calculate_match_score,
)

ACT = "Restaurantes con servicio de preparacion de tacos y tortas"


def _kb(rows: list[dict]) -> dict:
    """Arma las listas columnares de la KB a partir de dicts por candidato.

    Cada dict acepta las claves clee/name/razon/act/tel/email/www/cp; las
    ausentes quedan como "" (los datos de encuesta traen campos vacios y la
    cascada los trata como cadena vacia, no como nulo).
    """
    return {
        "kb_clees": [r["clee"] for r in rows],
        "kb_names": [r.get("name", "") for r in rows],
        "kb_razones": [r.get("razon", "") for r in rows],
        "kb_acts": [r.get("act", ACT) for r in rows],
        "kb_tels": [r.get("tel", "") for r in rows],
        "kb_emails": [r.get("email", "") for r in rows],
        "kb_wwws": [r.get("www", "") for r in rows],
        "kb_cps": [r.get("cp", "") for r in rows],
    }


def _call(neighbors, kb, *, name="", razon="", act=ACT, tel="", email="", web="", cp=""):
    """Invoca `_best_candidate` con el target como kwargs legibles."""
    return _best_candidate(
        np.array(neighbors, dtype=np.int64),
        name,
        razon,
        act,
        tel,
        email,
        web,
        cp,
        kb["kb_clees"],
        kb["kb_names"],
        kb["kb_razones"],
        kb["kb_acts"],
        kb["kb_tels"],
        kb["kb_emails"],
        kb["kb_wwws"],
        kb["kb_cps"],
    )


class TestRegresionPrimerMatch:
    """El caso que el `break` original resolvia mal."""

    def test_gana_el_nombre_identico_aunque_este_mas_lejos(self):
        # Vecino 0 (el mas cercano) pasa el umbral de nombre con lo justo;
        # vecino 1 (mas lejos) es el registro verdadero, nombre identico.
        kb = _kb(
            [
                {"clee": "IMPOSTOR", "name": "TAQUERIA EL BUEN SAZON"},
                {"clee": "CORRECTO", "name": "TAQUERIA EL BUEN SABOR"},
            ]
        )
        got = _call([0, 1], kb, name="TAQUERIA EL BUEN SABOR")
        assert got == "CORRECTO"

    def test_el_orden_de_los_vecinos_no_cambia_el_resultado(self):
        # Invariante central del argmax: el resultado no debe depender del
        # orden de llegada (salvo empates, cubiertos aparte).
        rows = [
            {"clee": "IMPOSTOR", "name": "TAQUERIA EL BUEN SAZON"},
            {"clee": "CORRECTO", "name": "TAQUERIA EL BUEN SABOR"},
        ]
        directo = _call([0, 1], _kb(rows), name="TAQUERIA EL BUEN SABOR")
        inverso = _call([1, 0], _kb(rows), name="TAQUERIA EL BUEN SABOR")
        assert directo == inverso == "CORRECTO"


class TestEmpates:
    """Ante scores iguales se conserva el mas cercano (comportamiento previo)."""

    def test_empate_conserva_al_primero_que_es_el_mas_cercano(self):
        # Gemelos reales: mismo nombre, mismo giro. El nombre no desempata,
        # asi que decide la cercania = el orden de llegada.
        kb = _kb(
            [
                {"clee": "CERCANO", "name": "CLINICA ONCOLOGICA"},
                {"clee": "LEJANO", "name": "CLINICA ONCOLOGICA"},
            ]
        )
        assert _call([0, 1], kb, name="CLINICA ONCOLOGICA") == "CERCANO"
        assert _call([1, 0], kb, name="CLINICA ONCOLOGICA") == "LEJANO"


class TestPrecedenciaDePaths:
    """Los tiers replican el orden de los `return` de la cascada."""

    def test_razon_social_le_gana_a_nombre_comercial(self):
        # Path A (razon) es evidencia mas fuerte que Path B (nombre), incluso
        # con un ratio de nombre mas alto en el otro candidato.
        kb = _kb(
            [
                {"clee": "POR_NOMBRE", "name": "PANADERIA LA ESPERANZA"},
                {"clee": "POR_RAZON", "razon": "GRUPO ESPERANZA SA DE CV"},
            ]
        )
        got = _call(
            [0, 1],
            kb,
            name="PANADERIA LA ESPERANZA",
            razon="GRUPO ESPERANZA SA DE CV",
        )
        assert got == "POR_RAZON"

    def test_match_directo_le_gana_a_combinado_en_cualquier_orden(self):
        # El combinado llega primero (mas cercano) pero el directo debe ganar.
        kb = _kb(
            [
                {
                    "clee": "COMBINADO",
                    "name": "TAQUERIA EL BUEN",
                    "cp": "01000",
                    "tel": "5555555555",
                },
                {"clee": "DIRECTO", "name": "TAQUERIA EL BUEN SABOR"},
            ]
        )
        got = _call([0, 1], kb, name="TAQUERIA EL BUEN SABOR", cp="01000", tel="5555555555")
        assert got == "DIRECTO"


class TestCorteTemprano:
    """El corte al techo teorico no cambia el resultado (es lossless)."""

    def test_razon_perfecta_corta_y_devuelve_lo_mismo(self):
        # El candidato 0 ya alcanza el techo (razon identica): lo que venga
        # despues no puede superarlo, y el empate conservaria al primero.
        kb = _kb(
            [
                {"clee": "PERFECTO", "razon": "GRUPO ESPERANZA SA DE CV"},
                {"clee": "OTRO_IGUAL", "razon": "GRUPO ESPERANZA SA DE CV"},
            ]
        )
        got = _call([0, 1], kb, razon="GRUPO ESPERANZA SA DE CV")
        assert got == "PERFECTO"

    def test_sin_razon_en_el_target_el_techo_es_el_de_nombre(self):
        # Sin razon social usable en el target, Path A no puede disparar
        # contra nadie: el techo baja un tier y el corte aplica igual.
        kb = _kb([{"clee": "A", "name": "FERRETERIA CENTRAL"}])
        assert _call([0], kb, name="FERRETERIA CENTRAL") == "A"
        _, _, score = calculate_match_score(
            "FERRETERIA CENTRAL",
            "",
            ACT,
            "",
            "",
            "",
            "",
            "FERRETERIA CENTRAL",
            "",
            ACT,
            "",
            "",
            "",
            "",
        )
        assert score == PERFECT_NAME_SCORE


class TestSinMatch:
    """Casos donde no debe asignarse ningun CLEE."""

    def test_sin_vecinos_devuelve_none(self):
        assert _call([], _kb([{"clee": "X", "name": "ALGO"}])) is None

    def test_ningun_candidato_pasa_devuelve_none(self):
        kb = _kb([{"clee": "X", "name": "FLORERIA DON PEPE"}])
        assert _call([0], kb, name="TALLER MECANICO HERMANOS LOPEZ") is None

    def test_gate_de_actividad_descarta_antes_de_comparar(self):
        # Mismo nombre exacto, pero giro distinto: el gate manda.
        kb = _kb([{"clee": "X", "name": "LA PASADITA", "act": "Bufetes juridicos"}])
        assert _call([0], kb, name="LA PASADITA") is None


class TestContratoDelScore:
    """El score solo es significativo cuando hay match."""

    @pytest.mark.parametrize(
        "t_act,kb_act,estrategia",
        [
            (ACT, "Bufetes juridicos", "DIFF_ACTIVITY"),
            (ACT, ACT, "NONE"),
        ],
    )
    def test_no_match_devuelve_score_cero(self, t_act, kb_act, estrategia):
        is_match, strategy, score = calculate_match_score(
            "FLORERIA DON PEPE",
            "",
            t_act,
            "",
            "",
            "",
            "",
            "TALLER HERMANOS LOPEZ",
            "",
            kb_act,
            "",
            "",
            "",
            "",
        )
        assert is_match is False
        assert strategy == estrategia
        assert score == 0.0

    def test_los_tiers_no_se_traslapan(self):
        # Garantia estructural: ningun combinado puede superar a un match
        # por nombre, y ninguno por nombre puede superar a uno por razon.
        max_combinado = 5 * 100.0 + 100.0
        assert max_combinado < NAME_TIER
        assert PERFECT_NAME_SCORE < RAZON_TIER
        assert PERFECT_RAZON_SCORE == RAZON_TIER + 100.0
