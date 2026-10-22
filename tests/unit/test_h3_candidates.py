"""Tests de paridad del generador de candidatos H3 vs BallTree.

`find_h3_neighbors` (blocking H3, tabla silver.clee_h3) debe producir
EXACTAMENTE los mismos conjuntos de vecinos dentro de SPATIAL_RADIUS_M que
`find_spatial_neighbors` (BallTree, tabla silver.clee_radio): mismo
radio, misma semantica; solo cambia el algoritmo de generacion.

Ambos generadores retornan distancias en METROS y comparten el mismo
tie-break determinista (distancia ascendente, luego indice de KB), asi que
la paridad exige orden IDENTICO de vecinos, no solo los mismos conjuntos.
"""

import numpy as np
import polars as pl
import pytest
from h3.api import basic_int as h3i

from src.etl.utils.clee_resolution_config import H3_BLOCKING_RES, SPATIAL_RADIUS_M
from src.etl.utils.matching import (
    find_h3_neighbors,
    find_spatial_neighbors,
)


def _make_points_df(n: int, seed: int, center=(19.4326, -99.1332), spread=0.02) -> pl.DataFrame:
    """DataFrame sintetico de puntos alrededor de un centro (CDMX) con su
    celda h3_cell_9, imitando el esquema que consume el matching."""
    rng = np.random.default_rng(seed)
    lats = center[0] + rng.uniform(-spread, spread, n)
    lons = center[1] + rng.uniform(-spread, spread, n)
    cells = [h3i.latlng_to_cell(lat, lon, H3_BLOCKING_RES) for lat, lon in zip(lats, lons)]
    return pl.DataFrame(
        {
            "latitud": lats,
            "longitud": lons,
            "h3_cell_9": pl.Series(cells, dtype=pl.Int64),
        }
    )


@pytest.mark.unit
class TestH3NeighborParity:
    """find_h3_neighbors == find_spatial_neighbors (mismos vecinos)."""

    def test_same_neighbor_sets_and_distances(self):
        ref_df = _make_points_df(400, seed=42)
        target_df = _make_points_df(150, seed=7)

        bt_idx, bt_dist = find_spatial_neighbors(ref_df, target_df, SPATIAL_RADIUS_M)
        h3_idx, h3_dist = find_h3_neighbors(ref_df, target_df, SPATIAL_RADIUS_M)

        assert len(h3_idx) == target_df.height == len(bt_idx)
        n_with_neighbors = 0
        for i in range(target_df.height):
            # Orden IDENTICO (tie-break determinista compartido), no solo
            # los mismos conjuntos.
            assert list(h3_idx[i]) == list(bt_idx[i]), f"vecinos distintos en target {i}"
            # Mismas distancias en metros (ambos generadores).
            np.testing.assert_allclose(h3_dist[i], bt_dist[i], rtol=1e-6)
            n_with_neighbors += 1 if len(h3_idx[i]) else 0
        # El escenario debe ejercitar de verdad el matching espacial.
        assert n_with_neighbors > 0

    def test_h3_distances_sorted_ascending(self):
        ref_df = _make_points_df(400, seed=42)
        target_df = _make_points_df(150, seed=7)

        _, h3_dist = find_h3_neighbors(ref_df, target_df, SPATIAL_RADIUS_M)
        for dist in h3_dist:
            assert np.all(np.diff(dist) >= 0)

    def test_all_h3_distances_within_radius(self):
        ref_df = _make_points_df(400, seed=1)
        target_df = _make_points_df(150, seed=2)

        _, h3_dist = find_h3_neighbors(ref_df, target_df, SPATIAL_RADIUS_M)
        for dist in h3_dist:
            assert np.all(dist <= SPATIAL_RADIUS_M)


@pytest.mark.unit
class TestH3NeighborEdgeCases:
    """Casos borde: entradas vacias y celdas nulas (datos de encuesta)."""

    def test_empty_ref_returns_empty_per_target(self):
        ref_df = _make_points_df(0, seed=3)
        target_df = _make_points_df(10, seed=4)

        idx, dist = find_h3_neighbors(ref_df, target_df, SPATIAL_RADIUS_M)
        assert len(idx) == 10
        assert all(len(a) == 0 for a in idx)
        assert all(len(a) == 0 for a in dist)

    def test_empty_target_returns_empty(self):
        ref_df = _make_points_df(10, seed=5)
        target_df = _make_points_df(0, seed=6)

        idx, dist = find_h3_neighbors(ref_df, target_df, SPATIAL_RADIUS_M)
        assert len(idx) == 0
        assert len(dist) == 0

    def test_null_target_cell_gets_no_candidates(self):
        ref_df = _make_points_df(50, seed=8)
        target_df = _make_points_df(5, seed=9).with_columns(
            pl.Series("h3_cell_9", [None] * 5, dtype=pl.Int64)
        )

        idx, _ = find_h3_neighbors(ref_df, target_df, SPATIAL_RADIUS_M)
        assert len(idx) == 5
        assert all(len(a) == 0 for a in idx)

    def test_null_ref_cell_excluded_from_candidates(self):
        ref_df = _make_points_df(50, seed=8).with_columns(
            pl.Series("h3_cell_9", [None] * 50, dtype=pl.Int64)
        )
        target_df = _make_points_df(5, seed=9)

        idx, _ = find_h3_neighbors(ref_df, target_df, SPATIAL_RADIUS_M)
        assert all(len(a) == 0 for a in idx)


@pytest.mark.unit
class TestRadioRobustnessAndTies:
    """Robustez de find_spatial_neighbors (coords nulas) y tie-break."""

    def test_null_coords_get_zero_candidates_not_crash(self):
        """Filas sin coords (NULL -> NaN) reciben 0 candidatos en radio,
        misma semantica que el blocking H3 (antes: crash del BallTree)."""
        ref_df = _make_points_df(20, seed=10)
        target_df = _make_points_df(4, seed=11).with_columns(
            latitud=pl.Series([19.43, None, 95.0, 19.44]),
            longitud=pl.Series([-99.13, None, -99.0, -99.14]),
        )

        idx, dist = find_spatial_neighbors(ref_df, target_df, SPATIAL_RADIUS_M)
        assert len(idx) == 4
        assert len(idx[1]) == 0 and len(dist[1]) == 0  # coords NULL
        assert len(idx[2]) == 0 and len(dist[2]) == 0  # fuera de dominio

    def test_null_ref_coords_excluded(self):
        ref_df = _make_points_df(5, seed=12).with_columns(
            latitud=pl.Series([None] * 5, dtype=pl.Float64)
        )
        target_df = _make_points_df(3, seed=13)

        idx, _ = find_spatial_neighbors(ref_df, target_df, SPATIAL_RADIUS_M)
        assert all(len(a) == 0 for a in idx)

    def test_exact_distance_ties_broken_by_kb_index_in_both(self):
        """Coords identicas (mismo edificio, comun en DENUE) = empate exacto
        de distancia: ambos generadores deben ordenar por indice de KB."""
        lat, lon = 19.4326, -99.1332
        cell = h3i.latlng_to_cell(lat, lon, H3_BLOCKING_RES)
        ref_df = pl.DataFrame(
            {
                "latitud": [lat, lat, lat],
                "longitud": [lon, lon, lon],
                "h3_cell_9": pl.Series([cell] * 3, dtype=pl.Int64),
            }
        )
        target_df = pl.DataFrame(
            {
                "latitud": [lat],
                "longitud": [lon],
                "h3_cell_9": pl.Series([cell], dtype=pl.Int64),
            }
        )

        bt_idx, _ = find_spatial_neighbors(ref_df, target_df, SPATIAL_RADIUS_M)
        h3_idx, _ = find_h3_neighbors(ref_df, target_df, SPATIAL_RADIUS_M)
        assert list(bt_idx[0]) == [0, 1, 2]
        assert list(h3_idx[0]) == [0, 1, 2]
