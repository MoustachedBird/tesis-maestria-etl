import os
import zipfile
import polars as pl
from pathlib import Path
from datetime import datetime
from typing import Tuple, Dict, List, Optional

# --- CONSTANTS ---
REQUIRED_RAW_COLUMNS = ['entidad', 'municipio']

# MASTER SCHEMA
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

# ALIASES FOR LEGACY DATA (2015-2016)
COLUMN_ALIASES = {
    'nom_ent': 'entidad',
    'entidad_federativa': 'entidad',
    'ent': 'entidad',
    'desc_ent': 'entidad',
    'nombre_ent': 'entidad',
    'nombre_entidad': 'entidad',
    
    'nom_mun': 'municipio',
    'municipio_del_domicilio': 'municipio',
    'mun': 'municipio',
    'desc_mun': 'municipio',
    'nombre_mun': 'municipio',
    'nombre_municipio': 'municipio'
}

def get_valid_columns_list() -> List[str]:
    return sorted(VALID_COLUMNS)

def extract_and_process_polars(zip_path: Path, output_dir: Path, period: str, target_columns: List[str]) -> Tuple[str, str, int, int]:
    warnings = [] 

    try:
        # 1. READ ZIP
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            files = zip_ref.namelist()
            csv_files = [f for f in files if f.endswith('.csv') and 'conjunto_de_datos' in f.lower()]
            if not csv_files: 
                csv_files = [f for f in files if f.endswith('.csv')]
            
            if not csv_files: 
                return "SKIPPED", "No CSV found in ZIP", 0, 0
            
            csv_bytes = zip_ref.read(csv_files[0])
        
        # 2. LOAD & FIX HEADERS (Robust Encoding)
        try:
            header_line = csv_bytes.split(b'\n')[0].decode('utf-8').lower()
            encoding_used = 'utf8-lossy'
        except UnicodeDecodeError:
            header_line = csv_bytes.split(b'\n')[0].decode('latin-1').lower()
            encoding_used = 'latin1'
            warnings.append("Used Latin-1 encoding")

        headers = [h.strip() for h in header_line.split(',')]

        # Fix Duplicates
        if len(headers) != len(set(headers)):
            seen = {}
            new_headers = []
            dupe_mappings = []
            for h in headers:
                if h in seen:
                    seen[h] += 1
                    new_name = f"{h}_dupe_{seen[h]}"
                    new_headers.append(new_name)
                    dupe_mappings.append(f"{h}->{new_name}")
                else:
                    seen[h] = 0
                    new_headers.append(h)
            
            if dupe_mappings:
                warnings.append(f"Renamed duplicates: {dupe_mappings[:3]}...") 
            
            df = pl.read_csv(csv_bytes, infer_schema_length=0, ignore_errors=True, encoding=encoding_used, has_header=True, new_columns=new_headers)
        else:
            df = pl.read_csv(csv_bytes, infer_schema_length=0, ignore_errors=True, encoding=encoding_used)

        # Normalize names
        df = df.rename({c: c.strip().lower() for c in df.columns})

        # Apply Aliases
        renames = {}
        for old, new in COLUMN_ALIASES.items():
            if old in df.columns and new not in df.columns:
                renames[old] = new
        
        if renames:
            df = df.rename(renames)
            warnings.append(f"Aliases applied: {list(renames.keys())}")

        # Validate minimal requirements
        missing_req = [c for c in REQUIRED_RAW_COLUMNS if c not in df.columns]
        if missing_req:
            found_cols = df.columns[:10] 
            return "ERROR", f"Missing {missing_req}. Found: {found_cols}", 0, 0

        # 3. STRICT SCHEMA ENFORCEMENT
        current_cols = set(df.columns)
        target_set = set(target_columns)
        
        missing_cols = list(target_set - current_cols)
        # Opcional: Loguear columnas faltantes si se desea
        # if missing_cols: warnings.append(f"Filled missing: {len(missing_cols)}")

        selection_exprs = []
        for col in target_columns:
            if col in df.columns:
                selection_exprs.append(pl.col(col))
            else:
                selection_exprs.append(pl.lit(None).alias(col))
        
        df = df.select(selection_exprs)

        # 4. CLEANING & ID GENERATION
        df = df.with_columns(
            pl.col(pl.String).str.replace_all(r"[^a-zA-Z0-9\s_-]", "")
                             .str.replace_all(r"\s+", "_")
                             .str.strip_chars("_")
        )
        
        # --- NEW: ROW ID FOR HARMONIZATION ---
        # Hash determinístico para rastrear la fila a través del tiempo
        df = df.with_columns(
            pl.concat_str(
                [pl.col("entidad"), pl.col("municipio"), pl.col("nom_estab"), pl.lit(period)],
                separator="_"
            ).hash().cast(pl.UInt64).alias("row_id")
        )

        total_rows = df.height
        valid_rows = 0

        # 5. WRITE PARQUET
        partitions = df.select(['entidad', 'municipio']).unique()
        
        for row in partitions.iter_rows():
            ent, mun = row
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
                    use_pyarrow=True
                )
                valid_rows += chunk.height

        final_msg = "; ".join(warnings) if warnings else ""
        return "OK", final_msg, total_rows, valid_rows

    except Exception as e:
        return "ERROR", str(e)[:100], 0, 0

def process_zip_worker(args: Tuple) -> Dict:
    """
    Worker que se ejecuta en paralelo.
    Recibe ruta absoluta, pero retorna ruta relativa para el Manifiesto.
    """
    os.environ["POLARS_MAX_THREADS"] = "1"
    
    zip_path, period, target_columns, output_dir = args
    start_time = datetime.now()
    
    status, msg, total, valid = extract_and_process_polars(zip_path, output_dir, period, target_columns)
    
    # Calcular ruta relativa para que coincida con el ID en manifest_download.csv
    try:
        file_log = os.path.relpath(zip_path, os.getcwd())
    except ValueError:
        file_log = str(zip_path)

    return {
        'file': file_log, # ID para el manifiesto
        'period': period,
        'status': status,
        'total_rows': total,
        'valid_rows': valid,
        'invalid_cols': msg, 
        'time': (datetime.now() - start_time).total_seconds()
    }