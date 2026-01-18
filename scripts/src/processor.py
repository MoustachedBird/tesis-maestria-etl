import zipfile
import json
import logging
import sys
import traceback
from pathlib import Path
from datetime import datetime
import polars as pl

# Importación de managers especializados
from scripts.src.denue_activities import ActivityManager
from scripts.src.denue_geography import StateManager, MunicipalityManager, LocalityManager
from scripts.src.denue_stores import (
    DataStoreManager, 
    EstratoManager, 
    RoadTypeManager, 
    SettlementTypeManager, 
    BusinessUnitTypeManager
)
from scripts.src.reporter import ReportManager
from scripts.utils.constants import (
    MASTER_COLUMNS, REQUIRED_COLUMNS, COLUMN_ALIASES, ENTITY_VALUE_NORMALIZATION
)

# Configuración de Logger persistente (Consola + Archivo)
log_file = Path("etl_process.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

class DenueProcessor:
    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self.warehouse_path = self.data_root / "denue_warehouse.db"
        
        # Inicialización de Gestores de Dimensiones y Hechos
        self.activities = ActivityManager(self.warehouse_path)
        self.entities = StateManager(self.warehouse_path)
        self.municipalities = MunicipalityManager(self.warehouse_path)
        self.localities = LocalityManager(self.warehouse_path)
        self.estratos = EstratoManager(self.warehouse_path)
        self.roads = RoadTypeManager(self.warehouse_path)
        self.settlements = SettlementTypeManager(self.warehouse_path)
        self.business_units = BusinessUnitTypeManager(self.warehouse_path)
        
        self.datastore = DataStoreManager(self.warehouse_path)
        self.report_mgr = ReportManager(self.data_root)

    def _clean_raw_data(self, zip_path: Path, period: str):
        """
        Motor de limpieza robusto con filtrado estricto de REQUIRED_COLUMNS 
        y generación de Fingerprint Hash.
        """
        logs = {"steps": {}, "stats": {"period": period}, "schema_changes": {}}

        with zipfile.ZipFile(zip_path, 'r') as z:
            csv_candidates = [f for f in z.namelist() if f.endswith('.csv') and 'conjunto_de_datos' in f.lower()]
            if not csv_candidates:
                csv_candidates = [f for f in z.namelist() if f.endswith('.csv') and 'metadatos' not in f.lower()]
            
            csv_name = csv_candidates[0]
            with z.open(csv_name) as f:
                raw_bytes = f.read()
                try:
                    df = pl.read_csv(raw_bytes, infer_schema_length=0, ignore_errors=True, encoding='utf-8')
                    logs["steps"]["encoding"] = "UTF-8"
                except Exception:
                    try:
                        df = pl.read_csv(raw_bytes, infer_schema_length=0, ignore_errors=True, encoding='latin-1')
                        logs["steps"]["encoding"] = "LATIN-1 (Fallback)"
                    except Exception:
                        sanitized_data = raw_bytes.decode('latin-1', errors='replace').encode('utf-8')
                        df = pl.read_csv(sanitized_data, infer_schema_length=0, ignore_errors=True, encoding='utf-8')
                        logs["steps"]["encoding"] = "LATIN-1 (Sanitized)"

        # 1. Normalización de cabeceras
        df = df.rename({c: c.lower().strip().replace('\ufeff', '') for c in df.columns})
        
        # 2. Aliases y Validación de estructura
        applied_aliases = {k: v for k, v in COLUMN_ALIASES.items() if k in df.columns and v not in df.columns}
        if applied_aliases:
            df = df.rename(applied_aliases)
        
        if missing_req_cols := [c for c in REQUIRED_COLUMNS if c not in df.columns]:
            raise ValueError(f"Required columns missing in CSV: {missing_req_cols}")

        # 4. Filtrado estricto (Elimina si REQUIRED_COLUMNS son nulas o vacías)
        initial_count = df.height
        
        # Máscara dinámica: verifica que ninguna columna requerida sea nula o string vacío
        required_mask = pl.all_horizontal([
            pl.col(c).is_not_null() & (pl.col(c).cast(pl.Utf8).str.strip_chars() != "")
            for c in REQUIRED_COLUMNS
        ])
        
        df = df.filter(required_mask)
        logs["stats"]["rows_dropped_invalid_req"] = initial_count - df.height

        # 5. Selección de MASTER_COLUMNS
        selection = [pl.col(c) if c in df.columns else pl.lit(None).alias(c) for c in MASTER_COLUMNS]
        df = df.select(selection)

        # 6. Transformación de Tipos y Normalización de valores
        df = df.with_columns([
            pl.col('cve_ent').cast(pl.Utf8).str.strip_chars().str.pad_start(2, "0"),
            pl.col('cve_mun').cast(pl.Utf8).str.strip_chars().str.pad_start(3, "0"),
            pl.col('cve_loc').cast(pl.Utf8).str.strip_chars().str.pad_start(4, "0"),
            pl.col('latitud').cast(pl.Float64, strict=False),
            pl.col('longitud').cast(pl.Float64, strict=False),
            pl.lit(period).str.to_date(format="%m_%Y").alias('period'),
            pl.col('entidad').cast(pl.Utf8).str.to_uppercase()
              .replace(ENTITY_VALUE_NORMALIZATION, default=pl.col('entidad').str.to_uppercase()),
            pl.col('municipio').str.to_uppercase().str.strip_chars(),
            pl.col('localidad').str.to_uppercase().str.strip_chars(),
            pl.col('tipo_vial').str.to_uppercase().str.strip_chars(),
            pl.col('tipo_asent').str.to_uppercase().str.strip_chars(),
            pl.col('tipounieco').str.to_uppercase().str.strip_chars(),
            pl.col('per_ocu').str.strip_chars()
        ])

        # 7. Generación de ID Único vía Hash (Fingerprinting)
        # Garantiza unicidad incluso si el 'id' original es nulo
        df = df.with_columns(
            pl.concat_str([
                pl.col('id').fill_null('no_id').cast(pl.Utf8).str.strip_chars(),
                pl.col('nom_estab').fill_null('no_name').str.to_uppercase(),
                pl.col('codigo_act').fill_null('000000'),
                pl.col('period').dt.strftime("%Y%m")
            ], separator="|").hash().cast(pl.Utf8).alias("id")
        )
        
        logs["stats"]["rows_final"] = df.height
        return df, logs

    def process_zip(self, zip_path: Path, period: str):
        """Coordina el flujo con reporte de errores detallado y traceback."""
        try:
            df, logs = self._clean_raw_data(zip_path, period)
            
            # Sincronización de dimensiones (Catálogos)
            self.activities.sync(df)
            self.entities.sync(df)
            self.municipalities.sync(df)
            self.localities.sync(df)
            self.estratos.sync(df)
            self.roads.sync(df)
            self.settlements.sync(df)
            self.business_units.sync(df)
            
            # Carga de tabla de hechos
            self.datastore.upsert(df)
            return "DONE", json.dumps(logs), df.height
            
        except Exception as e:
            error_msg = f"Error en {zip_path.name}: {str(e)}"
            full_trace = traceback.format_exc()
            logger.error(f"❌ Falló el procesamiento de {zip_path.name}")
            logger.error(f"DETALLE: {error_msg}")
            logger.error(f"TRACEBACK:\n{full_trace}")
            
            return "ERROR", json.dumps({"status": "FAILED", "error": error_msg, "traceback": full_trace}), 0

    def run_pipeline(self, force: bool = False):
        """Orquestador con reporte persistente y métricas de sesión."""
        if force:
            logger.warning("⚠️ MODO FORCE ACTIVADO")
            import duckdb
            with duckdb.connect(str(self.data_root / "reports.db")) as conn:
                res = conn.execute("SELECT file_path, period, process_status FROM denue_raw").fetchall()
                tasks = [{"file_path": r[0], "period": r[1], "process_status": r[2]} for r in res]
        else:
            tasks = [t for t in self.report_mgr.download_report.get_raw_tasks(retry_errors=True) 
                     if t.get('process_status') != 'DONE']

        if not tasks:
            logger.info("✨ Warehouse actualizado. Nada por procesar.")
            return

        logger.info(f"⚙️ Procesando {len(tasks)} archivos...")
        session_total_rows = 0

        for idx, task in enumerate(tasks, start=1):
            zip_p = Path.cwd() / task['file_path']
            if not zip_p.exists():
                logger.warning(f"   ⚠️ [{idx}/{len(tasks)}] No existe: {zip_p.name}")
                continue

            logger.info(f"📦 [{idx}/{len(tasks)}] Procesando: {zip_p.name} ({task['period']})...")
            status, msg, row_count = self.process_zip(zip_p, task['period'])
            
            self.report_mgr.download_report.update_raw_status_batch([{
                "file_path": task['file_path'], "process_status": status, "process_msg": msg
            }])
            
            if status == "DONE":
                session_total_rows += row_count
                logger.info(f"   ✅ Éxito: {row_count} registros integrados.")

        with self.datastore._get_connection() as conn:
            conn.execute("CHECKPOINT;")
        logger.info(f"✨ Proceso terminado. Total sesión: {session_total_rows:,} registros.")