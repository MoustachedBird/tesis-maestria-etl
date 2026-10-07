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

_EARTH_RADIUS_M = 6_371_000.0
"""Radio medio de la Tierra en metros (constante fisica, NO configuracion).

Convierte entre metros y radianes en las dos haversines de este modulo:
el radio de query del BallTree (find_spatial_neighbors) y el post-filtro
exacto del blocking H3 (find_h3_neighbors). Debe ser LA MISMA en ambas
para que los dos generadores de candidatos compartan la semantica de
SPATIAL_RADIUS_M.
"""


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


def _valid_coords_mask(coords_deg: np.ndarray) -> np.ndarray:
    """Mascara booleana de filas con coordenadas utilizables.

    Utilizable = lat/lon presentes (no NaN, que es como llegan los NULL de
    Polars via to_numpy) y dentro del dominio geografico [-90,90]/[-180,180]
    -- el MISMO criterio con el que el ingest computa h3_cell_9, para que
    ambos generadores de candidatos excluyan exactamente las mismas filas
    (datos de encuesta: cualquier campo puede faltar; una fila sin coords
    obtiene 0 candidatos en vez de tumbar el BallTree con NaN).
    """
    lat = coords_deg[:, 0]
    lon = coords_deg[:, 1]
    return (
        np.isfinite(lat)
        & np.isfinite(lon)
        & (np.abs(lat) <= 90.0)
        & (np.abs(lon) <= 180.0)
    )


def find_spatial_neighbors(ref_df: pl.DataFrame, target_df: pl.DataFrame, radius_meters: float):
    """
    Ejecuta BallTree para encontrar indices vecinos.

    Los vecinos se retornan ordenados por distancia ascendente y, en caso
    de EMPATE exacto de distancia (coordenadas identicas: mismo edificio o
    plaza, comun en DENUE), por indice de KB ascendente -- mismo tie-break
    determinista que find_h3_neighbors, para que ambos metodos elijan el
    mismo candidato y las corridas sean reproducibles. El candidato mas
    cercano se evalua primero (un COMBINED_MATCH cercano gana sobre uno
    lejano y el break de los matches directos se adelanta).

    Filas sin coordenadas validas (NULL/NaN o fuera de dominio) reciben 0
    candidatos -- misma semantica que el blocking H3 (celda NULL), en vez
    de crashear el BallTree con NaN.

    Retorna: (indices, distances) -- arrays object de longitud
    target_df.height; distancias en METROS (consistente con
    find_h3_neighbors).
    """
    ref_coords_deg = ref_df.select(["latitud", "longitud"]).to_numpy()
    target_coords_deg = target_df.select(["latitud", "longitud"]).to_numpy()

    n_target = target_coords_deg.shape[0]
    indices = np.empty(n_target, dtype=object)
    distances = np.empty(n_target, dtype=object)
    for i in range(n_target):
        indices[i] = np.array([], dtype=np.int64)
        distances[i] = np.array([], dtype=np.float64)

    ref_valid = _valid_coords_mask(ref_coords_deg) if ref_coords_deg.size else np.array([], dtype=bool)
    tgt_valid = _valid_coords_mask(target_coords_deg) if target_coords_deg.size else np.array([], dtype=bool)
    if not ref_valid.any() or not tgt_valid.any():
        return indices, distances

    # Indices originales de las filas validas: los vecinos retornados deben
    # ser posicionales sobre ref_df COMPLETO (las listas _shared_kb_* se
    # construyen sin filtrar), asi que se traducen de subset -> original.
    ref_orig_idx = np.flatnonzero(ref_valid)
    tgt_orig_idx = np.flatnonzero(tgt_valid)

    radius_rad = radius_meters / _EARTH_RADIUS_M
    tree = BallTree(np.radians(ref_coords_deg[ref_valid]), metric="haversine")
    sub_indices, sub_distances = tree.query_radius(
        np.radians(target_coords_deg[tgt_valid]),
        r=radius_rad,
        return_distance=True,
        sort_results=True,
    )

    for row_i, nbrs, dists in zip(tgt_orig_idx, sub_indices, sub_distances):
        if len(nbrs) == 0:
            continue
        orig = ref_orig_idx[nbrs]
        dists_m = dists * _EARTH_RADIUS_M
        # lexsort: primario distancia, secundario indice de KB (tie-break)
        order = np.lexsort((orig, dists_m))
        indices[row_i] = orig[order]
        distances[row_i] = dists_m[order]

    return indices, distances


def find_h3_neighbors(ref_df: pl.DataFrame, target_df: pl.DataFrame, radius_meters: float):
    """
    Genera candidatos espaciales por blocking de celdas H3.

    NO sustituye al BallTree: es el generador del metodo COMPLEMENTARIO de
    propagacion (columna `clee_propagado_h3`), que corre en paralelo al
    baseline por radio (`clee_propagado_radio`) para comparar ambos metodos
    lado a lado. Misma interfaz de retorno que find_spatial_neighbors:
    (indices, distances).

    Algoritmo (todo vectorizado en Polars = multihilo, sin loop por fila):
      1. grid_disk(cell, H3_GRID_DISK_K) SOLO sobre las celdas distintas del
         target (muchas filas comparten celda) -> pares (celda_target,
         celda_vecina) acumulados en arrays numpy preasignados (RAM acotada:
         16 bytes/par). k=3 en res 9 cubre >=500m en el peor caso.
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

    # 1. grid_disk solo por celda distinta del target (loop C corto).
    # RAM: los pares (celda_target, celda_vecina) se acumulan en arrays
    # numpy preasignados, NUNCA en una lista de tuplas Python -- con ~1M de
    # celdas distintas x 37 vecinos (k=3) la lista costaria varios GB de
    # objetos; los arrays cuestan 16 bytes/par (~0.6GB por millon de
    # celdas), dentro del presupuesto de la maquina (36GB).
    distinct_cells = tg.get_column("tgt_cell").drop_nulls().unique().to_list()
    if not distinct_cells:
        return _empty_result()

    # grid_disk(k) retorna a lo mas 3k(k+1)+1 celdas (37 con k=3; menos si
    # el disco toca uno de los 12 pentagonos del grid, por eso se rastrea
    # `pos` en vez de asumir el maximo).
    max_disk = 3 * H3_GRID_DISK_K * (H3_GRID_DISK_K + 1) + 1
    tgt_cells_arr = np.empty(len(distinct_cells) * max_disk, dtype=np.int64)
    nb_cells_arr = np.empty(len(distinct_cells) * max_disk, dtype=np.int64)
    pos = 0
    for cell in distinct_cells:
        nbs = h3i.grid_disk(cell, H3_GRID_DISK_K)
        n = len(nbs)
        tgt_cells_arr[pos : pos + n] = cell
        nb_cells_arr[pos : pos + n] = nbs
        pos += n
    disk = pl.DataFrame({"tgt_cell": tgt_cells_arr[:pos], "nb_cell": nb_cells_arr[:pos]})
    del tgt_cells_arr, nb_cells_arr

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
        # kb_idx como llave terciaria: tie-break determinista en empates
        # exactos de distancia (coords identicas), igual que el BallTree.
        .sort(["tgt_idx", "dist_m", "kb_idx"])
        .group_by("tgt_idx", maintain_order=True)
        .agg(pl.col("kb_idx"), pl.col("dist_m"))
        .collect(engine="streaming")
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


# ---------------------------------------------------------------------------
# Tiers de score para comparar candidatos entre si (argmax).
#
# NO son calibracion -- no van a .env. Son la codificacion numerica de la
# precedencia que la cascada ya tenia implicita en el orden de sus paths
# (A > B > C). Separarlos por 1000 garantiza que ningun score de un tier
# inferior pueda superar a uno de un tier superior, sea cual sea el ratio.
# ---------------------------------------------------------------------------
RAZON_TIER = 2000.0
"""Base del score para un match por razon social (Path A).

Path A es la evidencia mas fuerte de la cascada: la razon social es el
nombre legal y coincide mucho menos por casualidad que el comercial. Al
ser el tier mas alto, cualquier RAZON_MATCH le gana a cualquier
NAME_MATCH, replicando el `return` temprano de Path A sobre Path B.
"""

NAME_TIER = 1000.0
"""Base del score para un match por nombre comercial (Path B).

Queda por encima de COMBINED (que no suma mas de 600) y por debajo de
RAZON. Reproduce que Path B gane a Path C pero pierda contra Path A.
"""

COMBINED_SIGNAL_WEIGHT = 100.0
"""Peso de cada senal debil en el score de un match combinado (Path C).

Con 5 senales posibles el techo de Path C es 5*100 + 100 (ratio parcial)
= 600, estrictamente menor que NAME_TIER. Dentro del tier, mas senales
siempre le gana a menos senales.
"""

PERFECT_RAZON_SCORE = RAZON_TIER + 100.0
"""Score maximo alcanzable: razon social identica (ratio 100).

Nada puede superarlo, asi que el loop de candidatos puede cortar en seco
al encontrarlo sin cambiar el resultado del argmax (ver `_best_candidate`
en entity_resolution.py). Es la salida temprana que compensa el costo de
haber eliminado el `break` al primer match.
"""

PERFECT_NAME_SCORE = NAME_TIER + 100.0
"""Score maximo alcanzable cuando el target NO tiene razon social usable.

Si `len(t_raz) <= 2`, Path A no puede disparar contra ningun candidato,
asi que el techo real del target baja a este valor y el corte temprano
aplica igual. Cubre el caso mayoritario del DENUE: razon social vacia.
"""


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
) -> tuple[bool, str, float]:
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

    El tercer elemento del retorno es un score comparable ENTRE candidatos,
    para que el loop que llama pueda quedarse con el mejor en vez de con el
    primero (ver `_best_candidate` en entity_resolution.py). El score NO
    altera la cascada: los tiers replican la precedencia que ya existia por
    el orden de los `return` (Path A > Path B > Path C), de modo que un par
    dado se clasifica hoy exactamente igual que antes del cambio.

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
        Tupla (es_match: bool, estrategia: str, score: float)
        Estrategias posibles: "RAZON_MATCH", "NAME_MATCH", "COMBINED_MATCH",
                              "DIFF_ACTIVITY", "NONE"
        El score solo es significativo cuando es_match es True; en los no-match
        es 0.0 y no debe usarse para ordenar nada.
    """

    # === GATE OBLIGATORIO: ACTIVIDAD ECONOMICA (SCIAN) ===
    # Un negocio no puede ser el mismo si su giro cambio drasticamente.
    if not passes_activity_gate(t_act, kb_act):
        return False, "DIFF_ACTIVITY", 0.0

    # === PATH A: MATCH DIRECTO POR RAZON SOCIAL ===
    if len(t_raz) > 2 and kb_razon and len(kb_razon) > 2:
        razon_ratio = fuzz.token_sort_ratio(t_raz, kb_razon)
        if razon_ratio > LEGAL_NAME_MATCH_THRESH:
            return True, "RAZON_MATCH", RAZON_TIER + razon_ratio

    # === PATH B: MATCH DIRECTO POR NOMBRE COMERCIAL ===
    # El ratio se calcula una sola vez y se reutiliza como senal parcial
    # en Path C (mismo par, mismo scorer -- evita un fuzzy duplicado por
    # candidato en el hot loop).
    name_ratio = None
    if len(t_name) > 2 and kb_name and len(kb_name) > 2:
        name_ratio = fuzz.token_sort_ratio(t_name, kb_name)
        if name_ratio > BUSINESS_NAME_MATCH_THRESH:
            return True, "NAME_MATCH", NAME_TIER + name_ratio

    # === PATH C: MATCH COMBINADO (senales debiles acumuladas) ===
    # Contamos cuantas senales coinciden. Si alcanzamos MIN_COMBINED_SIGNALS,
    # declaramos match. Ninguna senal individual basta por si sola.
    signals = 0

    # Senal 1: Nombre comercial parcial (ratio reutilizado de Path B)
    if name_ratio is not None and name_ratio > PARTIAL_NAME_THRESH:
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
        # Mas senales siempre gana; el ratio parcial desempata dentro del tier.
        combined = signals * COMBINED_SIGNAL_WEIGHT + (name_ratio or 0)
        return True, "COMBINED_MATCH", combined

    return False, "NONE", 0.0
