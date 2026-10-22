"""Las derivaciones que el ingest calcula en vuelo.

Aqui vive la interpretacion de lo que publica INEGI, asi que los casos no
son hipoteticos: los seis formatos de fecha y sus volumenes salen de medir
el Silver completo. Un formato mal leido no da error, da 14.5M de fechas en
NULL.

No necesitan servidor: son funciones puras sobre DataFrames.
"""

from datetime import date

import h3
import polars as pl
import pytest

from src.etl.transformers.enrich import add_h3_cells, normalize_fecha_alta
from src.etl.utils.clee_resolution_config import H3_BLOCKING_RES, H3_PARTITION_RES

#: Zocalo de la Ciudad de Mexico, como punto de referencia conocido.
ZOCALO = (19.4326, -99.1332)


def con_fechas(*valores: str | None) -> pl.DataFrame:
    """DataFrame de una columna `fecha_alta_str`."""
    return pl.DataFrame({"fecha_alta_str": list(valores)}, schema={"fecha_alta_str": pl.Utf8})


def fechas_de(*valores: str | None) -> list[date | None]:
    """Resultado de normalizar los valores dados."""
    return normalize_fecha_alta(con_fechas(*valores))["fecha_alta_date"].to_list()


class TestFormatosDeINEGI:
    """Los seis que aparecen en los datos reales."""

    def test_formato_1_ano_mes(self):
        """El mas comun: ~68M filas."""
        assert fechas_de("2010-07") == [date(2010, 7, 1)]

    def test_formato_2_con_espacio_de_sobra(self):
        """~15K filas lo traen; el strip va antes de todo."""
        assert fechas_de("2010-07 ", " 2010-07", "  2010-07  ") == [date(2010, 7, 1)] * 3

    def test_formato_3_separado_por_espacio(self):
        assert fechas_de("2010 07") == [date(2010, 7, 1)]

    def test_formato_4_separado_por_guion_bajo(self):
        """Solo 10 filas en 82.9M, y aun asi hay que leerlas."""
        assert fechas_de("2010_07") == [date(2010, 7, 1)]

    def test_formato_5_mes_en_espanol(self):
        """~14.5M filas. El segundo formato mas comun."""
        assert fechas_de("JULIO 2010") == [date(2010, 7, 1)]

    def test_formato_5_los_doce_meses(self):
        """Un mes mal traducido se lleva un mes entero de datos."""
        esperado = [date(2015, m, 1) for m in range(1, 13)]
        nombres = [
            "ENERO",
            "FEBRERO",
            "MARZO",
            "ABRIL",
            "MAYO",
            "JUNIO",
            "JULIO",
            "AGOSTO",
            "SEPTIEMBRE",
            "OCTUBRE",
            "NOVIEMBRE",
            "DICIEMBRE",
        ]
        assert fechas_de(*[f"{n} 2015" for n in nombres]) == esperado

    def test_formato_6_dia_mes_ano(self):
        """12 filas. El dia se descarta: el grano del dato es el mes."""
        assert fechas_de("06/10/2023") == [date(2023, 10, 1)]


class TestNormalizacion:
    def test_todo_cae_en_el_dia_primero(self):
        """El dato original no tiene dia; inventarlo falsearia precision."""
        resultados = fechas_de("2010-07", "JULIO 2010", "06/07/2010", "2010_07")
        assert all(f == date(2010, 7, 1) for f in resultados)

    def test_el_mes_de_un_digito_no_se_confunde(self):
        assert fechas_de("2010-01", "ENERO 2010") == [date(2010, 1, 1)] * 2

    def test_diciembre_no_se_corre_de_ano(self):
        assert fechas_de("DICIEMBRE 2015", "2015-12") == [date(2015, 12, 1)] * 2


class TestLoNoReconocido:
    """Queda NULL, que es informacion: INEGI publico algo nuevo."""

    @pytest.mark.parametrize(
        "valor",
        [
            None,
            "",
            "   ",
            "basura",
            "2010",  # falta el mes
            "07-2010",  # invertido
            "2010-13",  # mes imposible
            "MARCIANO 2010",  # mes que no existe
            "2010/07",  # separador no contemplado
        ],
    )
    def test_queda_en_null(self, valor):
        assert fechas_de(valor) == [None]

    def test_una_fila_mala_no_arrastra_a_las_buenas(self):
        """Sobre 4M de filas por corte, una rara no puede tumbar el lote."""
        assert fechas_de("2010-07", "basura", "JULIO 2011") == [
            date(2010, 7, 1),
            None,
            date(2011, 7, 1),
        ]

    def test_sin_la_columna_devuelve_todo_null(self):
        df = normalize_fecha_alta(pl.DataFrame({"otra": [1, 2]}))
        assert df["fecha_alta_date"].to_list() == [None, None]


def con_coords(*pares) -> pl.DataFrame:
    """DataFrame de `latitud`/`longitud`."""
    return pl.DataFrame(
        {"latitud": [p[0] for p in pares], "longitud": [p[1] for p in pares]},
        schema={"latitud": pl.Float64, "longitud": pl.Float64},
    )


class TestCeldasH3:
    def test_coincide_con_la_libreria(self):
        """La referencia es h3 directo: aqui no hay formula propia."""
        df = add_h3_cells(con_coords(ZOCALO))
        esperada = h3.str_to_int(h3.latlng_to_cell(*ZOCALO, H3_BLOCKING_RES))
        assert df["h3_cell_9"][0] == esperada

    def test_la_res_5_es_el_padre_de_la_res_9(self):
        """La regla que hace la jerarquia consistente.

        No se calcula del punto: en los bordes, el padre de la celda res 9 y
        la celda res 5 del punto pueden diferir, y lo que la resolucion CLEE
        necesita es que toda res 9 este dentro de su res 5.
        """
        df = add_h3_cells(con_coords(ZOCALO))
        cell9 = h3.int_to_str(df["h3_cell_9"][0])
        cell5 = h3.int_to_str(df["h3_cell_5"][0])
        assert h3.cell_to_parent(cell9, H3_PARTITION_RES) == cell5

    def test_dos_puntos_cercanos_comparten_celda(self):
        """La propiedad que hace util el blocking: ~174m de arista."""
        lat, lon = ZOCALO
        df = add_h3_cells(con_coords((lat, lon), (lat + 0.0001, lon + 0.0001)))
        assert df["h3_cell_9"][0] == df["h3_cell_9"][1]

    def test_dos_puntos_lejanos_no_la_comparten(self):
        df = add_h3_cells(con_coords(ZOCALO, (20.6736, -103.344)))  # CDMX y GDL
        assert df["h3_cell_9"][0] != df["h3_cell_9"][1]
        assert df["h3_cell_5"][0] != df["h3_cell_5"][1]

    def test_cabe_en_un_entero_de_64_bits(self):
        """La columna es BIGINT: si no cupiera, se truncaria en silencio."""
        df = add_h3_cells(con_coords(ZOCALO))
        assert df.schema["h3_cell_9"] == pl.Int64
        assert 0 < df["h3_cell_9"][0] < 2**63


class TestSinCoordenadas:
    """Un establecimiento sin geolocalizar no es un error de datos."""

    @pytest.mark.parametrize(
        "par",
        [
            (None, None),
            (19.4326, None),
            (None, -99.1332),
            (91.0, 0.0),  # latitud fuera de rango
            (-91.0, 0.0),
            (0.0, 181.0),  # longitud fuera de rango
            (0.0, -181.0),
        ],
    )
    def test_queda_en_null_sin_lanzar(self, par):
        """Fuera de rango, `h3.latlng_to_cell` lanza: la guarda va antes."""
        df = add_h3_cells(con_coords(par))
        assert df["h3_cell_9"][0] is None
        assert df["h3_cell_5"][0] is None

    def test_una_fila_sin_coords_no_arrastra_a_las_buenas(self):
        df = add_h3_cells(con_coords(ZOCALO, (None, None), ZOCALO))
        assert df["h3_cell_9"][1] is None
        assert df["h3_cell_9"][0] == df["h3_cell_9"][2] is not None

    def test_sin_las_columnas_devuelve_todo_null(self):
        df = add_h3_cells(pl.DataFrame({"otra": [1, 2]}))
        assert df["h3_cell_9"].to_list() == [None, None]
        assert df.schema["h3_cell_9"] == pl.Int64


class TestJuntas:
    """El ingest las aplica en cadena sobre el mismo DataFrame."""

    def test_se_encadenan_sin_perder_columnas(self):
        df = pl.DataFrame(
            {
                "id": ["a", "b"],
                "fecha_alta_str": ["2010-07", "JULIO 2011"],
                "latitud": [ZOCALO[0], None],
                "longitud": [ZOCALO[1], None],
            }
        )
        salida = add_h3_cells(normalize_fecha_alta(df))

        assert salida["id"].to_list() == ["a", "b"]
        assert salida["fecha_alta_date"].to_list() == [date(2010, 7, 1), date(2011, 7, 1)]
        assert salida["h3_cell_9"][0] is not None
        assert salida["h3_cell_9"][1] is None

    def test_el_orden_de_aplicacion_no_importa(self):
        df = pl.DataFrame(
            {
                "fecha_alta_str": ["2010-07"],
                "latitud": [ZOCALO[0]],
                "longitud": [ZOCALO[1]],
            }
        )
        una = add_h3_cells(normalize_fecha_alta(df))
        otra = normalize_fecha_alta(add_h3_cells(df))
        assert (
            una.sort(by="fecha_alta_date").to_dicts() == otra.sort(by="fecha_alta_date").to_dicts()
        )
