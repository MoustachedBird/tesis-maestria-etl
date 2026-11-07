"""El método `celda`: la celda H3 ES el criterio, no un índice.

Es la diferencia de fondo con `find_h3_neighbors`, donde la celda solo
acelera la búsqueda y el criterio sigue siendo el círculo de 500 m. Dos
casos definen el método, y son los que estos tests fijan:

    lejos (>500 m) pero misma celda res 7  ->  `celda` sí, `radio`/`h3` no
    cerca (<500 m) pero celda distinta     ->  `radio`/`h3` sí, `celda` no

Las coordenadas salen de un barrido sobre la retícula real en Guadalajara,
no inventadas: cambiar `CELL_MATCH_RES` las invalida, y ese es justamente
el aviso que se quiere.
"""

import polars as pl
import pytest
from h3.api import basic_int as h3i

from src.etl.utils.clee_resolution_config import CELL_MATCH_RES
from src.etl.utils.matching import (
    find_cell_neighbors,
    find_h3_neighbors,
    find_spatial_neighbors,
)

#: Origen del escenario: centro de Guadalajara.
ORIGEN = (20.6736, -103.3440)

#: A 800 m al norte y en la MISMA celda res 7 que el origen.
LEJOS_MISMA_CELDA = (20.680795, -103.3440)

#: A 200 m y en una celda res 7 DISTINTA: al otro lado de una frontera.
CERCA_OTRA_CELDA = (20.67197, -103.343188)


def _df(*puntos: tuple[float, float]) -> pl.DataFrame:
    """DataFrame con las columnas que los generadores exigen.

    `h3_cell_7` se construye como PADRE de `h3_cell_9`, igual que lo hace el
    ingest: calcularla del punto daria otra celda en ~6.5% de los casos
    cerca de fronteras, y el test estaria probando otra cosa.
    """
    filas = []
    for lat, lon in puntos:
        cell9 = h3i.latlng_to_cell(lat, lon, 9)
        filas.append(
            {
                "latitud": lat,
                "longitud": lon,
                "h3_cell_9": cell9,
                "h3_cell_7": h3i.cell_to_parent(cell9, CELL_MATCH_RES),
            }
        )
    return pl.DataFrame(filas)


def _vecinos(fn, ref: pl.DataFrame, tgt: pl.DataFrame) -> list[int]:
    """Índices de KB que el generador devuelve para la única fila target."""
    indices, _ = fn(ref, tgt, 500.0)
    return [int(i) for i in indices[0]]


@pytest.mark.unit
class TestLaCeldaEsElCriterio:
    def test_acepta_lejos_si_comparten_celda(self):
        """A 800 m: fuera del radio, dentro de la celda.

        Es la mitad del método que gana recall — un negocio que se mudó
        unas cuadras sigue siendo candidato.
        """
        ref, tgt = _df(LEJOS_MISMA_CELDA), _df(ORIGEN)
        assert _vecinos(find_cell_neighbors, ref, tgt) == [0]
        assert _vecinos(find_spatial_neighbors, ref, tgt) == []
        assert _vecinos(find_h3_neighbors, ref, tgt) == []

    def test_rechaza_cerca_si_la_celda_difiere(self):
        """A 200 m: dentro del radio, al otro lado de una frontera.

        Es la mitad que pierde recall, y la razón por la que el método no
        reemplaza al radio: el error depende de dónde cae la retícula, no
        de la distancia.
        """
        ref, tgt = _df(CERCA_OTRA_CELDA), _df(ORIGEN)
        assert _vecinos(find_cell_neighbors, ref, tgt) == []
        assert _vecinos(find_spatial_neighbors, ref, tgt) == [0]
        assert _vecinos(find_h3_neighbors, ref, tgt) == [0]

    def test_el_radio_que_recibe_no_lo_usa(self):
        """Pasar radio 1 m o 10 km da el mismo resultado."""
        ref, tgt = _df(LEJOS_MISMA_CELDA), _df(ORIGEN)
        for radio in (1.0, 500.0, 10_000.0):
            indices, _ = find_cell_neighbors(ref, tgt, radio)
            assert [int(i) for i in indices[0]] == [0], f"el radio {radio} cambió el resultado"

    def test_declara_que_no_usa_radio(self):
        """Lo lee el motor para no escribir "radio=500m" en su log."""
        assert find_cell_neighbors.usa_radio is False
        assert getattr(find_h3_neighbors, "usa_radio", True) is True
        assert getattr(find_spatial_neighbors, "usa_radio", True) is True


@pytest.mark.unit
class TestContratoComun:
    """Mismo formato de retorno que los otros dos generadores."""

    def test_vecinos_ordenados_por_cercania(self):
        """`_best_candidate` rompe empates de score por distancia.

        Si los vecinos no llegaran ordenados, el desempate lo decidiría el
        orden del join — no determinista entre corridas.
        """
        ref = _df(LEJOS_MISMA_CELDA, ORIGEN)  # el lejano primero, a propósito
        indices, distancias = find_cell_neighbors(ref, _df(ORIGEN), 500.0)
        assert [int(i) for i in indices[0]] == [1, 0]
        assert list(distancias[0]) == sorted(distancias[0])

    def test_una_fila_sin_celda_no_obtiene_candidatos(self):
        """Datos de encuesta: sin coordenadas no hay celda, y eso no truena."""
        tgt = pl.DataFrame(
            [{"latitud": None, "longitud": None, "h3_cell_9": None, "h3_cell_7": None}],
            schema={
                "latitud": pl.Float64,
                "longitud": pl.Float64,
                "h3_cell_9": pl.Int64,
                "h3_cell_7": pl.Int64,
            },
        )
        indices, _ = find_cell_neighbors(_df(ORIGEN), tgt, 500.0)
        assert len(indices) == 1
        assert list(indices[0]) == []

    def test_kb_vacia_devuelve_un_arreglo_por_fila_target(self):
        vacia = pl.DataFrame(
            schema={
                "latitud": pl.Float64,
                "longitud": pl.Float64,
                "h3_cell_9": pl.Int64,
                "h3_cell_7": pl.Int64,
            }
        )
        indices, distancias = find_cell_neighbors(vacia, _df(ORIGEN, LEJOS_MISMA_CELDA), 500.0)
        assert len(indices) == 2
        assert all(len(x) == 0 for x in indices)
        assert len(distancias) == 2

    def test_la_celda_precalculada_sale_del_padre_no_del_punto(self):
        """`enrich.add_h3_cells` deriva `h3_cell_7` de `cell_to_parent`.

        ~6.5% de los puntos cerca de fronteras caen en una celda gruesa que
        NO es la ancestra de su celda res 9. Derivar del padre garantiza que
        la partición sea consistente con la jerarquía — misma regla que
        `h3_cell_5` (AGENTS.md, sección H3). El generador ya no deriva nada:
        lee la columna, así que el invariante se verifica donde se produce.
        """
        from src.etl.transformers.enrich import add_h3_cells

        # Un punto donde padre y celda-del-punto difieren, si existe en este
        # meridiano; es el caso que la regla protege.
        divergente = None
        for i in range(4000):
            lat = 20.60 + i * 0.0001
            c9 = h3i.latlng_to_cell(lat, -103.3440, 9)
            if h3i.cell_to_parent(c9, CELL_MATCH_RES) != h3i.latlng_to_cell(
                lat, -103.3440, CELL_MATCH_RES
            ):
                divergente = lat
                break

        lat = divergente if divergente is not None else 20.6736
        df = add_h3_cells(pl.DataFrame({"latitud": [lat], "longitud": [-103.3440]}))
        esperado = h3i.cell_to_parent(h3i.latlng_to_cell(lat, -103.3440, 9), CELL_MATCH_RES)
        assert df["h3_cell_7"][0] == esperado
        if divergente is not None:
            assert df["h3_cell_7"][0] != h3i.latlng_to_cell(lat, -103.3440, CELL_MATCH_RES)
