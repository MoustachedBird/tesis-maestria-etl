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
    kb_names: list,
    kb_razones: list,
    kb_acts: list,
    kb_tels: list,
    kb_emails: list,
    kb_wwws: list,
    kb_cps: list,
    kb_plazas: list,
) -> tuple[bool, str]:
    """
    Calcula el match siguiendo una cascada jerarquica con umbrales calibrables:

    GATE (obligatorio): Actividad SCIAN fuzzy > ACTIVITY_GATE_THRESH
    Path A: Razon social fuzzy > LEGAL_NAME_MATCH_THRESH  -> match directo
    Path B: Nombre comercial fuzzy > BUSINESS_NAME_MATCH_THRESH -> match directo
    Path C: 2+ senales debiles coinciden (nombre parcial, tel, email, web, CP)
            -> match combinado

    Los umbrales se importan desde clee_resolution_config.py para facilitar
    calibracion futura.

    Args:
        t_row: Diccionario con datos del registro objetivo
        kb_names: Lista historica de nombres comerciales del candidato KB
        kb_razones: Lista historica de razones sociales del candidato KB
        kb_acts: Lista historica de actividades del candidato KB
        kb_tels: Lista historica de telefonos del candidato KB
        kb_emails: Lista historica de emails del candidato KB
        kb_wwws: Lista historica de webs del candidato KB
        kb_cps: Lista historica de codigos postales del candidato KB
        kb_plazas: Lista historica de centros comerciales del candidato KB

    Returns:
        Tupla (es_match: bool, estrategia: str)
        Estrategias posibles: "RAZON_MATCH", "NAME_MATCH", "COMBINED_MATCH",
                              "DIFF_ACTIVITY", "NONE"
    """

    # === GATE OBLIGATORIO: ACTIVIDAD ECONOMICA (SCIAN) ===
    # Un negocio no puede ser el mismo si su giro cambio drasticamente.
    t_act = t_row.get("nombre_act", "")
    act_match = False
    for known_act in kb_acts:
        if fuzz.token_set_ratio(t_act, known_act) > ACTIVITY_GATE_THRESH:
            act_match = True
            break

    if not act_match:
        return False, "DIFF_ACTIVITY"

    # === PATH A: MATCH DIRECTO POR RAZON SOCIAL ===
    t_raz = t_row.get("raz_social", "")
    if len(t_raz) > 2:
        for known_raz in kb_razones:
            if known_raz and fuzz.token_sort_ratio(t_raz, known_raz) > LEGAL_NAME_MATCH_THRESH:
                return True, "RAZON_MATCH"

    # === PATH B: MATCH DIRECTO POR NOMBRE COMERCIAL ===
    t_name = t_row.get("nom_estab", "")
    if len(t_name) > 2:
        for known_name in kb_names:
            if (
                known_name
                and fuzz.token_sort_ratio(t_name, known_name) > BUSINESS_NAME_MATCH_THRESH
            ):
                return True, "NAME_MATCH"

    # === PATH C: MATCH COMBINADO (senales debiles acumuladas) ===
    # Contamos cuantas senales coinciden. Si alcanzamos MIN_COMBINED_SIGNALS,
    # declaramos match. Ninguna senal individual basta por si sola.
    signals = 0

    # Senal 1: Nombre comercial parcial
    if len(t_name) > 2:
        for known_name in kb_names:
            if known_name and fuzz.token_sort_ratio(t_name, known_name) > PARTIAL_NAME_THRESH:
                signals += 1
                break

    # Senal 2: Telefono exacto
    t_tel = t_row.get("tel_clean", "")
    if len(t_tel) >= MIN_TEL_LENGTH and t_tel in kb_tels:
        signals += 1

    # Senal 3: Email exacto
    t_email = t_row.get("email_clean", "")
    if len(t_email) >= MIN_EMAIL_LENGTH and t_email in kb_emails:
        signals += 1

    # Senal 4: Web exacto
    t_web = t_row.get("www_clean", "")
    if len(t_web) >= MIN_WEB_LENGTH and t_web in kb_wwws:
        signals += 1

    # Senal 5: Codigo postal igual
    t_cp = t_row.get("cod_postal", "")
    if t_cp != "" and t_cp in kb_cps:
        signals += 1

    if signals >= MIN_COMBINED_SIGNALS:
        return True, "COMBINED_MATCH"

    return False, "NONE"
