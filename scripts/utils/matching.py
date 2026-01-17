import polars as pl
import numpy as np
import uuid
import re
from sklearn.neighbors import BallTree
from rapidfuzz import fuzz

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
    kb_cps: list, kb_plazas: list,
    identity_threshold: int, activity_threshold: int
) -> tuple[bool, str]:
    """
    Calcula el match siguiendo una cascada jerárquica:
    1. Estabilidad Geográfica (CP / Plaza)
    2. Consistencia de Actividad (Filtro Obligatorio)
    3. Identidad Semántica (Nombre / Razón Social)
    4. Huella Digital (Tel / Email / WWW)
    """
    
    # --- PASO 1: VALIDACIÓN GEOGRÁFICA (SEGURIDAD) ---
    # Si fallan las coordenadas, el CP o la Plaza actúan como anclas
    t_cp = t_row.get('cod_postal', "")
    t_plaza = t_row.get('nom_cencom', "")
    
    geo_stable = False
    # Verificamos si el CP actual existe en el historial del candidato
    if t_cp != "" and t_cp in kb_cps:
        geo_stable = True
    # Si no, verificamos si el nombre del Centro Comercial coincide
    elif t_plaza != "" and t_plaza in kb_plazas:
        geo_stable = True
    else:
        # Si no hay CP ni Plaza (vacíos), permitimos continuar confiando 
        # en la proximidad de latitud/longitud ya filtrada previamente
        geo_stable = True 

    if not geo_stable:
        return False, "GEO_MISMATCH"

    # --- PASO 2: FILTRO DE ACTIVIDAD (REQUISITO MÍNIMO) ---
    # Un negocio no puede ser el mismo si su giro cambió drásticamente
    t_act = t_row.get('nombre_act', "")
    act_match = False
    for known_act in kb_acts:
        if fuzz.token_set_ratio(t_act, known_act) > activity_threshold:
            act_match = True
            break
    
    if not act_match:
        return False, "DIFF_ACTIVITY"

    # --- PASO 3: JERARQUÍA DE IDENTIDAD ---
    
    # A. Prioridad 1: Nombre Comercial
    t_name = t_row.get('nom_estab', "")
    for known_name in kb_names:
        if fuzz.token_sort_ratio(t_name, known_name) > identity_threshold:
            return True, "NAME_MATCH"

    # B. Prioridad 2: Razón Social (si el nombre falló o es genérico)
    t_raz = t_row.get('raz_social', "")
    if len(t_raz) > 2:
        for known_raz in kb_razones:
            if fuzz.token_sort_ratio(t_raz, known_raz) > identity_threshold:
                return True, "RAZON_MATCH"

    # C. Prioridad 3: Huella Digital (Última instancia de validación única)
    t_tel = t_row.get('tel_clean', "")
    t_email = t_row.get('email_clean', "")
    t_web = t_row.get('www_clean', "")

    if len(t_tel) > 6 and t_tel in kb_tels: 
        return True, "DIGITAL_TEL"
    if len(t_email) > 5 and t_email in kb_emails: 
        return True, "DIGITAL_EMAIL"
    if len(t_web) > 5 and t_web in kb_wwws: 
        return True, "DIGITAL_WWW"

    return False, "NONE"