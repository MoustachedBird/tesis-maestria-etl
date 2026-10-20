"""Derivaciones que el ingest calcula en vuelo, sobre el DataFrame.

Antes eran dos etapas aparte que hacian `UPDATE` sobre la tabla ya cargada:
una para la fecha y otra para las celdas H3. Sobre 82.9M filas eso es
reescribir la tabla dos veces despues de haberla escrito una. Calcularlas
aqui --cuando los datos todavia estan en memoria, antes del `COPY`-- las
vuelve gratis: el ingest las lleva ya puestas y las etapas desaparecen.

Son funciones puras sobre DataFrames de Polars: no abren conexion, no leen
configuracion y no registran nada. Eso las hace probables sin servidor, que
importa porque aqui vive la interpretacion de los formatos de INEGI.
"""

from __future__ import annotations

import h3
import polars as pl

from src.etl.utils.clee_resolution_config import H3_BLOCKING_RES, H3_PARTITION_RES

#: Meses en espanol como los escribe INEGI, a su numero.
SPANISH_MONTHS: dict[str, str] = {
    "ENERO": "01",
    "FEBRERO": "02",
    "MARZO": "03",
    "ABRIL": "04",
    "MAYO": "05",
    "JUNIO": "06",
    "JULIO": "07",
    "AGOSTO": "08",
    "SEPTIEMBRE": "09",
    "OCTUBRE": "10",
    "NOVIEMBRE": "11",
    "DICIEMBRE": "12",
}

#: Los seis formatos con que INEGI escribe `fecha_alta_str`, con su volumen
#: medido sobre el Silver completo. Todos se normalizan al dia 1 del mes:
#: el dato original no tiene dia, asi que inventarlo seria falsear precision.
#:
#:   1. `YYYY-MM`            ~68M filas
#:   2. `YYYY-MM ` (sobra un espacio)   15K
#:   3. `YYYY MM`             7K
#:   4. `YYYY_MM`             10
#:   5. `MES_ESPANOL AAAA`   ~14.5M  (p. ej. "JULIO 2010")
#:   6. `DD/MM/YYYY`          12
#:
#: Lo que no encaja en ninguno queda NULL, que es informacion: dice que
#: INEGI publico un formato nuevo y que hay que mirarlo.
_ANO_MES = r"^\d{4}-\d{2}$"
_MES_ANO = r"^[A-ZÁÉÍÓÚÑ]+ \d{4}$"
_DIA_MES_ANO = r"^\d{2}/\d{2}/\d{4}$"

#: Rango valido de coordenadas. Fuera de el, `h3.latlng_to_cell` lanza
#: error, asi que la guarda va antes y no despues.
_LAT_MIN, _LAT_MAX = -90.0, 90.0
_LON_MIN, _LON_MAX = -180.0, 180.0


def normalize_fecha_alta(df: pl.DataFrame) -> pl.DataFrame:
    """Deriva `fecha_alta_date` desde `fecha_alta_str`.

    Reconoce los seis formatos de `SPANISH_MONTHS` y `_ANO_MES` y los lleva
    todos al primer dia del mes. Lo no reconocido queda NULL.

    Args:
        df: DataFrame con la columna `fecha_alta_str`.

    Returns:
        pl.DataFrame: el mismo, con `fecha_alta_date` agregada o reemplazada.
    """
    if "fecha_alta_str" not in df.columns:
        return df.with_columns(fecha_alta_date=pl.lit(None, dtype=pl.Date))

    crudo = pl.col("fecha_alta_str").str.strip_chars()

    # Formatos 1-4: las cuatro variantes de YYYY-MM se unifican cambiando el
    # separador, asi que una sola rama las cubre.
    ano_mes = crudo.str.replace_all("_", "-").str.replace_all(" ", "-")

    # Formato 5: el nombre del mes se traduce con un replace encadenado. Es
    # un literal por mes, no una regex: los nombres son doce y fijos.
    mes_ano = crudo.str.to_uppercase()
    mes_traducido = mes_ano.str.split(" ").list.first()
    for nombre, numero in SPANISH_MONTHS.items():
        mes_traducido = mes_traducido.str.replace_all(f"^{nombre}$", numero)
    desde_mes_ano = (
        mes_ano.str.split(" ").list.last() + pl.lit("-") + mes_traducido + pl.lit("-01")
    )

    # Formato 6: DD/MM/YYYY -> se toma ano y mes, el dia se descarta.
    # `null_on_oob` no es opcional: Polars evalua TODAS las ramas de un
    # when/then, asi que esta corre tambien sobre las filas sin barras, y
    # sin el un "2010-07" tumbaria el lote entero con "index out of bounds".
    partes = crudo.str.split("/")
    desde_dia_mes_ano = (
        partes.list.get(2, null_on_oob=True)
        + pl.lit("-")
        + partes.list.get(1, null_on_oob=True)
        + pl.lit("-01")
    )

    texto_iso = (
        pl.when(crudo.str.contains(_MES_ANO))
        .then(desde_mes_ano)
        .when(crudo.str.contains(_DIA_MES_ANO))
        .then(desde_dia_mes_ano)
        .when(ano_mes.str.contains(_ANO_MES))
        .then(ano_mes + pl.lit("-01"))
        .otherwise(None)
    )

    return df.with_columns(
        fecha_alta_date=texto_iso.str.to_date("%Y-%m-%d", strict=False)
    )


def add_h3_cells(df: pl.DataFrame) -> pl.DataFrame:
    """Deriva `h3_cell_9` y `h3_cell_5` desde las coordenadas.

    `h3_cell_9` es la clave de blocking de la resolucion CLEE (arista ~174m)
    y `h3_cell_5` la de particionado espacial (~8.5km).

    **`h3_cell_5` se deriva como PADRE de `h3_cell_9`, no del punto.**
    Calcular ambas desde las coordenadas daria casi siempre lo mismo, pero
    no siempre: en los bordes, el padre de la celda res 9 y la celda res 5
    del mismo punto pueden diferir. Lo que la resolucion necesita es que la
    jerarquia sea consistente --que toda res 9 este dentro de su res 5--, no
    que cada una sea la mas cercana al punto.

    Las filas sin coordenadas, o con coordenadas fuera de rango, quedan en
    NULL en ambas columnas: no son un error de datos sino un
    establecimiento que INEGI publico sin geolocalizar.

    Args:
        df: DataFrame con `latitud` y `longitud`.

    Returns:
        pl.DataFrame: el mismo, con `h3_cell_9` y `h3_cell_5` agregadas.
    """
    vacio = {
        "h3_cell_9": pl.lit(None, dtype=pl.Int64),
        "h3_cell_5": pl.lit(None, dtype=pl.Int64),
    }
    if not {"latitud", "longitud"} <= set(df.columns):
        return df.with_columns(**vacio)

    geolocalizable = (
        pl.col("latitud").is_not_null()
        & pl.col("longitud").is_not_null()
        & pl.col("latitud").is_between(_LAT_MIN, _LAT_MAX)
        & pl.col("longitud").is_between(_LON_MIN, _LON_MAX)
    )

    def celdas(fila: dict) -> dict[str, int | None]:
        """Par (res 9, res 5) de un punto, o (None, None) si no aplica.

        Args:
            fila: con `latitud` y `longitud`.

        Returns:
            dict: `h3_cell_9` y `h3_cell_5` como entero de 64 bits.
        """
        lat, lon = fila["latitud"], fila["longitud"]
        if lat is None or lon is None:
            return {"h3_cell_9": None, "h3_cell_5": None}
        cell9 = h3.latlng_to_cell(lat, lon, H3_BLOCKING_RES)
        return {
            "h3_cell_9": h3.str_to_int(cell9),
            "h3_cell_5": h3.str_to_int(h3.cell_to_parent(cell9, H3_PARTITION_RES)),
        }

    # `map_elements` sobre una struct y no dos columnas sueltas: asi
    # `cell_to_parent` reusa la celda ya calculada en vez de recalcularla.
    return df.with_columns(
        pl.when(geolocalizable)
        .then(
            pl.struct("latitud", "longitud").map_elements(
                celdas,
                return_dtype=pl.Struct(
                    {"h3_cell_9": pl.Int64, "h3_cell_5": pl.Int64}
                ),
            )
        )
        .otherwise(None)
        .alias("_h3")
    ).with_columns(
        h3_cell_9=pl.col("_h3").struct.field("h3_cell_9"),
        h3_cell_5=pl.col("_h3").struct.field("h3_cell_5"),
    ).drop("_h3")
