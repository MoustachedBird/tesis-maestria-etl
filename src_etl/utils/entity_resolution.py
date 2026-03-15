"""
Entity Resolution: Funciones de propagacion de CLEEs entre periodos.

Este modulo contiene la logica aislada de propagacion de identidades.
La funcion principal `propagate_clees()` es el punto de intercambio
para futuros algoritmos de matching -- mientras respete la interfaz
(recibe target + ref_kb, retorna target con clee_master parcialmente
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
import logging
import multiprocessing as mp
import os
import time

import numpy as np
import polars as pl

from src_etl.utils.clee_resolution_config import (
    KB_MAX_ROWS_PER_PARTITION,
    MATCH_PARALLEL_THRESHOLD,
    MATCH_WORKERS,
)
from src_etl.utils.matching import (
    calculate_match_score,
    find_spatial_neighbors,
    generate_temp_clee,
)

logger = logging.getLogger(__name__)

# Columnas minimas que la KB necesita para el algoritmo de matching.
# Cualquier otra columna es innecesaria y consume RAM sin aportar.
KB_COLUMNS = [
    "clee_master",
    "latitud",
    "longitud",
    "nom_estab",
    "raz_social",
    "nombre_act",
    "tel_clean",
    "email_clean",
    "www_clean",
    "cod_postal",
]

PROGRESS_LOG_INTERVAL = 100_000

# ---------------------------------------------------------------------------
# Variables globales para compartir KB entre workers via fork() + COW.
# Se asignan en el proceso padre justo antes de crear el Pool.
# Los hijos las heredan sin copia (copy-on-write del OS).
# NUNCA escribir en estas desde un worker -- rompe COW.
# ---------------------------------------------------------------------------
_shared_kb_clee_masters: list[str] = []
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
    chunk_data: tuple[int, list[dict], list[np.ndarray]],
) -> tuple[int, list[str | None]]:
    """
    Worker function: procesa un chunk de filas target contra la KB completa.

    Se ejecuta en un proceso hijo. Accede a la KB via variables globales
    del modulo (heredadas por fork, COW, sin copia real en RAM).

    Args:
        chunk_data: Tupla (chunk_id, target_rows_slice, indices_slice)

    Returns:
        Tupla (chunk_id, resultados). chunk_id se usa para reordenar
        los resultados cuando se usa imap_unordered.
    """
    chunk_id, target_rows, indices_slice = chunk_data

    # Referencia local a las globales (micro-optimizacion: evita LOAD_GLOBAL repetido)
    kb_clees = _shared_kb_clee_masters
    kb_names = _shared_kb_names
    kb_razones = _shared_kb_razones
    kb_acts = _shared_kb_acts
    kb_tels = _shared_kb_tels
    kb_emails = _shared_kb_emails
    kb_wwws = _shared_kb_wwws
    kb_cps = _shared_kb_cps

    results: list[str | None] = []

    for idx, row in enumerate(target_rows):
        neighbors = indices_slice[idx]
        if len(neighbors) == 0:
            results.append(None)
            continue

        # Sanitizar nulos
        safe_row = {k: (v if v is not None else "") for k, v in row.items()}
        best_clee = None

        for n_idx in neighbors:
            is_match, strategy = calculate_match_score(
                safe_row,
                kb_names[n_idx],
                kb_razones[n_idx],
                kb_acts[n_idx],
                kb_tels[n_idx],
                kb_emails[n_idx],
                kb_wwws[n_idx],
                kb_cps[n_idx],
            )

            if is_match:
                best_clee = kb_clees[n_idx]
                if strategy in ("NAME_MATCH", "RAZON_MATCH"):
                    break

        results.append(best_clee)

    return (chunk_id, results)


def _get_num_workers() -> int:
    """Calcula el numero de workers a usar (reserva 1 core para padre/log + 2 OS)."""
    if MATCH_WORKERS is not None:
        return max(1, MATCH_WORKERS)
    cpu = os.cpu_count() or 4
    return max(1, cpu - 3)


def get_region_prefix(state_list: list[str]) -> str:
    """Genera un prefijo corto unico para namespacing de IDs temporales."""
    if len(state_list) > 1:
        combined = "".join(sorted([s[:3] for s in state_list]))
        return f"REG_{abs(hash(combined)) % 10000:04d}"
    else:
        state = state_list[0]
        clean_state = state.replace(" ", "").upper()
        return f"{clean_state[:3]}_{abs(hash(state)) % 1000:03d}"


def build_single_period_kb(df_ref: pl.DataFrame) -> pl.DataFrame:
    """
    Construye una KB escalar desde un periodo ya resuelto.

    No hace group_by ni listas: cada fila del periodo de referencia
    es una fila de la KB. Solo incluye las columnas necesarias para
    el algoritmo de matching, minimizando el uso de RAM.

    Args:
        df_ref: DataFrame de un periodo ya resuelto (con clee_master poblado).

    Returns:
        DataFrame con columnas KB_COLUMNS, solo filas con clee_master valido.
    """
    available_cols = [c for c in KB_COLUMNS if c in df_ref.columns]
    kb = df_ref.filter(
        (pl.col("clee_master").is_not_null()) & (pl.col("clee_master") != "")
    ).select(available_cols)

    # Rellenar nulos en campos de texto para evitar crashes en comparacion
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

    return kb


def _run_matching_sequential(
    target_rows: list[dict],
    indices: np.ndarray,
) -> list[str | None]:
    """
    Matching secuencial (fallback o cuando hay pocas filas).

    Identico al loop original. Usa las variables _shared_kb_* del modulo.
    """
    n_total = len(target_rows)
    results: list[str | None] = []
    n_matched = 0

    for idx, row in enumerate(target_rows):
        if idx > 0 and idx % PROGRESS_LOG_INTERVAL == 0:
            logger.info(
                f"      Progreso: {idx:,}/{n_total:,} filas procesadas, "
                f"{n_matched:,} matches encontrados"
            )

        neighbors = indices[idx]
        if len(neighbors) == 0:
            results.append(None)
            continue

        safe_row = {k: (v if v is not None else "") for k, v in row.items()}
        best_clee = None

        for n_idx in neighbors:
            is_match, strategy = calculate_match_score(
                safe_row,
                _shared_kb_names[n_idx],
                _shared_kb_razones[n_idx],
                _shared_kb_acts[n_idx],
                _shared_kb_tels[n_idx],
                _shared_kb_emails[n_idx],
                _shared_kb_wwws[n_idx],
                _shared_kb_cps[n_idx],
            )

            if is_match:
                best_clee = _shared_kb_clee_masters[n_idx]
                if strategy in ("NAME_MATCH", "RAZON_MATCH"):
                    break

        results.append(best_clee)
        if best_clee:
            n_matched += 1

    logger.info(
        f"      Completado: {n_total:,} filas procesadas, {n_matched:,} matches encontrados"
    )
    return results


def _run_matching_parallel(
    target_rows: list[dict],
    indices: np.ndarray,
    n_workers: int,
) -> list[str | None]:
    """
    Matching paralelo: particiona filas en chunks, un proceso por chunk.

    Usa multiprocessing.Pool con fork(). La KB se comparte via variables
    globales del modulo (COW). Solo los chunks de target_rows y sus
    indices se serializan (pickle) para enviar a cada worker.

    Args:
        target_rows: Lista de dicts (filas pendientes).
        indices: Array de arrays de vecinos (del BallTree).
        n_workers: Numero de procesos hijo.

    Returns:
        Lista de clee_master (None si no hubo match), misma longitud que target_rows.
    """
    n_total = len(target_rows)
    chunk_size = (n_total + n_workers - 1) // n_workers  # ceil division

    chunks: list[tuple[int, list[dict], list[np.ndarray]]] = []
    for i in range(n_workers):
        start = i * chunk_size
        end = min(start + chunk_size, n_total)
        if start >= n_total:
            break
        chunks.append((i, target_rows[start:end], indices[start:end]))

    logger.info(f"      Paralelizando: {len(chunks)} workers, ~{chunk_size:,} filas/worker")

    # fork() hereda _shared_kb_* sin copia (COW).
    # IMPORTANTE: macOS usa "spawn" por defecto (Python 3.8+), que NO hereda
    # variables globales. Forzamos "fork" explicitamente para que los workers
    # hereden _shared_kb_* sin serializar ni copiar. Seguro aqui porque no
    # usamos threads ni frameworks de UI (solo listas Python + rapidfuzz C).
    # imap_unordered retorna resultados tan pronto como cada worker termina,
    # permitiendo al padre loguear progreso. Se reordenan por chunk_id.
    fork_ctx = mp.get_context("fork")
    ordered_results: list[list[str | None] | None] = [None] * len(chunks)

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
    partition_label: str = "",
) -> pl.DataFrame:
    """
    Propaga clee_master desde UNA particion de KB a registros pendientes.

    Funcion interna que ejecuta el matching completo (BallTree + cascada)
    para una unica particion de la KB de referencia. Llamada una o mas
    veces por propagate_clees() dependiendo de si la KB se particiono.

    Args:
        df_pending: DataFrame de filas sin clee_master (pendientes).
        kb_partition: Particion de KB (o KB completa si no se particiono).
        spatial_tol: Radio de busqueda espacial en metros.
        partition_label: Etiqueta para logging (ej. "particion 1/3").

    Returns:
        df_pending con clee_master parcialmente poblado donde hubo match.
    """
    global _shared_kb_clee_masters, _shared_kb_names, _shared_kb_razones
    global _shared_kb_acts, _shared_kb_tels, _shared_kb_emails
    global _shared_kb_wwws, _shared_kb_cps

    prefix = f"      [{partition_label}] " if partition_label else "      "

    if df_pending.height == 0 or kb_partition.height == 0:
        return df_pending

    # 1. Pre-filtro espacial: BallTree sobre la particion de KB
    logger.info(
        f"{prefix}BallTree: construyendo indice ({kb_partition.height:,} refs, "
        f"{df_pending.height:,} target, radio={spatial_tol}m)..."
    )
    t0 = time.perf_counter()
    indices, _ = find_spatial_neighbors(kb_partition, df_pending, spatial_tol)
    t_bt = time.perf_counter() - t0
    logger.info(f"{prefix}BallTree: completado en {t_bt:.1f}s")

    # 2. Materializar KB a variables globales para acceso COW en workers
    logger.info(f"{prefix}Materializando KB ({kb_partition.height:,} filas x 8 columnas)...")
    t0 = time.perf_counter()
    _shared_kb_clee_masters = kb_partition["clee_master"].to_list()
    _shared_kb_names = kb_partition["nom_estab"].to_list()
    _shared_kb_razones = kb_partition["raz_social"].to_list()
    _shared_kb_acts = kb_partition["nombre_act"].to_list()
    _shared_kb_tels = kb_partition["tel_clean"].to_list()
    _shared_kb_emails = kb_partition["email_clean"].to_list()
    _shared_kb_wwws = kb_partition["www_clean"].to_list()
    _shared_kb_cps = kb_partition["cod_postal"].to_list()
    t_mat = time.perf_counter() - t0
    logger.info(f"{prefix}KB materializada en {t_mat:.1f}s")

    # 3. Convertir target a lista de dicts para el loop
    logger.info(f"{prefix}Preparando {df_pending.height:,} filas target para matching...")
    t0 = time.perf_counter()
    target_rows = df_pending.to_dicts()
    t_dicts = time.perf_counter() - t0
    logger.info(f"{prefix}Target preparado en {t_dicts:.1f}s")

    n_total = len(target_rows)
    n_workers = _get_num_workers()

    # 4. Elegir modo: paralelo si hay suficientes filas y mas de 1 worker
    if n_total >= MATCH_PARALLEL_THRESHOLD and n_workers > 1:
        match_results = _run_matching_parallel(target_rows, indices, n_workers)
    else:
        if n_workers > 1:
            logger.info(
                f"{prefix}{n_total:,} filas < {MATCH_PARALLEL_THRESHOLD:,} threshold, "
                f"usando modo secuencial"
            )
        match_results = _run_matching_sequential(target_rows, indices)

    # 5. Aplicar resultados a los dicts del target
    n_matched = 0
    for idx, clee in enumerate(match_results):
        if clee is not None:
            target_rows[idx]["clee_master"] = clee
            n_matched += 1

    logger.info(f"{prefix}{n_matched:,} matches encontrados en esta particion")

    # 6. Reconstruir DataFrame con resultados
    df_result = pl.DataFrame(target_rows).select(df_pending.columns)

    # 7. Limpiar referencias globales (liberar KB para GC)
    _shared_kb_clee_masters = []
    _shared_kb_names = []
    _shared_kb_razones = []
    _shared_kb_acts = []
    _shared_kb_tels = []
    _shared_kb_emails = []
    _shared_kb_wwws = []
    _shared_kb_cps = []

    return df_result


def propagate_clees(
    df_target: pl.DataFrame,
    ref_kb: pl.DataFrame,
    spatial_tol: int,
) -> pl.DataFrame:
    """
    Propaga clee_master desde una KB de referencia a registros sin resolver.

    Funcion aislada de propagacion: compara registros del periodo objetivo
    que aun no tienen clee_master contra una KB de un solo periodo de
    referencia (valores escalares, no listas acumuladas).

    Si la KB excede KB_MAX_ROWS_PER_PARTITION filas, se divide en
    particiones que se procesan secuencialmente. Cada particion construye
    su propio BallTree y materializa su propia copia de _shared_kb_*.
    Las filas que matchean en una particion ya no se evaluan en la
    siguiente, reduciendo el trabajo progresivamente. No se pierde
    ningun match potencial -- es funcionalmente equivalente a procesar
    la KB completa, solo cambia el pico de RAM.

    Algoritmo (por particion):
    1. Separa registros ya resueltos de pendientes (clee_master IS NULL)
    2. Construye BallTree sobre la particion de KB
    3. Para cada pendiente, evalua vecinos espaciales con calculate_match_score()
    4. Si hay match -> asigna clee_master del vecino
    5. Si no hay match -> deja clee_master como NULL (siguiente particion o ronda)

    Paralelismo: Si hay mas de MATCH_PARALLEL_THRESHOLD filas pendientes,
    se usa multiprocessing para distribuir el loop de matching entre
    N workers. La KB se comparte via fork() + COW (sin copia de RAM).
    Si hay menos filas, se usa el loop secuencial (el overhead de fork
    no vale la pena).

    NOTA: Esta funcion es el punto de intercambio para futuros algoritmos
    de propagacion. Mientras respete la interfaz (recibe target + ref_kb,
    retorna target con clee_master parcialmente poblado), el orquestador
    no necesita cambios.

    Args:
        df_target: DataFrame del periodo a resolver. Debe incluir columna
                   clee_master (puede ser NULL para registros pendientes).
        ref_kb: KB escalar de un solo periodo de referencia.
        spatial_tol: Radio de busqueda espacial en metros.

    Returns:
        df_target con clee_master parcialmente poblado donde hubo match.
    """
    # Separar resueltos de pendientes
    df_resolved = df_target.filter(
        (pl.col("clee_master").is_not_null()) & (pl.col("clee_master") != "")
    )
    df_pending = df_target.filter((pl.col("clee_master").is_null()) | (pl.col("clee_master") == ""))

    if df_pending.height == 0 or ref_kb.height == 0:
        return df_target

    # Decidir si particionar la KB
    if ref_kb.height <= KB_MAX_ROWS_PER_PARTITION:
        # KB cabe en una sola particion -- logica directa (sin overhead)
        df_pending = _propagate_single_kb(df_pending, ref_kb, spatial_tol)
    else:
        # KB excede el umbral -- dividir en particiones secuenciales
        n_partitions = (ref_kb.height + KB_MAX_ROWS_PER_PARTITION - 1) // KB_MAX_ROWS_PER_PARTITION
        logger.info(
            f"      KB grande ({ref_kb.height:,} filas) -> "
            f"particionando en {n_partitions} sub-KBs "
            f"de ~{KB_MAX_ROWS_PER_PARTITION:,} filas max"
        )

        for p_idx in range(n_partitions):
            # Recalcular pendientes: filas ya matcheadas no se re-evaluan
            still_pending = df_pending.filter(
                (pl.col("clee_master").is_null()) | (pl.col("clee_master") == "")
            )
            already_matched = df_pending.filter(
                (pl.col("clee_master").is_not_null()) & (pl.col("clee_master") != "")
            )

            if still_pending.height == 0:
                logger.info(
                    f"      Todas las filas resueltas tras particion {p_idx}/{n_partitions}. "
                    f"Saltando particiones restantes."
                )
                break

            # Extraer particion de la KB
            start = p_idx * KB_MAX_ROWS_PER_PARTITION
            end = min(start + KB_MAX_ROWS_PER_PARTITION, ref_kb.height)
            kb_partition = ref_kb.slice(start, end - start)

            label = f"particion {p_idx + 1}/{n_partitions}"
            logger.info(
                f"      [{label}] KB slice: filas {start:,}-{end:,} "
                f"({kb_partition.height:,} refs), "
                f"{still_pending.height:,} pendientes"
            )

            # Propagar contra esta particion
            df_after = _propagate_single_kb(still_pending, kb_partition, spatial_tol, label)

            # Liberar particion de KB
            del kb_partition
            gc.collect()

            # Reunir: ya matcheados anteriormente + resultado de esta particion
            df_pending = already_matched.vstack(df_after)

            del df_after, still_pending, already_matched
            gc.collect()

    return df_resolved.vstack(df_pending)


def assign_new_clees(
    df_target: pl.DataFrame,
    region_prefix: str,
) -> pl.DataFrame:
    """
    Asigna NEW_prefix-uuid a registros que quedaron sin clee_master
    despues de todas las rondas de propagacion.

    Args:
        df_target: DataFrame con clee_master parcialmente poblado.
        region_prefix: Prefijo para generar CLEEs temporales.

    Returns:
        df_target con todos los clee_master poblados (sin NULLs).
    """
    df_resolved = df_target.filter(
        (pl.col("clee_master").is_not_null()) & (pl.col("clee_master") != "")
    )
    df_missing = df_target.filter((pl.col("clee_master").is_null()) | (pl.col("clee_master") == ""))

    if df_missing.height == 0:
        return df_target

    df_assigned = df_missing.with_columns(
        pl.col("id")
        .map_elements(
            lambda x: f"NEW_{region_prefix}-{generate_temp_clee()}",
            return_dtype=pl.Utf8,
        )
        .alias("clee_master")
    )

    return df_resolved.vstack(df_assigned.select(df_resolved.columns))
