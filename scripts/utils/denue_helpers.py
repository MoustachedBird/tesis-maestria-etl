import os
import zipfile
import polars as pl
from pathlib import Path
from datetime import datetime
from typing import Tuple, Dict, List, Optional

# --- CONSTANTS ---
# Minimum columns required to determine folder structure (State/Municipality)
REQUIRED_RAW_COLUMNS = ['entidad', 'municipio']

# THE MASTER SCHEMA: Only these columns will be kept.
VALID_COLUMNS = [
    'id', 'clee', 'nom_estab', 'raz_social', 'codigo_act', 'nombre_act', 'per_ocu',
    'tipo_vial', 'nom_vial', 'tipo_v_e_1', 'nom_v_e_1', 'tipo_v_e_2', 
    'nom_v_e_2', 'tipo_v_e_3', 'nom_v_e_3', 'numero_ext', 'letra_ext', 
    'edificio', 'edificio_e', 'numero_int', 'letra_int', 'tipo_asent', 
    'nomb_asent', 'tipocencom', 'nom_cencom', 'num_local', 'cod_postal', 
    'cve_ent', 'entidad', 'cve_mun', 'municipio', 'cve_loc', 'localidad', 
    'ageb', 'manzana', 'telefono', 'correoelec', 'www', 'tipounieco', 
    'latitud', 'longitud', 'fecha_alta'
]

# Aliases to map older column names (e.g., 2015 data) to the standard schema
COLUMN_ALIASES = {
    'nom_ent': 'entidad',
    'entidad_federativa': 'entidad',
    'nom_mun': 'municipio',
    'municipio_del_domicilio': 'municipio'
}

def get_valid_columns_list() -> List[str]:
    """Returns the sorted list of valid columns for the schema."""
    return sorted(VALID_COLUMNS)

def extract_and_process_polars(zip_path: Path, output_dir: Path, period: str, target_columns: List[str]) -> Tuple[str, str, int, int]:
    """
    Processes the ZIP file applying Strict Schema Enforcement.
    Returns: (Status, ChangeReport, TotalRows, ValidRows)
    """
    warnings = [] 

    try:
        # 1. READ ZIP (I/O Bound)
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            files = zip_ref.namelist()
            # Smart search for the data CSV
            csv_files = [f for f in files if f.endswith('.csv') and 'conjunto_de_datos' in f.lower()]
            if not csv_files: 
                csv_files = [f for f in files if f.endswith('.csv')]
            
            if not csv_files: 
                return "SKIPPED", "No CSV found in ZIP", 0, 0
            
            # Read bytes directly into memory
            csv_bytes = zip_ref.read(csv_files[0])
        
        # 2. LOAD & FIX HEADERS (Handling duplicates before Polars reads)
        try:
            header_line = csv_bytes.split(b'\n')[0].decode('utf-8', errors='ignore').lower()
            headers = [h.strip() for h in header_line.split(',')]
        except Exception:
            return "ERROR", "Could not decode CSV header", 0, 0

        # Fix Duplicates (e.g., 'aguascalientes' appearing twice)
        if len(headers) != len(set(headers)):
            seen = {}
            new_headers = []
            dupe_mappings = [] # To store "column -> column_dupe_1"
            
            for h in headers:
                if h in seen:
                    seen[h] += 1
                    new_name = f"{h}_dupe_{seen[h]}"
                    new_headers.append(new_name)
                    # Log the explicit renaming
                    dupe_mappings.append(f"{h}->{new_name}")
                else:
                    seen[h] = 0
                    new_headers.append(h)
            
            if dupe_mappings:
                warnings.append(f"Renamed duplicates: {dupe_mappings}")
            
            # Read CSV with the corrected header list
            df = pl.read_csv(
                csv_bytes, 
                infer_schema_length=0, 
                ignore_errors=True, 
                encoding='utf8-lossy', 
                has_header=True, 
                new_columns=new_headers
            )
        else:
            # Standard fast read
            df = pl.read_csv(
                csv_bytes, 
                infer_schema_length=0, 
                ignore_errors=True, 
                encoding='utf8-lossy'
            )

        # Normalize names to lowercase/stripped
        df = df.rename({c: c.strip().lower() for c in df.columns})

        # Apply Aliases (Fix 2015/Legacy columns)
        renames = {}
        for old, new in COLUMN_ALIASES.items():
            if old in df.columns and new not in df.columns:
                renames[old] = new
        
        if renames:
            df = df.rename(renames)
            warnings.append(f"Applied aliases: {list(renames.keys())}")

        # Validate minimal requirements (Entidad/Municipio)
        missing_req = [c for c in REQUIRED_RAW_COLUMNS if c not in df.columns]
        if missing_req: 
            return "ERROR", f"Missing critical columns: {missing_req}", 0, 0

        # ==============================================================================
        # STEP 3: STRICT SCHEMA ENFORCEMENT
        # ==============================================================================
        
        current_cols = set(df.columns)
        target_set = set(target_columns)
        
        # A. Detect extra columns (Outside schema)
        extra_cols = list(current_cols - target_set)
        if extra_cols:
            extra_cols.sort()
            # Log that we are ignoring/dropping these
            warnings.append(f"Ignored columns: {extra_cols}")

        # B. Detect missing columns (For informational purposes)
        missing_cols = list(target_set - current_cols)
        if missing_cols:
            missing_cols.sort()
            # Log that we are filling these with nulls
            warnings.append(f"Filled missing: {missing_cols}")

        # C. Build Final Selector
        # This creates the projection: Keep valid existing cols, Create nulls for missing ones.
        selection_exprs = []
        for col in target_columns:
            if col in df.columns:
                selection_exprs.append(pl.col(col))
            else:
                selection_exprs.append(pl.lit(None).alias(col))
        
        # Apply strict filtering
        df = df.select(selection_exprs)
        
        # ==============================================================================

        # 4. CLEANING (Rust Engine)
        df = df.with_columns(
            pl.col(pl.String).str.replace_all(r"[^a-zA-Z0-9\s_-]", "")
                             .str.replace_all(r"\s+", "_")
                             .str.strip_chars("_")
        )

        total_rows = df.height
        valid_rows = 0

        # 5. WRITE PARQUET (Partitioned)
        partitions = df.select(['entidad', 'municipio']).unique()
        
        for row in partitions.iter_rows():
            ent, mun = row
            # Skip if keys are missing
            if not ent or not mun: continue
            
            ent_clean = str(ent).strip().replace(' ', '_')
            mun_clean = str(mun).strip().replace(' ', '_')
            
            save_path = output_dir / f"state={ent_clean}" / f"municipality={mun_clean}" / f"period={period}"
            save_path.mkdir(parents=True, exist_ok=True)
            
            chunk = df.filter((pl.col('entidad') == ent) & (pl.col('municipio') == mun))
            
            if chunk.height > 0:
                chunk.write_parquet(
                    save_path / "data.parquet", 
                    compression='snappy',
                    use_pyarrow=True # Explicitly using PyArrow engine
                )
                valid_rows += chunk.height

        # Join warnings into a single string for the CSV report
        final_msg = "; ".join(warnings) if warnings else ""
        return "OK", final_msg, total_rows, valid_rows

    except Exception as e:
        return "ERROR", str(e)[:100], 0, 0

def process_zip_worker(args: Tuple) -> Dict:
    """
    Multiprocessing Worker Wrapper.
    """
    # CRITICAL: Limit Polars to 1 thread per worker to avoid CPU contention.
    os.environ["POLARS_MAX_THREADS"] = "1"
    
    zip_path, period, target_columns, output_dir = args
    start_time = datetime.now()
    
    status, msg, total, valid = extract_and_process_polars(zip_path, output_dir, period, target_columns)
    
    return {
        'file': zip_path.name,
        'period': period,
        'status': status,
        'total_rows': total,
        'valid_rows': valid,
        'invalid_cols': msg, # This column will contain all the detailed logs
        'time': (datetime.now() - start_time).total_seconds()
    }