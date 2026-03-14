import uuid

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
)


def generate_temp_clee():
    """Genera un UUID seguro para nuevos negocios."""
    return uuid.uuid4().hex[:12]


def clean_contact_columns(df: pl.DataFrame) -> pl.DataFrame:
    """Normaliza telefonos, emails y webs para comparacion exacta."""
    return df.with_columns(
        [
            pl.col("telefono").str.replace_all(r"[^0-9]", "").alias("tel_clean"),
            pl.col("correoelec").str.to_lowercase().str.strip_chars().alias("email_clean"),
            pl.col("www").str.to_lowercase().str.strip_chars().alias("www_clean"),
            # Llenar nulos para evitar crash en listas
            pl.col("nom_estab").fill_null(""),
            pl.col("raz_social").fill_null(""),
            pl.col("nombre_act").fill_null(""),
        ]
    )


def find_spatial_neighbors(ref_df: pl.DataFrame, target_df: pl.DataFrame, radius_meters: float):
    """
    Ejecuta BallTree para encontrar indices vecinos.
    Retorna: (indices, distances)
    """
    ref_coords = np.radians(ref_df.select(["latitud", "longitud"]).to_numpy())
    target_coords = np.radians(target_df.select(["latitud", "longitud"]).to_numpy())

    # Radio Tierra ~ 6371000m
    radius_rad = radius_meters / 6371000

    tree = BallTree(ref_coords, metric="haversine")
    return tree.query_radius(target_coords, r=radius_rad, return_distance=True)


def calculate_match_score(
    t_row: dict,
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

    Compara un registro objetivo contra UN candidato de la KB (valores escalares,
    no listas). Cada candidato es un establecimiento de un solo periodo de
    referencia.

    GATE (obligatorio): Actividad SCIAN fuzzy > ACTIVITY_GATE_THRESH
    Path A: Razon social fuzzy > LEGAL_NAME_MATCH_THRESH  -> match directo
    Path B: Nombre comercial fuzzy > BUSINESS_NAME_MATCH_THRESH -> match directo
    Path C: 2+ senales debiles coinciden (nombre parcial, tel, email, web, CP)
            -> match combinado

    Los umbrales se importan desde clee_resolution_config.py para facilitar
    calibracion futura.

    Args:
        t_row: Diccionario con datos del registro objetivo
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
    t_act = t_row.get("nombre_act", "")
    if not kb_act or fuzz.token_set_ratio(t_act, kb_act) <= ACTIVITY_GATE_THRESH:
        return False, "DIFF_ACTIVITY"

    # === PATH A: MATCH DIRECTO POR RAZON SOCIAL ===
    t_raz = t_row.get("raz_social", "")
    if len(t_raz) > 2 and kb_razon and len(kb_razon) > 2:
        if fuzz.token_sort_ratio(t_raz, kb_razon) > LEGAL_NAME_MATCH_THRESH:
            return True, "RAZON_MATCH"

    # === PATH B: MATCH DIRECTO POR NOMBRE COMERCIAL ===
    t_name = t_row.get("nom_estab", "")
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
    t_tel = t_row.get("tel_clean", "")
    if len(t_tel) >= MIN_TEL_LENGTH and kb_tel and t_tel == kb_tel:
        signals += 1

    # Senal 3: Email exacto
    t_email = t_row.get("email_clean", "")
    if len(t_email) >= MIN_EMAIL_LENGTH and kb_email and t_email == kb_email:
        signals += 1

    # Senal 4: Web exacto
    t_web = t_row.get("www_clean", "")
    if len(t_web) >= MIN_WEB_LENGTH and kb_www and t_web == kb_www:
        signals += 1

    # Senal 5: Codigo postal igual
    t_cp = t_row.get("cod_postal", "")
    if t_cp and kb_cp and t_cp == kb_cp:
        signals += 1

    if signals >= MIN_COMBINED_SIGNALS:
        return True, "COMBINED_MATCH"

    return False, "NONE"
