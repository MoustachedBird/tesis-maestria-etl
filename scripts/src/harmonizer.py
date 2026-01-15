import polars as pl
import numpy as np
from sklearn.neighbors import BallTree
from thefuzz import fuzz
from pathlib import Path
import os
import uuid
import logging

# --- CONFIGURATION ---
# We use relative paths assuming the script runs from project root
SILVER_DIR = Path("data/processed/denue")
GOLD_DIR = Path("data/gold")
GOLD_DIR.mkdir(parents=True, exist_ok=True)

# Tuning Parameters
SPATIAL_TOLERANCE_METERS = 60  # Search radius
NAME_SIMILARITY_THRESHOLD = 80 # String similarity (0-100)

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(message)s',
        datefmt='%H:%M:%S'
    )

def generate_temp_clee():
    """Generates a UUID for stores that don't have an official CLEE."""
    return f"NEW_{uuid.uuid4().hex[:12]}"

def harmonize_timeline():
    setup_logging()
    logging.info("🔗 STARTING IDENTITY RESOLUTION (GOLD LAYER)")

    # 1. LOAD SILVER DATA (Lazy Load)
    # We scan the entire processed folder. Polars is smart enough to handle thousands of files.
    logging.info("⏳ Scanning Silver Layer (Parquet files)...")
    
    try:
        q = pl.scan_parquet(SILVER_DIR / "**/*.parquet")
    except Exception as e:
        logging.error(f"❌ Failed to read Silver Layer. Have you run 'make etl-process'? Error: {e}")
        return

    # We only bring the necessary columns into memory to keep it fast
    required_cols = ['row_id', 'clee', 'nom_estab', 'latitud', 'longitud', 'period']
    
    # Check if columns exist (lazy validation)
    try:
        schema = q.collect_schema()
        if 'row_id' not in schema:
             logging.error("❌ 'row_id' column missing. You need to re-run 'make etl-process --force' with the updated helper.")
             return
    except Exception as e:
        logging.warning(f"Could not validate schema (dataset might be empty): {e}")
        return

    logging.info("📦 Loading dataset into memory...")
    # Filter out invalid coordinates to prevent crashes in BallTree
    df_all = q.select(required_cols).filter(
        pl.col('latitud').is_not_null() & pl.col('longitud').is_not_null()
    ).collect()

    if df_all.height == 0:
        logging.warning("⚠️ No data found in processed folder.")
        return

    # 2. PREPARE TIMELINE
    # Sort periods NEWEST -> OLDEST (e.g., [2024, 2023, 2022, ...])
    # We assume the newest data is the "Ground Truth".
    periods = sorted(df_all['period'].unique().to_list(), reverse=True)
    logging.info(f"📅 Timeline found: {periods}")
    
    # Initialize Master Index with the newest year
    # Structure: [row_id, master_clee, match_type, confidence]
    df_master = df_all.filter(pl.col('period') == periods[0]).select(
        pl.col('row_id'),
        pl.col('clee').alias('master_clee'),
        pl.lit('original').alias('match_type'),
        pl.lit(1.0).alias('confidence')
    )
    
    # The 'Reference' frame starts as the newest year
    df_reference = df_all.filter(pl.col('period') == periods[0])

    # 3. BACK-PROPAGATION LOOP
    for i in range(1, len(periods)):
        target_period = periods[i]
        reference_period = periods[i-1]
        
        logging.info(f"🔄 Harmonizing: {target_period} (Target) -> {reference_period} (Reference)")
        
        df_target = df_all.filter(pl.col('period') == target_period)
        
        if df_target.height == 0 or df_reference.height == 0:
            continue

        # --- SPATIAL INDEXING ---
        # Convert to radians for Haversine metric
        ref_coords = np.radians(df_reference.select(['latitud', 'longitud']).to_numpy())
        target_coords = np.radians(df_target.select(['latitud', 'longitud']).to_numpy())
        
        # Build Tree on Reference Data (The "Future" known locations)
        tree = BallTree(ref_coords, metric='haversine')
        
        # Earth Radius ~ 6371000m. Radius in radians.
        radius_rad = SPATIAL_TOLERANCE_METERS / 6371000
        
        # Query: Find neighbors for every target store
        indices, _ = tree.query_radius(target_coords, r=radius_rad, return_distance=True)
        
        # --- IDENTITY MATCHING ---
        # Extract columns to Python lists for fast iteration
        target_names = df_target['nom_estab'].to_list()
        target_ids = df_target['row_id'].to_list()
        
        ref_names = df_reference['nom_estab'].to_list()
        ref_clees = df_reference['clee'].to_list()
        
        new_links = []
        stats_linked = 0
        stats_new = 0
        
        for idx, neighbors in enumerate(indices):
            best_clee = None
            match_type = 'generated'
            confidence = 0.0
            
            if len(neighbors) > 0:
                t_name = str(target_names[idx])
                best_score = 0
                
                # Check all spatial neighbors
                for n_idx in neighbors:
                    r_name = str(ref_names[n_idx])
                    
                    # Fuzzy match names
                    score = fuzz.token_sort_ratio(t_name, r_name)
                    
                    if score > NAME_SIMILARITY_THRESHOLD and score > best_score:
                        best_score = score
                        best_clee = ref_clees[n_idx]
                        
                if best_clee:
                    match_type = 'linked'
                    confidence = best_score / 100.0
                    stats_linked += 1
            
            # If no match found, use existing CLEE (if exists) or generate temp
            # Note: We rely on the propagate logic. If we didn't find a match, 
            # this store likely didn't exist in the future, so it gets a NEW ID.
            if not best_clee:
                # Fallback: Does the CSV itself have a CLEE? (Some old files might)
                # current_row_clee = ... (omitted for speed, usually old files don't have it)
                best_clee = generate_temp_clee()
                match_type = 'generated'
                confidence = 1.0
                stats_new += 1
                
            new_links.append({
                'row_id': target_ids[idx],
                'master_clee': best_clee,
                'match_type': match_type,
                'confidence': confidence
            })
            
        # Add to Master Catalog
        df_links = pl.DataFrame(new_links)
        df_master = df_master.vstack(df_links)
        
        # UPDATE REFERENCE: 
        # The target year now becomes the reference for the NEXT iteration (older year).
        # We need to attach the calculated CLEEs to the target dataframe 
        # so the next year (e.g., 2022) can link to these IDs.
        
        # Join links back to target to get the 'clee' column updated
        df_reference = df_target.join(df_links, on='row_id', how='left') \
                                .drop('clee') \
                                .rename({'master_clee': 'clee'})
        
        logging.info(f"   > Linked: {stats_linked} | New: {stats_new}")

    # 4. SAVE GOLD LAYER
    output_file = GOLD_DIR / "master_catalog.parquet"
    logging.info(f"💾 Saving Master Catalog to {output_file}...")
    df_master.write_parquet(output_file)
    
    # Optional: Save a CSV sample for quick inspection
    df_master.head(1000).write_csv(GOLD_DIR / "master_catalog_sample.csv")
    
    logging.info("✅ Harmonization Complete. Gold Layer Ready.")

if __name__ == "__main__":
    harmonize_timeline()