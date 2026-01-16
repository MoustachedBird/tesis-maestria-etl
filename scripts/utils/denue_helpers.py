import os
import zipfile
import polars as pl
import uuid
from pathlib import Path
from datetime import datetime
from typing import Tuple, Dict, List

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

# ALIASES (Expandido pero seguro)
COLUMN_ALIASES = {
    # Entidad
    'nom_ent': 'entidad',
    'entidad_federativa': 'entidad',
    'ent': 'entidad',
    'desc_ent': 'entidad',
    'nombre_ent': 'entidad',
    'nombre_entidad': 'entidad',
    'cve_ent': 'entidad', 
    
    # Municipio
    'nom_mun': 'municipio',
    'municipio_del_domicilio': 'municipio',
    'mun': 'municipio',
    'desc_mun': 'municipio',
    'nombre_mun': 'municipio',
    'nombre_municipio': 'municipio',
    'municipio_delegacion': 'municipio',
    'municipio_o_delegacion': 'municipio',
    'cve_mun': 'municipio',

    # Localidad
    'nom_loc': 'localidad',
    'loc': 'localidad',
    
    # CP
    'cod_post': 'cod_postal',
    'cp': 'cod_postal',
    'codigo_postal': 'cod_postal',
    
    # Fecha
    'fecha_alta_base': 'fecha_alta'
}

# --- NUEVO: NORMALIZACIÓN DE VALORES DE ENTIDAD ---
ENTITY_VALUE_NORMALIZATION = {
    "DISTRITO FEDERAL": "CIUDAD DE MÉXICO",
    "DISTRITO_FEDERAL": "CIUDAD DE MÉXICO",
    "DF": "CIUDAD DE MÉXICO",
    "COAHUILA DE ZARAGOZA": "COAHUILA DE ZARAGOZA", 
    "MICHOACÁN DE OCAMPO": "MICHOACÁN DE OCAMPO",
    "VERACRUZ DE IGNACIO DE LA LLAVE": "VERACRUZ DE IGNACIO DE LA LLAVE",
    "MÉXICO": "ESTADO DE MÉXICO",
    "MEXICO": "ESTADO DE MÉXICO",
    "EDO. DE MÉXICO": "ESTADO DE MÉXICO",
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
        
        # 2. DETECT ENCODING
        try:
            header_line = csv_bytes.split(b'\n')[0].decode('utf-8').lower()
            encoding_used = 'utf8' 
        except UnicodeDecodeError:
            encoding_used = 'latin1' 
            warnings.append("Enc:Latin1")

        # 3. LOAD CSV
        try:
            df = pl.read_csv(csv_bytes, infer_schema_length=0, ignore_errors=True, encoding=encoding_used)
        except Exception:
            df = pl.read_csv(csv_bytes, infer_schema_length=0, ignore_errors=True, encoding='latin1')
            warnings.append("Force:Latin1")

        # 4. NORMALIZE HEADERS
        df = df.rename({c: c.lower().strip().replace('\ufeff', '') for c in df.columns})

        # 5. FIX DUPLICATE HEADERS
        if len(df.columns) != len(set(df.columns)):
            new_cols = []
            seen = {}
            for c in df.columns:
                if c in seen:
                    seen[c] += 1
                    new_cols.append(f"{c}_dupe_{seen[c]}")
                else:
                    seen[c] = 0
                    new_cols.append(c)
            df.columns = new_cols
            warnings.append("DedupedCols")

        # 6. APPLY ALIASES
        renames = {}
        for old, new in COLUMN_ALIASES.items():
            if old in df.columns and new not in df.columns:
                renames[old] = new
        
        if renames:
            df = df.rename(renames)

        # 7. VALIDATE REQUIREMENTS
        missing_req = [c for c in REQUIRED_RAW_COLUMNS if c not in df.columns]
        if missing_req:
            return "ERROR", f"Missing {missing_req}. Found: {df.columns[:5]}...", 0, 0

        # 8. SCHEMA REPORTING & FILL
        current_cols = set(df.columns)
        target_set = set(target_columns)
        
        missing_cols = list(target_set - current_cols)
        if missing_cols:
            missing_cols.sort()
            detail = f"({', '.join(missing_cols[:2])}...)" if len(missing_cols) > 2 else f"({', '.join(missing_cols)})"
            warnings.append(f"Filled: {len(missing_cols)} {detail}")

        ignored_cols = list(current_cols - target_set)
        if ignored_cols:
            warnings.append(f"Ignored: {len(ignored_cols)}")

        selection_exprs = []
        for col in target_columns:
            if col in df.columns:
                selection_exprs.append(pl.col(col))
            else:
                selection_exprs.append(pl.lit(None).alias(col))
        
        df = df.select(selection_exprs)

        # 9. CLEANING & CASTING
        df = df.unique()

        # --- APLICAMOS NORMALIZACIÓN DE VALORES AQUÍ (ANTES DE PARTICIONAR) ---
        df = df.with_columns([
            pl.col('latitud').cast(pl.Float64, strict=False),
            pl.col('longitud').cast(pl.Float64, strict=False),
            # Normalización de Entidad: UpperCase + Reemplazo DF->CDMX
            pl.col('entidad')
              .cast(pl.Utf8)
              .fill_null("DESCONOCIDO")
              .str.to_uppercase()
              .replace(ENTITY_VALUE_NORMALIZATION, default=pl.col('entidad').str.to_uppercase()),
            
            pl.col('municipio').cast(pl.Utf8).fill_null("DESCONOCIDO").str.to_uppercase(),
            pl.lit(period).alias('period')
        ])
        # ----------------------------------------------------------------------

        # 10. ROW ID GENERATION
        df = df.with_columns(
            pl.concat_str(
                [
                    pl.col('id').fill_null('NOID'), 
                    pl.col('nom_estab').fill_null('NONAME'), 
                    pl.col('latitud').fill_null(0).cast(pl.Utf8),
                    pl.col('longitud').fill_null(0).cast(pl.Utf8),
                    pl.col('period')
                ],
                separator="|"
            )
            .hash(seed=42)
            .cast(pl.Utf8)
            .alias("row_id")
        )

        total_rows = df.height
        valid_rows = 0

        # 11. ATOMIC WRITE
        partitions = df.select(['entidad', 'municipio']).unique()
        
        for row in partitions.iter_rows():
            ent, mun = row
            if not ent or not mun: continue
            
            ent_clean = str(ent).strip().replace(' ', '_').replace('/', '-').replace('.', '')
            mun_clean = str(mun).strip().replace(' ', '_').replace('/', '-').replace('.', '')
            
            # Ahora ent_clean será CIUDAD_DE_MÉXICO incluso para datos viejos del DF
            save_path = output_dir / f"state={ent_clean}" / f"municipality={mun_clean}" / f"period={period}"
            save_path.mkdir(parents=True, exist_ok=True)
            
            chunk = df.filter((pl.col('entidad') == ent) & (pl.col('municipio') == mun))
            
            if chunk.height > 0:
                final_file = save_path / "data.parquet"
                temp_file = save_path / f"temp_{uuid.uuid4().hex}.parquet"
                
                try:
                    chunk.write_parquet(temp_file, compression='snappy', use_pyarrow=True)
                    temp_file.replace(final_file)
                    valid_rows += chunk.height
                except Exception as e:
                    if temp_file.exists(): temp_file.unlink()
                    raise e

        final_msg = "; ".join(warnings) if warnings else "Clean Schema"
        return "OK", final_msg, total_rows, valid_rows

    except Exception as e:
        return "ERROR", str(e)[:150], 0, 0

def process_zip_worker(args: Tuple) -> Dict:
    os.environ["POLARS_MAX_THREADS"] = "1"
    
    zip_path, period, target_columns, output_dir = args
    start_time = datetime.now()
    
    status, msg, total, valid = extract_and_process_polars(zip_path, output_dir, period, target_columns)
    
    try:
        file_log = os.path.relpath(zip_path, os.getcwd())
    except ValueError:
        file_log = str(zip_path)

    return {
        'file': file_log,
        'period': period,
        'status': status,
        'total_rows': total,
        'valid_rows': valid,
        'invalid_cols': msg, 
        'time': (datetime.now() - start_time).total_seconds()
    }