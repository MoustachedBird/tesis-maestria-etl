import polars as pl
from scripts.utils.matching import (
    generate_temp_clee, 
    find_spatial_neighbors, 
    calculate_match_score
)

def get_region_prefix(state_list: list[str]) -> str:
    """Genera un prefijo corto único para namespacing de IDs temporales."""
    if len(state_list) > 1:
        combined = "".join(sorted([s[:3] for s in state_list]))
        return f"REG_{abs(hash(combined)) % 10000:04d}"
    else:
        state = state_list[0]
        clean_state = state.replace(" ", "").upper()
        return f"{clean_state[:3]}_{abs(hash(state)) % 1000:03d}"

def initialize_knowledge_base(df_base: pl.DataFrame, region_prefix: str) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Crea la KB inicial. Asigna IDs oficiales o temporales con prefijo."""
    df_resolved = df_base.with_columns(
        pl.when((pl.col('clee').is_not_null()) & (pl.col('clee') != ""))
        .then(pl.col('clee'))
        .otherwise(
            pl.col('row_id').map_elements(
                lambda x: f"{region_prefix}-{generate_temp_clee()}", 
                return_dtype=pl.Utf8
            )
        )
        .alias('master_clee')
    )

    kb = df_resolved.group_by('master_clee').agg([
        pl.col('latitud').first(), 
        pl.col('longitud').first(),
        # --- NUEVO: Agregamos el periodo inicial ---
        pl.col('period').alias('periods'),
        # -------------------------------------------
        pl.col('nom_estab').fill_null("").alias('names'),
        pl.col('raz_social').fill_null("").alias('razones'),
        pl.col('nombre_act').fill_null("").alias('acts'),
        pl.col('tel_clean').fill_null("").alias('tels'),
        pl.col('email_clean').fill_null("").alias('emails'),
        pl.col('www_clean').fill_null("").alias('wwws')
    ])
    
    map_accumulator = df_resolved.select(['row_id', 'master_clee'])
    return map_accumulator, kb

def resolve_period_logic(df_target: pl.DataFrame, kb: pl.DataFrame, region_prefix: str, 
                         spatial_tol: int, identity_thresh: int, activity_thresh: int) -> pl.DataFrame:
    """Núcleo algorítmico de resolución."""
    
    # A. Determinista
    df_with_clee = df_target.filter((pl.col('clee').is_not_null()) & (pl.col('clee') != ""))
    df_res_A = df_with_clee.with_columns(pl.col('clee').alias('master_clee'))
    
    # B. Probabilístico
    df_missing = df_target.filter((pl.col('clee').is_null()) | (pl.col('clee') == ""))
    
    new_links = []
    
    if df_missing.height > 0 and kb.height > 0:
        indices, _ = find_spatial_neighbors(kb, df_missing, spatial_tol)
        
        kb_ids = kb['master_clee'].to_list()
        # Convertir a dict para acceso rápido
        kb_lists = {
            'names': kb['names'].to_list(),
            'razones': kb['razones'].to_list(),
            'acts': kb['acts'].to_list(),
            'tels': kb['tels'].to_list(),
            'emails': kb['emails'].to_list(),
            'wwws': kb['wwws'].to_list()
        }
        
        target_rows = df_missing.to_dicts()
        
        for idx, row in enumerate(target_rows):
            neighbors = indices[idx]
            best_clee = None
            safe_row = {k: (v if v is not None else "") for k, v in row.items()}
            
            if len(neighbors) > 0:
                for n_idx in neighbors:
                    is_match, strategy = calculate_match_score(
                        safe_row, 
                        kb_lists['names'][n_idx], kb_lists['razones'][n_idx], kb_lists['acts'][n_idx],
                        kb_lists['tels'][n_idx], kb_lists['emails'][n_idx], kb_lists['wwws'][n_idx],
                        identity_thresh, activity_thresh
                    )
                    if is_match:
                        best_clee = kb_ids[n_idx]
                        if strategy == "DIGITAL": break
                        
            final_clee = best_clee if best_clee else f"{region_prefix}-{generate_temp_clee()}"
            new_item = row.copy()
            new_item['master_clee'] = final_clee
            new_links.append(new_item)
            
        df_res_B = pl.DataFrame(new_links).select(df_res_A.columns)
    else:
        df_res_B = df_missing.with_columns(
            pl.col('row_id').map_elements(
                lambda x: f"{region_prefix}-{generate_temp_clee()}", 
                return_dtype=pl.Utf8
            ).alias('master_clee')
        ).select(df_res_A.columns)

    return df_res_A.vstack(df_res_B)

def update_kb_logic(kb: pl.DataFrame, new_resolved_data: pl.DataFrame) -> pl.DataFrame:
    """Actualiza la KB con nuevos datos."""
    kb_updates = new_resolved_data.group_by('master_clee').agg([
        # --- NUEVO: Agregamos el periodo del nuevo hallazgo ---
        pl.col('period').alias('periods'),
        # ----------------------------------------------------
        pl.col('nom_estab').fill_null("").alias('names'),
        pl.col('raz_social').fill_null("").alias('razones'),
        pl.col('nombre_act').fill_null("").alias('acts'),
        pl.col('tel_clean').fill_null("").alias('tels'),
        pl.col('email_clean').fill_null("").alias('emails'),
        pl.col('www_clean').fill_null("").alias('wwws'),
        pl.col('latitud').first().alias('latitud'),
        pl.col('longitud').first().alias('longitud')
    ])
    
    kb = kb.join(kb_updates, on='master_clee', how='full', suffix='_new')
    
    def merge_lists(col_name):
        return pl.concat_list([
            pl.col(col_name).fill_null([]), 
            pl.col(f"{col_name}_new").fill_null([])
        ]).list.eval(pl.element().flatten().unique())

    return kb.select([
        pl.col('master_clee'),
        pl.coalesce(['latitud', 'latitud_new']).alias('latitud'),
        pl.coalesce(['longitud', 'longitud_new']).alias('longitud'),
        # --- NUEVO: Merge de periodos (ordenados) ---
        merge_lists('periods').list.sort().alias('periods'),
        # --------------------------------------------
        merge_lists('names').alias('names'),
        merge_lists('razones').alias('razones'),
        merge_lists('acts').alias('acts'),
        merge_lists('tels').alias('tels'),
        merge_lists('emails').alias('emails'),
        merge_lists('wwws').alias('wwws')
    ])

def consolidate_global_catalog(catalog_buffer: list[pl.DataFrame]) -> pl.DataFrame:
    """Fusiona catálogos regionales."""
    full_concat = pl.concat(catalog_buffer)
    
    def merge_lists_final(col_name):
        return pl.col(col_name).list.eval(pl.element().flatten().unique())

    return full_concat.group_by('master_clee').agg([
        pl.col('latitud').first(),
        pl.col('longitud').first(),
        # --- NUEVO: Merge de periodos en consolidación final (ordenados) ---
        merge_lists_final('periods').list.sort().alias('periods'),
        # ------------------------------------------------------------------
        merge_lists_final('names'),
        merge_lists_final('razones'),
        merge_lists_final('acts'),
        merge_lists_final('tels'),
        merge_lists_final('emails'),
        merge_lists_final('wwws')
    ])