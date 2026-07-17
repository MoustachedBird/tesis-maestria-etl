"""
Entity Resolution: Funciones de propagacion de CLEEs entre periodos.

Este modulo contiene la logica aislada de propagacion de identidades.
La funcion principal `propagate_clees()` es el punto de intercambio
para futuros algoritmos de matching -- mientras respete la interfaz
(recibe target + ref_kb, retorna target con clee_propagado_radio parcialmente
poblado), el orquestador no necesita cambios.

Paralelismo: El loop de matching es embarrassingly parallel (cada fila
es independiente). `propagate_clees()` particiona las filas pendientes
en chunks y las distribuye entre N workers via multiprocessing.Pool.

La KB se comparte entre workers via fork() + copy-on-write (COW).
Se asigna a variables globales del modulo en el proceso padre antes
del fork, y el initializer del Pool las referencia en cada hijo sin
serializacion. Esto mantiene el uso de RAM practicamente igual al
modo secuencial (~2 GB extra para chunks + resultados, no N copias).
"""

import gc
import hashlib
import logging
import multiprocessing as mp
import os
import time
from collections.abc import Callable

import numpy as np
import polars as pl

from src_etl.utils.clee_resolution_config import (
    KB_MAX_ROWS_PER_PARTITION,
    MATCH_PARALLEL_THRESHOLD,
    MATCH_WORKERS,
)
from src_etl.utils.matching import (
    calculate_match_score,
    find_h3_neighbors,
    find_spatial_neighbors,
    generate_temp_clee,
)

logger = logging.getLogger(__name__)

KB_COLUMNS = [
    "clee_id",
    "latitud",
    "longitud",
    "h3_cell_9",
    "nom_estab",
    "raz_social",
    "nombre_act",
    "tel_clean",
    "email_clean",
    "www_clean",
    "cod_postal",
]
"""Columnas minimas que la KB de referencia necesita para el matching.

Contrato de `build_single_period_kb()`: la KB retornada tiene EXACTAMENTE
estas columnas. `clee_id` es la identidad a propagar, NEUTRAL respecto al
metodo: `build_single_period_kb(df_ref, master_column)` copia ahi el valor
de la columna del metodo correspondiente (clee_propagado_radio o
clee_propagado_h3). `latitud`/`longitud` alimentan el post-filtro exacto y
`h3_cell_9` el blocking H3 (ignorada por el generador BallTree). Las de
texto ausentes en el periodo se crean como "".
"""

PROGRESS_LOG_INTERVAL = 100_000
"""Cada cuantas filas target el loop secuencial loguea progreso.

Solo aplica al modo secuencial (`_run_matching_sequential`); en modo
paralelo el progreso se reporta por chunk completado. Subirlo reduce ruido
en logs de periodos grandes; bajarlo da feedback mas granular en debug.
"""

TARGET_MATCH_COLUMNS = [
    "nom_estab",
    "raz_social",
    "nombre_act",
    "tel_clean",
    "email_clean",
    "www_clean",
    "cod_postal",
]
"""Columnas del target que participan en el matching, en orden posicional.

El orden DEBE coincidir con los primeros 7 parametros de
`calculate_match_score()` (t_name, t_raz, t_act, t_tel, t_email, t_web,
t_cp): `_propagate_single_kb()` extrae estas columnas como tuple de listas
y los loops las desempaquetan por posicion. Se usan listas columnares en
vez de `to_dicts()` para no materializar millones de dicts de ~20 campos
(ahorra GBs de RAM y el costo de reconstruir el DataFrame).
"""

# ---------------------------------------------------------------------------
# Variables globales para compartir KB entre workers via fork() + COW.
# Se asignan en el proceso padre justo antes de crear el Pool.
# Los hijos las heredan sin copia (copy-on-write del OS).
# NUNCA escribir en estas desde un worker -- rompe COW.
# ---------------------------------------------------------------------------
_shared_kb_clee_ids: list[str] = []
_shared_kb_names: list[str] = []
_shared_kb_razones: list[str] = []
_shared_kb_acts: list[str] = []
_shared_kb_tels: list[str] = []
_shared_kb_emails: list[str] = []
_shared_kb_wwws: list[str] = []
_shared_kb_cps: list[str] = []


def _init_worker() -> None:
    """
    Initializer del Pool: no-op intencional.

    Los workers heredan las variables globales _shared_kb_* del padre
    via fork(). Este initializer existe solo para documentar el patron
    y como punto de extension si se necesita setup adicional por worker.
    """
    pass


def _match_chunk(
    chunk_data: tuple[int, tuple[list[str], ...], list[np.ndarray]],
) -> tuple[int, list[str | None]]:
    """
    Worker function: procesa un chunk de filas target contra la KB completa.

    Se ejecuta en un proceso hijo. Accede a la KB via variables globales
    del modulo (heredadas por fork, COW, sin copia real en RAM). El target
    llega como tuple de listas columnares (TARGET_MATCH_COLUMNS), mucho mas
    barato de picklear que listas de dicts.

    Args:
        chunk_data: Tupla (chunk_id, target_cols_slice, indices_slice)

    Returns:
        Tupla (chunk_id, resultados). chunk_id se usa para reordenar
        los resultados cuando se usa imap_unordered.
    """
    chunk_id, target_cols, indices_slice = chunk_data
    t_names, t_razones, t_acts, t_tels, t_emails, t_webs, t_cps = target_cols

    # Referencia local a las globales (micro-optimizacion: evita LOAD_GLOBAL repetido)
    kb_clees = _shared_kb_clee_ids
    kb_names = _shared_kb_names
    kb_razones = _shared_kb_razones
    kb_acts = _shared_kb_acts
    kb_tels = _shared_kb_tels
    kb_emails = _shared_kb_emails
    kb_wwws = _shared_kb_wwws
    kb_cps = _shared_kb_cps

    results: list[str | None] = []

    for idx in range(len(t_names)):
        neighbors = indices_slice[idx]
        if len(neighbors) == 0:
            results.append(None)
            continue

        # Vecinos ordenados por distancia: el primer COMBINED es el mas
        # cercano y se conserva; un match directo (mas fuerte) lo reemplaza.
        best_clee = None

        for n_idx in neighbors:
            is_match, strategy = calculate_match_score(
                t_names[idx],
                t_razones[idx],
                t_acts[idx],
                t_tels[idx],
                t_emails[idx],
                t_webs[idx],
                t_cps[idx],
                kb_names[n_idx],
                kb_razones[n_idx],
                kb_acts[n_idx],
                kb_tels[n_idx],
                kb_emails[n_idx],
                kb_wwws[n_idx],
                kb_cps[n_idx],
            )

            if is_match:
                if strategy in ("NAME_MATCH", "RAZON_MATCH"):
                    best_clee = kb_clees[n_idx]
                    break
                if best_clee is None:
                    best_clee = kb_clees[n_idx]

        results.append(best_clee)

    return (chunk_id, results)


def _get_num_workers() -> int:
    """
    Calcula el numero de workers para el matching paralelo.

    Prioridad: MATCH_WORKERS (env/.env) si esta definido; si no,
    cpu_count - 3 (reserva 1 core para el proceso padre/logging y 2 para
    el OS). Retornar 1 desactiva el paralelismo (se usa el loop secuencial).
    """
    if MATCH_WORKERS is not None:
        return max(1, MATCH_WORKERS)
    cpu = os.cpu_count() or 4
    return max(1, cpu - 3)


def get_region_prefix(state_list: list[str]) -> str:
    """
    Genera un prefijo corto y determinista para namespacing de IDs sinteticos.

    Se usa en los CLEEs temporales `NEW_{prefix}-{uuid12}` que
    `assign_new_clees()` asigna a establecimientos sin match. El prefijo
    identifica la region procesada (un estado -> "AGU_123"; varios ->
    "REG_4567") para poder distinguir corridas regionales en el warehouse.

    Args:
        state_list: Lista de nombres/claves de estados incluidos en la corrida.

    Returns:
        Prefijo estable para la misma lista de estados (hash truncado).

    Nota: usa hashlib (no hash() builtin) porque hash() de strings esta
    randomizado por proceso (PYTHONHASHSEED) y romperia la estabilidad
    del prefijo entre corridas.
    """

    def _stable_hash(s: str, mod: int) -> int:
        return int(hashlib.sha256(s.encode("utf-8")).hexdigest()[:8], 16) % mod

    if len(state_list) > 1:
        combined = "".join(sorted([s[:3] for s in state_list]))
        return f"REG_{_stable_hash(combined, 10000):04d}"
    else:
        state = state_list[0]
        clean_state = state.replace(" ", "").upper()
        return f"{clean_state[:3]}_{_stable_hash(state, 1000):03d}"


def build_single_period_kb(
    df_ref: pl.DataFrame,
    master_column: str = "clee_propagado_radio",
) -> pl.DataFrame:
    """
    Construye una KB escalar desde un periodo ya resuelto.

    No hace group_by ni listas: cada fila del periodo de referencia
    es una fila de la KB. Solo incluye las columnas necesarias para
    el algoritmo de matching, minimizando el uso de RAM.

    Args:
        df_ref: DataFrame de un periodo ya resuelto.
        master_column: Columna de identidad del metodo (clee_propagado_radio
            o clee_propagado_h3). Se copia a la columna neutral `clee_id`.

    Returns:
        DataFrame con columnas KB_COLUMNS, solo filas con master_column valido.
    """
    df_ref = df_ref.filter(
        (pl.col(master_column).is_not_null()) & (pl.col(master_column) != "")
    ).with_columns(pl.col(master_column).alias("clee_id"))

    available_cols = [c for c in KB_COLUMNS if c in df_ref.columns]
    kb = df_ref.select(available_cols)

    # Rellenar nulos en campos de texto para evitar crashes en comparacion.
    # Datos de encuesta: un campo puede venir vacio o incluso faltar en un
    # periodo entero -- las columnas ausentes se crean como "" para que la
    # KB siempre tenga exactamente KB_COLUMNS.
    text_cols = [
        "nom_estab",
        "raz_social",
        "nombre_act",
        "tel_clean",
        "email_clean",
        "www_clean",
        "cod_postal",
    ]
    fill_exprs = [pl.col(c).fill_null("") for c in text_cols if c in kb.columns]
    if fill_exprs:
        kb = kb.with_columns(fill_exprs)

    missing_cols = [c for c in text_cols if c not in kb.columns]
    if missing_cols:
        kb = kb.with_columns([pl.lit("").alias(c) for c in missing_cols])

    return kb.select(KB_COLUMNS)


def _run_matching_sequential(
    target_cols: tuple[list[str], ...],
    indices: np.ndarray,
) -> list[str | None]:
    """
    Matching secuencial (fallback o cuando hay pocas filas).

    Mismo loop que _match_chunk pero con logging de progreso.
    Usa las variables _shared_kb_* del modulo.
    """
    t_names, t_razones, t_acts, t_tels, t_emails, t_webs, t_cps = target_cols
    n_total = len(t_names)
    results: list[str | None] = []
    n_matched = 0

    for idx in range(n_total):
        if idx > 0 and idx % PROGRESS_LOG_INTERVAL == 0:
            logger.info(
                f"      Progreso: {idx:,}/{n_total:,} filas procesadas, "
                f"{n_matched:,} matches encontrados"
            )

        neighbors = indices[idx]
        if len(neighbors) == 0:
            results.append(None)
            continue

        best_clee = None

        for n_idx in neighbors:
            is_match, strategy = calculate_match_score(
                t_names[idx],
                t_razones[idx],
                t_acts[idx],
                t_tels[idx],
                t_emails[idx],
                t_webs[idx],
                t_cps[idx],
                _shared_kb_names[n_idx],
                _shared_kb_razones[n_idx],
                _shared_kb_acts[n_idx],
                _shared_kb_tels[n_idx],
                _shared_kb_emails[n_idx],
                _shared_kb_wwws[n_idx],
                _shared_kb_cps[n_idx],
            )

            if is_match:
                if strategy in ("NAME_MATCH", "RAZON_MATCH"):
                    best_clee = _shared_kb_clee_ids[n_idx]
                    break
                if best_clee is None:
                    best_clee = _shared_kb_clee_ids[n_idx]

        results.append(best_clee)
        if best_clee:
            n_matched += 1

    logger.info(
        f"      Completado: {n_total:,} filas procesadas, {n_matched:,} matches encontrados"
    )
    return results


def _run_matching_parallel(
    target_cols: tuple[list[str], ...],
    indices: np.ndarray,
    n_workers: int,
) -> list[str | None]:
    """
    Matching paralelo: particiona filas en chunks, un proceso por chunk.

    Usa multiprocessing.Pool con fork(). La KB se comparte via variables
    globales del modulo (COW). Solo los slices columnares del target y sus
    indices se serializan (pickle) para enviar a cada worker -- mucho mas
    liviano que picklear listas de dicts.

    Args:
        target_cols: Tuple de listas columnares (TARGET_MATCH_COLUMNS).
        indices: Array de arrays de vecinos (del generador de candidatos
            activo: BallTree o blocking H3).
        n_workers: Numero de procesos hijo.

    Returns:
        Lista de clee_id propagados (None si no hubo match), misma longitud
        que el target.
    """
    n_total = len(target_cols[0])
    chunk_size = (n_total + n_workers - 1) // n_workers  # ceil division

    chunks: list[tuple[int, tuple[list[str], ...], list[np.ndarray]]] = []
    for i in range(n_workers):
        start = i * chunk_size
        end = min(start + chunk_size, n_total)
        if start >= n_total:
            break
        cols_slice = tuple(col[start:end] for col in target_cols)
        chunks.append((i, cols_slice, indices[start:end]))

    logger.info(f"      Paralelizando: {len(chunks)} workers, ~{chunk_size:,} filas/worker")

    # fork() hereda _shared_kb_* sin copia (COW).
    # IMPORTANTE: macOS usa "spawn" por defecto (Python 3.8+), que NO hereda
    # variables globales. Forzamos "fork" explicitamente para que los workers
    # hereden _shared_kb_* sin serializar ni copiar. Seguro aqui porque no
    # usamos threads ni frameworks de UI (solo listas Python + rapidfuzz C).
    # imap_unordered retorna resultados tan pronto como cada worker termina,
    # permitiendo al padre loguear progreso. Se reordenan por chunk_id.
    #
    # RAM: el COW en CPython es imperfecto -- los refcounts y los flags del
    # GC escriben en las cabeceras de los objetos, copiando paginas al
    # tocarlas. gc.freeze() mueve todo lo existente a la generacion
    # permanente ANTES del fork, eliminando las escrituras del GC en los
    # hijos (las de refcount son inevitables, por eso KB_MAX_ROWS_PER_
    # PARTITION acota el tamanho de lo compartido).
    gc.freeze()
    fork_ctx = mp.get_context("fork")
    ordered_results: list[list[str | None] | None] = [None] * len(chunks)

    try:
        with fork_ctx.Pool(processes=len(chunks), initializer=_init_worker) as pool:
            completed = 0
            total_matched = 0

            for chunk_id, result_list in pool.imap_unordered(_match_chunk, chunks):
                ordered_results[chunk_id] = result_list
                completed += 1
                chunk_matched = sum(1 for r in result_list if r is not None)
                total_matched += chunk_matched
                logger.info(
                    f"      Chunk {completed}/{len(chunks)} completado: "
                    f"{chunk_matched:,} matches ({len(result_list):,} filas)"
                )
    finally:
        gc.unfreeze()

    # Concatenar resultados en orden original de chunks
    all_results: list[str | None] = []
    for result_list in ordered_results:
        if result_list is not None:
            all_results.extend(result_list)

    logger.info(
        f"      Completado: {n_total:,} filas procesadas, {total_matched:,} matches encontrados"
    )
    return all_results


def _propagate_single_kb(
    df_pending: pl.DataFrame,
    kb_partition: pl.DataFrame,
    spatial_tol: int,
    master_column: str,
    neighbor_finder: Callable,
    partition_label: str = "",
) -> pl.DataFrame:
    """
    Propaga la identidad desde UNA particion de KB a registros pendientes.

    Funcion interna que ejecuta el matching completo (pre-filtro espacial +
    cascada) para una unica particion de la KB de referencia. Llamada una o
    mas veces por el core segun se particione la KB.

    Args:
        df_pending: DataFrame de filas pendientes (master_column NULL/"").
        kb_partition: Particion de KB (o KB completa si no se particiono).
        spatial_tol: Radio de busqueda espacial en metros.
        master_column: Columna destino donde escribir la identidad propagada
            (clee_propagado_radio o clee_propagado_h3).
        neighbor_finder: Generador de candidatos (ref_df, target_df, radius)
            -> (indices, distances). find_spatial_neighbors (BallTree) o
            find_h3_neighbors (blocking H3).
        partition_label: Etiqueta para logging (ej. "particion 1/3").

    Returns:
        df_pending con master_column parcialmente poblado donde hubo match.
    """
    global _shared_kb_clee_ids, _shared_kb_names, _shared_kb_razones
    global _shared_kb_acts, _shared_kb_tels, _shared_kb_emails
    global _shared_kb_wwws, _shared_kb_cps

    prefix = f"      [{partition_label}] " if partition_label else "      "

    if df_pending.height == 0 or kb_partition.height == 0:
        return df_pending

    # 1. Pre-filtro espacial (BallTree o blocking H3, segun neighbor_finder)
    logger.info(
        f"{prefix}Candidatos ({neighbor_finder.__name__}): {kb_partition.height:,} refs, "
        f"{df_pending.height:,} target, radio={spatial_tol}m..."
    )
    t0 = time.perf_counter()
    indices, _ = neighbor_finder(kb_partition, df_pending, spatial_tol)
    t_bt = time.perf_counter() - t0
    logger.info(f"{prefix}Candidatos: completado en {t_bt:.1f}s")

    # 2. Materializar KB a variables globales para acceso COW en workers
    logger.info(f"{prefix}Materializando KB ({kb_partition.height:,} filas)...")
    t0 = time.perf_counter()
    _shared_kb_clee_ids = kb_partition["clee_id"].to_list()
    _shared_kb_names = kb_partition["nom_estab"].to_list()
    _shared_kb_razones = kb_partition["raz_social"].to_list()
    _shared_kb_acts = kb_partition["nombre_act"].to_list()
    _shared_kb_tels = kb_partition["tel_clean"].to_list()
    _shared_kb_emails = kb_partition["email_clean"].to_list()
    _shared_kb_wwws = kb_partition["www_clean"].to_list()
    _shared_kb_cps = kb_partition["cod_postal"].to_list()
    t_mat = time.perf_counter() - t0
    logger.info(f"{prefix}KB materializada en {t_mat:.1f}s")

    # 3. Extraer solo las columnas de matching como listas columnares.
    # Nada de to_dicts(): el DataFrame original queda intacto y los
    # resultados se aplican por indice de fila al final.
    logger.info(f"{prefix}Preparando {df_pending.height:,} filas target para matching...")
    t0 = time.perf_counter()
    # Datos de encuesta: una columna puede faltar en un periodo entero;
    # se sustituye por "" (misma semantica que campo vacio).
    target_cols = tuple(
        df_pending[c].fill_null("").to_list()
        if c in df_pending.columns
        else [""] * df_pending.height
        for c in TARGET_MATCH_COLUMNS
    )
    t_prep = time.perf_counter() - t0
    logger.info(f"{prefix}Target preparado en {t_prep:.1f}s")

    n_total = df_pending.height
    n_workers = _get_num_workers()

    # 4. Elegir modo: paralelo si hay suficientes filas y mas de 1 worker
    if n_total >= MATCH_PARALLEL_THRESHOLD and n_workers > 1:
        match_results = _run_matching_parallel(target_cols, indices, n_workers)
    else:
        if n_workers > 1:
            logger.info(
                f"{prefix}{n_total:,} filas < {MATCH_PARALLEL_THRESHOLD:,} threshold, "
                f"usando modo secuencial"
            )
        match_results = _run_matching_sequential(target_cols, indices)

    del target_cols

    # 5. Aplicar resultados por indice: todas las filas de df_pending llegan
    # con master_column NULL/"", asi que reemplazar la columna completa con
    # los resultados (None donde no hubo match) es equivalente y evita
    # reconstruir el DataFrame desde dicts.
    n_matched = sum(1 for clee in match_results if clee is not None)
    logger.info(f"{prefix}{n_matched:,} matches encontrados en esta particion")

    df_result = df_pending.with_columns(
        pl.Series(master_column, match_results, dtype=pl.Utf8)
    )

    # 7. Limpiar referencias globales (liberar KB para GC)
    _shared_kb_clee_ids = []
    _shared_kb_names = []
    _shared_kb_razones = []
    _shared_kb_acts = []
    _shared_kb_tels = []
    _shared_kb_emails = []
    _shared_kb_wwws = []
    _shared_kb_cps = []

    return df_result


def _propagate_clees(
    df_target: pl.DataFrame,
    ref_kb: pl.DataFrame,
    spatial_tol: int,
    master_column: str,
    neighbor_finder: Callable,
) -> pl.DataFrame:
    """
    CORE de propagacion (compartido por radio y H3). No llamar directo:
    usar `propagate_clees_radio` o `propagate_clees_h3`.

    Compara los registros del periodo objetivo sin resolver (master_column
    NULL/"") contra una KB de un solo periodo de referencia y propaga la
    identidad `clee_id` de la KB al target donde la cascada declara match.

    Si la KB excede KB_MAX_ROWS_PER_PARTITION, se divide en particiones
    secuenciales (control de RAM); las filas ya matcheadas no se reevaluan.
    Funcionalmente equivalente a procesar la KB completa.

    Args:
        df_target: DataFrame del periodo a resolver (incluye master_column).
        ref_kb: KB de un periodo de referencia (build_single_period_kb).
        spatial_tol: Radio de busqueda espacial en metros.
        master_column: Columna destino (clee_propagado_radio | clee_propagado_h3).
        neighbor_finder: Generador de candidatos (BallTree o H3).

    Returns:
        df_target con master_column parcialmente poblado donde hubo match.
    """

    def _is_pending(col: str) -> pl.Expr:
        return pl.col(col).is_null() | (pl.col(col) == "")

    def _is_resolved(col: str) -> pl.Expr:
        return pl.col(col).is_not_null() & (pl.col(col) != "")

    df_resolved = df_target.filter(_is_resolved(master_column))
    df_pending = df_target.filter(_is_pending(master_column))

    if df_pending.height == 0 or ref_kb.height == 0:
        return df_target

    if ref_kb.height <= KB_MAX_ROWS_PER_PARTITION:
        df_pending = _propagate_single_kb(
            df_pending, ref_kb, spatial_tol, master_column, neighbor_finder
        )
    else:
        n_partitions = (ref_kb.height + KB_MAX_ROWS_PER_PARTITION - 1) // KB_MAX_ROWS_PER_PARTITION
        logger.info(
            f"      KB grande ({ref_kb.height:,} filas) -> "
            f"particionando en {n_partitions} sub-KBs "
            f"de ~{KB_MAX_ROWS_PER_PARTITION:,} filas max"
        )

        for p_idx in range(n_partitions):
            still_pending = df_pending.filter(_is_pending(master_column))
            already_matched = df_pending.filter(_is_resolved(master_column))

            if still_pending.height == 0:
                logger.info(
                    f"      Todas las filas resueltas tras particion {p_idx}/{n_partitions}. "
                    f"Saltando particiones restantes."
                )
                break

            start = p_idx * KB_MAX_ROWS_PER_PARTITION
            end = min(start + KB_MAX_ROWS_PER_PARTITION, ref_kb.height)
            kb_partition = ref_kb.slice(start, end - start)

            label = f"particion {p_idx + 1}/{n_partitions}"
            logger.info(
                f"      [{label}] KB slice: filas {start:,}-{end:,} "
                f"({kb_partition.height:,} refs), "
                f"{still_pending.height:,} pendientes"
            )

            df_after = _propagate_single_kb(
                still_pending, kb_partition, spatial_tol, master_column, neighbor_finder, label
            )

            del kb_partition
            gc.collect()

            df_pending = already_matched.vstack(df_after)

            del df_after, still_pending, already_matched
            gc.collect()

    return df_resolved.vstack(df_pending)


def propagate_clees_radio(
    df_target: pl.DataFrame,
    ref_kb: pl.DataFrame,
    spatial_tol: int,
) -> pl.DataFrame:
    """
    Propagacion por RADIO DE TOLERANCIA (BallTree haversine).

    Escribe en la columna `clee_propagado_radio`. Metodo baseline: pre-filtro
    espacial con BallTree + cascada de matching.
    """
    return _propagate_clees(
        df_target, ref_kb, spatial_tol, "clee_propagado_radio", find_spatial_neighbors
    )


def propagate_clees_h3(
    df_target: pl.DataFrame,
    ref_kb: pl.DataFrame,
    spatial_tol: int,
) -> pl.DataFrame:
    """
    Propagacion por BLOCKING H3 (grid_disk + post-filtro haversine exacto).

    Escribe en la columna `clee_propagado_h3`. Genera candidatos por celdas
    H3 (misma cascada de matching que el metodo radio); el post-filtro exacto
    conserva la misma semantica de radio (SPATIAL_RADIUS_M).
    """
    return _propagate_clees(
        df_target, ref_kb, spatial_tol, "clee_propagado_h3", find_h3_neighbors
    )


def assign_new_clees(
    df_target: pl.DataFrame,
    region_prefix: str,
    master_column: str = "clee_propagado_radio",
) -> pl.DataFrame:
    """
    Asigna NEW_prefix-uuid a registros sin resolver tras la propagacion.

    Args:
        df_target: DataFrame con master_column parcialmente poblado.
        region_prefix: Prefijo para generar CLEEs temporales.
        master_column: Columna destino (clee_propagado_radio | clee_propagado_h3).

    Returns:
        df_target con todos los master_column poblados (sin NULLs).
    """
    df_resolved = df_target.filter(
        (pl.col(master_column).is_not_null()) & (pl.col(master_column) != "")
    )
    df_missing = df_target.filter(
        (pl.col(master_column).is_null()) | (pl.col(master_column) == "")
    )

    if df_missing.height == 0:
        return df_target

    # UUIDs generados en bulk (evita map_elements fila por fila).
    new_ids = [f"NEW_{region_prefix}-{generate_temp_clee()}" for _ in range(df_missing.height)]
    df_assigned = df_missing.with_columns(
        pl.Series(master_column, new_ids, dtype=pl.Utf8)
    )

    return df_resolved.vstack(df_assigned.select(df_resolved.columns))
