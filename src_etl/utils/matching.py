"""
Utilidades de matching espacial y difuso para la resolucion CLEE.

Contiene los tres bloques del algoritmo de matching:
  - Pre-filtro espacial: BallTree haversine (`find_spatial_neighbors`)
  - Gate de actividad SCIAN memoizado (`passes_activity_gate`)
  - Cascada de matching por candidato (`calculate_match_score`)

Los umbrales viven en `clee_resolution_config.py` para calibracion sin
tocar codigo. Este modulo se importa dentro del hot loop de
`entity_resolution.py`: cualquier cambio aqui impacta directamente el
tiempo de corrida sobre ~100M de filas.
"""

import uuid
from functools import cache

import numpy as np
import polars as pl
from rapidfuzz import fuzz
from sklearn.neighbors import BallTree

from src_etl.utils.clee_resolution_config import (
    ACTIVITY_GATE_THRESH,
    BUSINESS_NAME_MATCH_THRESH,
    LEGAL_NAME_MATCH_THRESH,
    MIN_COMBINED_SIGNALS,
    MIN_EMAIL_LENGTH,
    MIN_TEL_LENGTH,
    MIN_WEB_LENGTH,
    PARTIAL_NAME_THRESH,
    TEXT_COLS_TO_SANITIZE,
)


def generate_temp_clee() -> str:
    """
    Genera el sufijo aleatorio (12 hex) de un CLEE sintetico.

    Se usa en `assign_new_clees()` para formar `NEW_{prefix}-{uuid12}`.
    NO es estable entre corridas con --force: no usarlo como FK permanente
    en sistemas downstream (ver AGENTS.md, seccion CLEE Resolution).
    """
    return uuid.uuid4().hex[:12]


def clean_contact_columns(df: pl.DataFrame) -> pl.DataFrame:
    """
    Normaliza campos de contacto para comparacion exacta en el matching.

    Punto UNICO de sanitizacion pre-matching (antes estaba repartida en
    3 lugares: aqui, _load_period y build_single_period_kb):
      1. Rellena con "" los nulos de TODAS las columnas de texto del
         matching (TEXT_COLS_TO_SANITIZE) presentes en el DataFrame
         (datos de encuesta: cualquier campo puede venir vacio).
      2. Genera las columnas derivadas que consume la cascada:
         - tel_clean: telefono solo digitos
         - email_clean: email en minusculas sin espacios
         - www_clean: web en minusculas sin espacios

    Debe aplicarse a todo DataFrame que entre al matching (target o KB);
    `CLEEResolver._load_period()` lo hace automaticamente.
    """
    fill_exprs = [pl.col(c).fill_null("") for c in TEXT_COLS_TO_SANITIZE if c in df.columns]
    if fill_exprs:
        df = df.with_columns(fill_exprs)

    return df.with_columns(
        [
            pl.col("telefono").str.replace_all(r"[^0-9]", "").alias("tel_clean"),
            pl.col("correoelec").str.to_lowercase().str.strip_chars().alias("email_clean"),
            pl.col("www").str.to_lowercase().str.strip_chars().alias("www_clean"),
        ]
    )


def find_spatial_neighbors(ref_df: pl.DataFrame, target_df: pl.DataFrame, radius_meters: float):
    """
    Ejecuta BallTree para encontrar indices vecinos.

    Los vecinos se retornan ordenados por distancia ascendente
    (sort_results=True): el candidato mas cercano se evalua primero,
    lo que mejora precision (un COMBINED_MATCH cercano gana sobre uno
    lejano) y adelanta el break de los matches directos.

    Retorna: (indices, distances)
    """
    ref_coords = np.radians(ref_df.select(["latitud", "longitud"]).to_numpy())
    target_coords = np.radians(target_df.select(["latitud", "longitud"]).to_numpy())

    # Radio Tierra ~ 6371000m
    radius_rad = radius_meters / 6371000

    tree = BallTree(ref_coords, metric="haversine")
    return tree.query_radius(
        target_coords, r=radius_rad, return_distance=True, sort_results=True
    )


_EARTH_RADIUS_M = 6_371_000.0


def find_h3_neighbors(ref_df: pl.DataFrame, target_df: pl.DataFrame, radius_meters: float):
    """
    Genera candidatos espaciales por blocking de celdas H3 (sustituto del
    BallTree). Misma interfaz de retorno: (indices, distances).

    Algoritmo (todo vectorizado en Polars = multihilo, sin loop por fila):
      1. grid_disk(cell, H3_GRID_DISK_K) SOLO sobre las celdas distintas del
         target (muchas filas comparten celda) -> pares (celda_target,
         celda_vecina). k=3 en res 9 cubre >=500m en el peor caso.
      2. Join target -> celdas vecinas -> join con KB por celda: produce los
         pares candidatos (tgt_idx, kb_idx) como hash joins de Polars.
      3. Post-filtro haversine EXACTO <= radius_meters (en el motor de
         expresiones), recuperando la misma semantica de radio que el
         BallTree. Se corre en modo streaming para acotar el pico de RAM.
      4. Ordena por distancia ascendente y agrupa por target -> arrays por
         fila (mismo formato que query_radius con sort_results=True).

    Requiere columna `h3_cell_9` en ambos DataFrames (filas sin coords la
    tienen NULL -> 0 candidatos, igual que el baseline las descartaria).

    Retorna: (indices, distances) -- np.ndarray(object) de longitud
    target_df.height; cada elemento es un np.ndarray de indices posicionales
    de KB (o distancias) ordenado por cercania.
    """
    from h3.api import basic_int as h3i

    from src_etl.utils.clee_resolution_config import H3_GRID_DISK_K

    n_target = target_df.height

    def _empty_result():
        arr = np.empty(n_target, dtype=object)
        for i in range(n_target):
            arr[i] = np.array([], dtype=np.int64)
        dist = np.empty(n_target, dtype=object)
        for i in range(n_target):
            dist[i] = np.array([], dtype=np.float64)
        return arr, dist

    if ref_df.height == 0 or n_target == 0:
        return _empty_result()

    kb = ref_df.select(
        pl.int_range(pl.len(), dtype=pl.Int64).alias("kb_idx"),
        pl.col("h3_cell_9").alias("kb_cell"),
        pl.col("latitud").alias("kb_lat"),
        pl.col("longitud").alias("kb_lon"),
    ).drop_nulls("kb_cell")

    tg = target_df.select(
        pl.int_range(pl.len(), dtype=pl.Int64).alias("tgt_idx"),
        pl.col("h3_cell_9").alias("tgt_cell"),
        pl.col("latitud").alias("tgt_lat"),
        pl.col("longitud").alias("tgt_lon"),
    )

    # 1. grid_disk solo por celda distinta del target (loop C corto)
    distinct_cells = tg.get_column("tgt_cell").drop_nulls().unique().to_list()
    if not distinct_cells:
        return _empty_result()

    disk_pairs: list[tuple[int, int]] = []
    for cell in distinct_cells:
        for nb in h3i.grid_disk(cell, H3_GRID_DISK_K):
            disk_pairs.append((cell, nb))
    disk = pl.DataFrame(
        disk_pairs, schema={"tgt_cell": pl.Int64, "nb_cell": pl.Int64}, orient="row"
    )

    # 2-3. Joins + haversine exacto (lazy/streaming para acotar RAM)
    rad = np.pi / 180.0
    pairs = (
        tg.lazy()
        .join(disk.lazy(), on="tgt_cell", how="inner")
        .join(kb.lazy(), left_on="nb_cell", right_on="kb_cell", how="inner")
        .with_columns(
            (
                2
                * _EARTH_RADIUS_M
                * (
                    (
                        ((pl.col("kb_lat") - pl.col("tgt_lat")) * rad / 2).sin() ** 2
                        + (pl.col("tgt_lat") * rad).cos()
                        * (pl.col("kb_lat") * rad).cos()
                        * ((pl.col("kb_lon") - pl.col("tgt_lon")) * rad / 2).sin() ** 2
                    )
                    .sqrt()
                    .arcsin()
                )
            ).alias("dist_m")
        )
        .filter(pl.col("dist_m") <= radius_meters)
        .select("tgt_idx", "kb_idx", "dist_m")
        .sort(["tgt_idx", "dist_m"])
        .group_by("tgt_idx", maintain_order=True)
        .agg(pl.col("kb_idx"), pl.col("dist_m"))
        .collect(streaming=True)
    )

    # 4. Dispersar a arrays por fila alineados a 0..n_target-1
    indices = np.empty(n_target, dtype=object)
    distances = np.empty(n_target, dtype=object)
    for i in range(n_target):
        indices[i] = np.array([], dtype=np.int64)
        distances[i] = np.array([], dtype=np.float64)

    for tgt_idx, kb_idx_list, dist_list in zip(
        pairs.get_column("tgt_idx").to_list(),
        pairs.get_column("kb_idx").to_list(),
        pairs.get_column("dist_m").to_list(),
    ):
        indices[tgt_idx] = np.asarray(kb_idx_list, dtype=np.int64)
        distances[tgt_idx] = np.asarray(dist_list, dtype=np.float64)

    return indices, distances


@cache
def _activity_ratio_cached(act_a: str, act_b: str) -> float:
    """token_set_ratio memoizado para pares de actividades SCIAN.

    nombre_act proviene de un catalogo de ~1,127 valores, asi que el
    universo de pares distintos es acotado (~1.27M en el peor caso) y el
    hit-rate del cache es masivo: el gate pasa de costar un fuzzy por par
    candidato a un lookup de dict.
    """
    return fuzz.token_set_ratio(act_a, act_b)


def passes_activity_gate(t_act: str, kb_act: str) -> bool:
    """Gate obligatorio de actividad SCIAN (memoizado).

    Fast path: igualdad exacta (el caso dominante entre vecinos del mismo
    giro) evita incluso el lookup. La clave del cache se normaliza por
    orden lexicografico porque token_set_ratio es simetrico.
    """
    if not kb_act:
        return False
    if t_act == kb_act:
        return True
    if t_act <= kb_act:
        score = _activity_ratio_cached(t_act, kb_act)
    else:
        score = _activity_ratio_cached(kb_act, t_act)
    return score > ACTIVITY_GATE_THRESH


def calculate_match_score(
    t_name: str,
    t_raz: str,
    t_act: str,
    t_tel: str,
    t_email: str,
    t_web: str,
    t_cp: str,
    kb_name: str,
    kb_razon: str,
    kb_act: str,
    kb_tel: str,
    kb_email: str,
    kb_www: str,
    kb_cp: str,
) -> tuple[bool, str]:
    """
    Calcula el match siguiendo una cascada jerarquica con umbrales calibrables.

    Compara un registro objetivo contra UN candidato de la KB. Recibe
    escalares planos (no dicts) para evitar materializar el target como
    lista de diccionarios en el hot loop.

    GATE (obligatorio): Actividad SCIAN fuzzy > ACTIVITY_GATE_THRESH
                        (memoizado -- ver passes_activity_gate)
    Path A: Razon social fuzzy > LEGAL_NAME_MATCH_THRESH  -> match directo
    Path B: Nombre comercial fuzzy > BUSINESS_NAME_MATCH_THRESH -> match directo
    Path C: 2+ senales debiles coinciden (nombre parcial, tel, email, web, CP)
            -> match combinado

    Los umbrales se importan desde clee_resolution_config.py para facilitar
    calibracion futura.

    Args:
        t_name: Nombre comercial del registro objetivo
        t_raz: Razon social del registro objetivo
        t_act: Actividad economica del registro objetivo
        t_tel: Telefono limpio del registro objetivo
        t_email: Email limpio del registro objetivo
        t_web: Web limpia del registro objetivo
        t_cp: Codigo postal del registro objetivo
        kb_name: Nombre comercial del candidato KB
        kb_razon: Razon social del candidato KB
        kb_act: Actividad economica del candidato KB
        kb_tel: Telefono limpio del candidato KB
        kb_email: Email limpio del candidato KB
        kb_www: Web limpia del candidato KB
        kb_cp: Codigo postal del candidato KB

    Returns:
        Tupla (es_match: bool, estrategia: str)
        Estrategias posibles: "RAZON_MATCH", "NAME_MATCH", "COMBINED_MATCH",
                              "DIFF_ACTIVITY", "NONE"
    """

    # === GATE OBLIGATORIO: ACTIVIDAD ECONOMICA (SCIAN) ===
    # Un negocio no puede ser el mismo si su giro cambio drasticamente.
    if not passes_activity_gate(t_act, kb_act):
        return False, "DIFF_ACTIVITY"

    # === PATH A: MATCH DIRECTO POR RAZON SOCIAL ===
    if len(t_raz) > 2 and kb_razon and len(kb_razon) > 2:
        if fuzz.token_sort_ratio(t_raz, kb_razon) > LEGAL_NAME_MATCH_THRESH:
            return True, "RAZON_MATCH"

    # === PATH B: MATCH DIRECTO POR NOMBRE COMERCIAL ===
    if len(t_name) > 2 and kb_name and len(kb_name) > 2:
        if fuzz.token_sort_ratio(t_name, kb_name) > BUSINESS_NAME_MATCH_THRESH:
            return True, "NAME_MATCH"

    # === PATH C: MATCH COMBINADO (senales debiles acumuladas) ===
    # Contamos cuantas senales coinciden. Si alcanzamos MIN_COMBINED_SIGNALS,
    # declaramos match. Ninguna senal individual basta por si sola.
    signals = 0

    # Senal 1: Nombre comercial parcial
    if len(t_name) > 2 and kb_name and len(kb_name) > 2:
        if fuzz.token_sort_ratio(t_name, kb_name) > PARTIAL_NAME_THRESH:
            signals += 1

    # Senal 2: Telefono exacto
    if len(t_tel) >= MIN_TEL_LENGTH and kb_tel and t_tel == kb_tel:
        signals += 1

    # Senal 3: Email exacto
    if len(t_email) >= MIN_EMAIL_LENGTH and kb_email and t_email == kb_email:
        signals += 1

    # Senal 4: Web exacto
    if len(t_web) >= MIN_WEB_LENGTH and kb_www and t_web == kb_www:
        signals += 1

    # Senal 5: Codigo postal igual
    if t_cp and kb_cp and t_cp == kb_cp:
        signals += 1

    if signals >= MIN_COMBINED_SIGNALS:
        return True, "COMBINED_MATCH"

    return False, "NONE"
