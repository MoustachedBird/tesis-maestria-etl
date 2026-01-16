import polars as pl
import numpy as np
import uuid
import re
from sklearn.neighbors import BallTree
from thefuzz import fuzz

def generate_temp_clee():
    """Genera un UUID seguro para nuevos negocios."""
    return f"NEW_{uuid.uuid4().hex[:12]}"

def clean_contact_columns(df: pl.DataFrame) -> pl.DataFrame:
    """Normaliza teléfonos, emails y webs para comparación exacta."""
    return df.with_columns([
        pl.col('telefono').str.replace_all(r"[^0-9]", "").alias('tel_clean'),
        pl.col('correoelec').str.to_lowercase().str.strip_chars().alias('email_clean'),
        pl.col('www').str.to_lowercase().str.strip_chars().alias('www_clean'),
        # Llenar nulos para evitar crash en listas
        pl.col('nom_estab').fill_null(""),
        pl.col('raz_social').fill_null(""),
        pl.col('nombre_act').fill_null("")
    ])

def find_spatial_neighbors(ref_df: pl.DataFrame, target_df: pl.DataFrame, radius_meters: float):
    """
    Ejecuta BallTree para encontrar índices vecinos.
    Retorna: (indices, distances)
    """
    ref_coords = np.radians(ref_df.select(['latitud', 'longitud']).to_numpy())
    target_coords = np.radians(target_df.select(['latitud', 'longitud']).to_numpy())
    
    # Radio Tierra ~ 6371000m
    radius_rad = radius_meters / 6371000
    
    tree = BallTree(ref_coords, metric='haversine')
    return tree.query_radius(target_coords, r=radius_rad, return_distance=True)

def calculate_match_score(
    t_row: dict, 
    kb_names: list, kb_razones: list, kb_acts: list, 
    kb_tels: list, kb_emails: list, kb_wwws: list,
    identity_threshold: int, activity_threshold: int
) -> tuple[bool, str]:
    """
    Calcula si hay match entre un Target Row y un candidato de la KB.
    Retorna: (is_match, strategy_name)
    """
    # 1. Match Digital (Prioridad Absoluta)
    t_tel = t_row.get('tel_clean', "")
    t_email = t_row.get('email_clean', "")
    t_web = t_row.get('www_clean', "")

    if len(t_tel) > 6 and t_tel in kb_tels: return True, "DIGITAL"
    if len(t_email) > 5 and t_email in kb_emails: return True, "DIGITAL"
    if len(t_web) > 5 and t_web in kb_wwws: return True, "DIGITAL"

    # 2. Match Semántico (Fallback)
    t_act = t_row.get('nombre_act', "")
    t_name = t_row.get('nom_estab', "")
    t_raz = t_row.get('raz_social', "")

    # A. Filtro de Giro (Activity Check)
    # Revisamos contra los últimos 3 giros conocidos para eficiencia
    act_match = False
    for known_act in kb_acts[:3]:
        if fuzz.token_set_ratio(t_act, known_act) > activity_threshold:
            act_match = True
            break
    
    if not act_match: return False, "NONE"

    # B. Score de Identidad
    max_score = 0
    # Checar nombres comerciales
    for known_name in kb_names:
        s = fuzz.token_sort_ratio(t_name, known_name)
        if s > max_score: max_score = s
    
    # Checar razones sociales (si existen)
    if len(t_raz) > 2:
        for known_raz in kb_razones:
            s = fuzz.token_sort_ratio(t_raz, known_raz)
            if s > max_score: max_score = s
            
    if max_score > identity_threshold:
        return True, "SPATIAL_SEMANTIC"

    return False, "NONE"