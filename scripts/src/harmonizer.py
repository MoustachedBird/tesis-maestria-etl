import polars as pl
import numpy as np
from sklearn.neighbors import BallTree
from thefuzz import fuzz
from pathlib import Path
import os
import uuid
import logging
import shutil
import re # Para limpieza de teléfonos

from scripts.utils.manifest import ManifestManager

# --- CONFIGURACIÓN ---
SILVER_DIR = Path("data/processed/denue")
CURATED_ROOT = Path("data/curated")
CURATED_DENUE_DIR = CURATED_ROOT / "denue"
MASTER_CATALOG_PATH = CURATED_DENUE_DIR / "master_catalog.parquet"

# --- PARÁMETROS ---
SPATIAL_TOLERANCE_METERS = 60 # Mantenemos esto estricto para casos sin contacto
IDENTITY_SIMILARITY_THRESHOLD = 80 
ACTIVITY_SIMILARITY_THRESHOLD = 60 

def setup_logging():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s', datefmt='%H:%M:%S')

def generate_temp_clee():
    return f"NEW_{uuid.uuid4().hex[:12]}"

def clean_phone(phone_series):
    """Normaliza teléfonos: quita guiones, espacios, paréntesis."""
    # Nota: Esto es vectorizado en Polars
    return phone_series.str.replace_all(r"[^0-9]", "")

def resolve_identities_smart(df_all: pl.DataFrame) -> pl.DataFrame:
    """
    Jerarquía de Identidad:
    1. CLEE Oficial (Gold Standard).
    2. Huella Digital (Teléfono/Email/Web) -> Permite mudanzas lejanas.
    3. Proximidad Física + Nombre + Giro -> Para negocios locales sin datos digitales.
    """
    periods = sorted(df_all['period'].unique().to_list(), reverse=True)
    logging.info(f"📅 Procesando línea de tiempo: {periods}")

    # Columnas extendidas
    cols_needed = [
        'row_id', 'clee', 'nom_estab', 'raz_social', 'nombre_act', 
        'latitud', 'longitud', 'period',
        'telefono', 'correoelec', 'www' # <--- NUEVOS CAMPOS CLAVE
    ]
    
    # Pre-procesamiento global: Limpieza básica de datos de contacto
    df_clean = df_all.select(cols_needed).with_columns([
        pl.col('telefono').str.replace_all(r"[^0-9]", "").alias('tel_clean'),
        pl.col('correoelec').str.to_lowercase().str.strip_chars().alias('email_clean'),
        pl.col('www').str.to_lowercase().str.strip_chars().alias('www_clean')
    ])

    dict_dfs = {p: df_clean.filter(pl.col('period') == p) for p in periods}

    # --- AÑO BASE ---
    current_period = periods[0]
    df_current = dict_dfs[current_period]
    
    df_resolved_accumulated = df_current.with_columns(
        pl.when((pl.col('clee').is_not_null()) & (pl.col('clee') != ""))
        .then(pl.col('clee'))
        .otherwise(pl.col('row_id').map_elements(lambda x: generate_temp_clee(), return_dtype=pl.Utf8))
        .alias('master_clee')
    ) # Mantenemos todas las columnas para el matching

    # --- BACK-PROPAGATION ---
    for i in range(1, len(periods)):
        target_period = periods[i]
        ref_period = periods[i-1]
        
        logging.info(f"🔗 Vinculando: {target_period} -> {ref_period}")
        
        df_target = dict_dfs[target_period]
        
        # Referencia (Futuro ya resuelto)
        # Hacemos join para traer los datos del futuro (coords, nombres, contactos) + el master_clee asignado
        df_ref_context = df_resolved_accumulated.join(
            dict_dfs[ref_period].select(['row_id']), on='row_id', how='inner'
        )

        if df_target.height == 0 or df_ref_context.height == 0:
            # Fallback local
            df_target_local = df_target.with_columns(
                pl.when((pl.col('clee').is_not_null()) & (pl.col('clee') != ""))
                .then(pl.col('clee'))
                .otherwise(pl.col('row_id').map_elements(lambda x: generate_temp_clee(), return_dtype=pl.Utf8))
                .alias('master_clee')
            )
            df_resolved_accumulated = df_resolved_accumulated.vstack(df_target_local)
            continue

        # --- ESTRATEGIA DE 3 CAPAS ---
        
        # 1. MATCH DETERMINISTA (CLEE Oficial)
        df_target_with_clee = df_target.filter((pl.col('clee').is_not_null()) & (pl.col('clee') != ""))
        
        df_res_A = df_target_with_clee.with_columns(pl.col('clee').alias('master_clee'))
        
        # Registros restantes (Sin CLEE)
        df_target_missing = df_target.filter((pl.col('clee').is_null()) | (pl.col('clee') == ""))
        
        # 2. MATCH DIGITAL (Huella Digital)
        # Buscamos coincidencias exactas en Teléfono, Email o Web
        # Esto es costoso en Python puro, pero rápido con Joins en Polars
        
        # Filtramos referencia para tener solo datos válidos de contacto
        ref_digital = df_ref_context.filter(
            (pl.col('tel_clean').str.len_chars() > 6) | 
            (pl.col('email_clean').str.len_chars() > 5) |
            (pl.col('www_clean').str.len_chars() > 5)
        )
        
        # Hacemos un Join "sucio" (Outer o concatenación de condiciones) para encontrar matches digitales
        # Para simplificar y optimizar, lo haremos iterativo solo para los que quedan
        
        # Preparamos listas para el BallTree (Fase 3), pero iremos restando los que encontremos en Fase 2
        # NOTA: Para no complicar excesivamente con Joins múltiples, usaremos la iteración Python 
        # para aplicar Fase 2 y Fase 3 secuencialmente sobre los mismos índices.
        
        # --- PREPARACIÓN VECTORES ---
        ref_coords = np.radians(df_ref_context.select(['latitud', 'longitud']).to_numpy())
        target_coords = np.radians(df_target_missing.select(['latitud', 'longitud']).to_numpy())
        
        # Datos Target
        t_ids = df_target_missing['row_id'].to_list()
        t_names = df_target_missing['nom_estab'].fill_null("").to_list()
        t_razones = df_target_missing['raz_social'].fill_null("").to_list()
        t_acts = df_target_missing['nombre_act'].fill_null("").to_list()
        t_tels = df_target_missing['tel_clean'].fill_null("").to_list()
        t_emails = df_target_missing['email_clean'].fill_null("").to_list()
        t_wwws = df_target_missing['www_clean'].fill_null("").to_list()
        
        # Datos Referencia
        r_names = df_ref_context['nom_estab'].fill_null("").to_list()
        r_razones = df_ref_context['raz_social'].fill_null("").to_list()
        r_acts = df_ref_context['nombre_act'].fill_null("").to_list()
        r_tels = df_ref_context['tel_clean'].fill_null("").to_list()
        r_emails = df_ref_context['email_clean'].fill_null("").to_list()
        r_wwws = df_ref_context['www_clean'].fill_null("").to_list()
        r_master_clees = df_ref_context['master_clee'].to_list()
        
        # BallTree para Fase 3 (Proximidad)
        tree = BallTree(ref_coords, metric='haversine')
        radius_rad = SPATIAL_TOLERANCE_METERS / 6371000
        indices, _ = tree.query_radius(target_coords, r=radius_rad, return_distance=True)
        
        new_links = []
        
        # Métricas de depuración
        count_digital = 0
        count_spatial = 0
        
        for idx in range(len(t_ids)):
            best_clee = None
            match_found_strategy = None
            
            # --- ESTRATEGIA 2: HUELLA DIGITAL (Prioridad Alta) ---
            # Verificamos si este target tiene match digital con ALGUIEN de la referencia (no solo vecinos cercanos)
            # NOTA: Hacer búsqueda global es O(N^2). Para eficiencia, limitamos la búsqueda digital 
            # a un radio geográfico más amplio (ej. 20km) o asumimos que el BallTree nos da candidatos cercanos.
            # *Mejora:* Si tienes millones de datos, indexa teléfonos en un Hash Map.
            # Aquí, para simplicidad, verificamos primero dentro de los vecinos cercanos, 
            # y si el usuario quiere, podríamos extenderlo.
            
            # Verificación rápida en vecinos (Combinamos proximidad + digital para altísima certeza)
            neighbors = indices[idx]
            
            if len(neighbors) > 0:
                t_phone = t_tels[idx]
                t_mail = t_emails[idx]
                t_web = t_wwws[idx]
                t_act = t_acts[idx]
                t_name = str(t_names[idx])
                t_raz = str(t_razones[idx])
                
                best_score = 0
                
                for n_idx in neighbors:
                    # 1. Match Digital (The "Silver Bullet")
                    # Si coincide teléfono (y es válido > 6 dígitos)
                    is_digital_match = False
                    if len(t_phone) > 6 and t_phone == r_tels[n_idx]: is_digital_match = True
                    if len(t_mail) > 5 and t_mail == r_emails[n_idx]: is_digital_match = True
                    if len(t_web) > 5 and t_web == r_wwws[n_idx]: is_digital_match = True
                    
                    if is_digital_match:
                        best_clee = r_master_clees[n_idx]
                        match_found_strategy = "DIGITAL"
                        break # Encontramos al padre definitivo, dejamos de buscar
                    
                    # 2. Match Espacial + Nombre (Fallback)
                    # Filtro de Giro
                    act_score = fuzz.token_set_ratio(t_act, r_acts[n_idx])
                    if act_score < ACTIVITY_SIMILARITY_THRESHOLD: continue
                    
                    # Filtro de Nombre
                    score_name = fuzz.token_sort_ratio(t_name, str(r_names[n_idx]))
                    score_raz = 0
                    if len(t_raz) > 2:
                        score_raz = fuzz.token_sort_ratio(t_raz, str(r_razones[n_idx]))
                    
                    final_score = max(score_name, score_raz)
                    
                    if final_score > IDENTITY_SIMILARITY_THRESHOLD and final_score > best_score:
                        best_score = final_score
                        best_clee = r_master_clees[n_idx]
                        match_found_strategy = "SPATIAL"

            # Resultados
            if best_clee:
                final_clee = best_clee
                if match_found_strategy == "DIGITAL": count_digital += 1
                else: count_spatial += 1
            else:
                final_clee = generate_temp_clee()
            
            # Agregamos al dataframe de resultados (incluyendo metadatos para la siguiente iteración)
            # Usamos el diccionario original para llenar los datos
            new_links.append(df_target_missing[idx].select(df_resolved_accumulated.columns).with_columns(
                pl.lit(final_clee).alias('master_clee')
            ))

        # Reconstruir DataFrame B
        if new_links:
            # Concatenar lista de DataFrames de una fila es lento, mejor construir dicts
            # Pero como ya tenemos rows en polars, usamos vstack sobre un esquema vacío o concat
            df_res_B = pl.concat(new_links)
        else:
            df_res_B = df_res_A.clear() # Vacío con mismo esquema

        # Unir A y B
        df_period_resolved = df_res_A.vstack(df_res_B)
        
        # Agregar al acumulado
        df_resolved_accumulated = df_resolved_accumulated.vstack(df_period_resolved)
        
        logging.info(f"   📊 Stats: Oficiales={df_res_A.height} | Digital={count_digital} | Espacial={count_spatial} | Nuevos={len(new_links)-count_digital-count_spatial}")

    return df_resolved_accumulated.select(['row_id', 'master_clee'])

def propagate_to_curated(id_map: pl.DataFrame):
    logging.info(f"🚀 Materializando capa Curated en: {CURATED_DENUE_DIR}")
    if CURATED_DENUE_DIR.exists(): shutil.rmtree(CURATED_DENUE_DIR)
    CURATED_DENUE_DIR.mkdir(parents=True, exist_ok=True)
    
    files = list(SILVER_DIR.glob("**/*.parquet"))
    lz_map = id_map.lazy()
    
    for i, file_path in enumerate(files, 1):
        try:
            rel_path = file_path.relative_to(SILVER_DIR)
            dest_path = CURATED_DENUE_DIR / rel_path
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Join y reemplazo de CLEE
            df_curated = pl.read_parquet(file_path).drop('clee').lazy().join(
                lz_map, on='row_id', how='left'
            ).rename({'master_clee': 'clee'}).collect()
            
            cols = ['clee'] + [c for c in df_curated.columns if c != 'clee']
            df_curated.select(cols).write_parquet(dest_path, compression='snappy')
            
            if i % 100 == 0: print(f"   ⏳ {i}/{len(files)}...", end='\r')
        except Exception as e:
            logging.error(f"❌ Error: {e}")

def harmonize():
    setup_logging()
    CURATED_ROOT.mkdir(parents=True, exist_ok=True)
    manifest_mgr = ManifestManager(Path("data"))

    logging.info("⏳ Cargando Silver Layer...")
    try:
        q = pl.scan_parquet(SILVER_DIR / "**/*.parquet", hive_partitioning=True)
        if 'row_id' not in q.collect_schema(): return
    except Exception: return

    # Columnas extendidas requeridas
    cols = [
        'row_id', 'clee', 'nom_estab', 'raz_social', 'nombre_act', 
        'latitud', 'longitud', 'period', 'telefono', 'correoelec', 'www'
    ]
    
    try:
        df_input = q.select(cols).filter(
            pl.col('latitud').is_not_null() & pl.col('longitud').is_not_null()
        ).collect()
    except Exception as e:
        logging.error(f"Faltan columnas requeridas (telefono, correoelec, etc): {e}")
        return

    if df_input.height == 0: return

    df_identity_map = resolve_identities_smart(df_input)
    propagate_to_curated(df_identity_map)
    
    logging.info(f"💾 Guardando Catálogo...")
    df_identity_map.write_parquet(MASTER_CATALOG_PATH)

    tasks = manifest_mgr.get_harmonization_tasks(retry_errors=True)
    if tasks:
        manifest_mgr.update_harmonization_status([{"partition_path": t['partition_path'], "status": "HARMONIZED"} for t in tasks])
    
    logging.info("🏆 PROCESO TERMINADO")

if __name__ == "__main__":
    harmonize()