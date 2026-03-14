"""
Entity Resolution: Funciones de propagacion de CLEEs entre periodos.

Este modulo contiene la logica aislada de propagacion de identidades.
La funcion principal `propagate_clees()` es el punto de intercambio
para futuros algoritmos de matching -- mientras respete la interfaz
(recibe target + ref_kb, retorna target con clee_master parcialmente
poblado), el orquestador no necesita cambios.
"""

import logging

import polars as pl

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

    Algoritmo:
    1. Separa registros ya resueltos de pendientes (clee_master IS NULL)
    2. Construye BallTree sobre ref_kb (max ~6M puntos)
    3. Para cada pendiente, evalua vecinos espaciales con calculate_match_score()
    4. Si hay match -> asigna clee_master del vecino
    5. Si no hay match -> deja clee_master como NULL (siguiente ronda lo intenta)

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

    # 1. Pre-filtro espacial: BallTree sobre la KB de referencia
    indices, _ = find_spatial_neighbors(ref_kb, df_pending, spatial_tol)

    # 2. Materializar KB a arrays Python para acceso rapido en el loop
    kb_clee_masters = ref_kb["clee_master"].to_list()
    kb_names = ref_kb["nom_estab"].to_list()
    kb_razones = ref_kb["raz_social"].to_list()
    kb_acts = ref_kb["nombre_act"].to_list()
    kb_tels = ref_kb["tel_clean"].to_list()
    kb_emails = ref_kb["email_clean"].to_list()
    kb_wwws = ref_kb["www_clean"].to_list()
    kb_cps = ref_kb["cod_postal"].to_list()

    # 3. Iterar sobre registros pendientes
    target_rows = df_pending.to_dicts()
    n_total = len(target_rows)
    n_matched = 0

    for idx, row in enumerate(target_rows):
        if idx > 0 and idx % PROGRESS_LOG_INTERVAL == 0:
            logger.info(
                f"      Progreso: {idx:,}/{n_total:,} filas procesadas, "
                f"{n_matched:,} matches encontrados"
            )

        neighbors = indices[idx]
        if len(neighbors) == 0:
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
                best_clee = kb_clee_masters[n_idx]
                if strategy in ("NAME_MATCH", "RAZON_MATCH"):
                    break

        if best_clee:
            row["clee_master"] = best_clee
            n_matched += 1

    logger.info(
        f"      Completado: {n_total:,} filas procesadas, {n_matched:,} matches encontrados"
    )

    # 4. Reconstruir DataFrame con resultados
    df_newly_resolved = pl.DataFrame(target_rows).select(df_target.columns)

    return df_resolved.vstack(df_newly_resolved)


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
