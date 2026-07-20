"""Tests for the Fase 7 masked-CLEE evaluation (classification + metrics)."""

import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evaluate_masked_clee import MASTER_COLUMN, classify_results, compute_metrics  # noqa: E402


def _results_frame() -> pl.DataFrame:
    """Escenario minimo con las 5 categorias del protocolo.

    Verdad conocida en refs: {C1, C2, C3}. Filas:
    - a: verdad C1, asigno C1  -> correcto
    - b: verdad C2, asigno C3  -> falso_match
    - c: verdad C3, asigno NEW -> falso_nuevo
    - d: verdad X9 (no en refs), asigno NEW -> nuevo_correcto
    - e: verdad Y8 (no en refs), asigno C1  -> match_espurio
    """
    return pl.DataFrame(
        {
            "id": ["a", "b", "c", "d", "e"],
            "clee_verdadero": ["C1", "C2", "C3", "X9", "Y8"],
            MASTER_COLUMN: ["C1", "C3", "NEW_EVAL-111", "NEW_EVAL-222", "C1"],
        }
    )


@pytest.mark.unit
class TestClassifyResults:
    """classify_results(): particion evaluables/no-evaluables."""

    def test_all_five_categories(self):
        out = classify_results(_results_frame(), "clee_verdadero", {"C1", "C2", "C3"})
        got = dict(zip(out["id"].to_list(), out["categoria"].to_list(), strict=True))
        assert got == {
            "a": "correcto",
            "b": "falso_match",
            "c": "falso_nuevo",
            "d": "nuevo_correcto",
            "e": "match_espurio",
        }

    def test_evaluable_flag(self):
        out = classify_results(_results_frame(), "clee_verdadero", {"C1", "C2", "C3"})
        flags = dict(zip(out["id"].to_list(), out["evaluable"].to_list(), strict=True))
        assert flags == {"a": True, "b": True, "c": True, "d": False, "e": False}


@pytest.mark.unit
class TestComputeMetrics:
    """compute_metrics(): formulas del protocolo."""

    def test_metrics_on_known_scenario(self):
        out = classify_results(_results_frame(), "clee_verdadero", {"C1", "C2", "C3"})
        m = compute_metrics(out)

        assert m["evaluables"] == 3
        assert m["no_evaluables"] == 2
        # precision = correctos / asignados-no-NEW = 1 / (1+1+1 espurio)
        assert m["precision"] == pytest.approx(1 / 3)
        # recall = correctos / evaluables = 1/3
        assert m["recall"] == pytest.approx(1 / 3)
        # tasa de espurios = 1 espurio / 2 no-evaluables
        assert m["tasa_espurios"] == pytest.approx(0.5)

    def test_perfect_resolution(self):
        df = pl.DataFrame(
            {
                "id": ["a", "d"],
                "clee_verdadero": ["C1", "X9"],
                MASTER_COLUMN: ["C1", "NEW_EVAL-1"],
            }
        )
        m = compute_metrics(classify_results(df, "clee_verdadero", {"C1"}))
        assert (m["precision"], m["recall"], m["f1"]) == (1.0, 1.0, 1.0)
        assert m["tasa_espurios"] == 0.0

    def test_empty_frame_no_division_error(self):
        df = pl.DataFrame(
            {"id": [], "clee_verdadero": [], MASTER_COLUMN: []},
            schema={"id": pl.Utf8, "clee_verdadero": pl.Utf8, MASTER_COLUMN: pl.Utf8},
        )
        m = compute_metrics(classify_results(df, "clee_verdadero", set()))
        assert m["total"] == 0
        assert m["precision"] == 0.0
