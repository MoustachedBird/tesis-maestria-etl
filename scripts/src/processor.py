import zipfile
import json
from pathlib import Path
from datetime import datetime
import polars as pl

# Importación de managers normalizados
from scripts.src.denue_activities import ActivityManager
from scripts.src.denue_entities import EntityManager
from scripts.src.denue_stores import DataStoreManager
from scripts.src.reporter import ReportManager
from scripts.utils.constants import (
    MASTER_COLUMNS, REQUIRED_COLUMNS, COLUMN_ALIASES, ENTITY_VALUE_NORMALIZATION
)

class DenueProcessor:
    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self.warehouse_path = self.data_root / "denue_warehouse.db"
        
        # Gestores especializados
        self.activities = ActivityManager(self.warehouse_path)
        self.entities = EntityManager(self.warehouse_path)
        self.datastore = DataStoreManager(self.warehouse_path)
        self.report_mgr = ReportManager(self.data_root)

    def _clean_raw_data(self, zip_path: Path, period: str):
        """
        Motor de limpieza con telemetría de pasos y manejo robusto de encoding.
        Retorna: (DataFrame, dict_logs)
        """
        logs = {
            "steps": {},
            "stats": {"period": period},
            "schema_changes": {}
        }

        with zipfile.ZipFile(zip_path, 'r') as z:
            # Buscar el archivo dentro de la carpeta conjunto_de_datos
            csv_candidates = [f for f in z.namelist() if f.endswith('.csv') and 'conjunto_de_datos' in f.lower()]
            if not csv_candidates:
                csv_candidates = [f for f in z.namelist() if f.endswith('.csv') and 'metadatos' not in f.lower()]
            
            csv_name = csv_candidates[0]
            
            with z.open(csv_name) as f:
                # ESTRATEGIA DE LECTURA ROBUSTA:
                # Leemos todo el contenido como bytes para manejar manualmente el encoding
                raw_bytes = f.read()
                
                try:
                    # Intento 1: UTF-8 puro (ideal para archivos modernos)
                    df = pl.read_csv(raw_bytes, infer_schema_length=0, ignore_errors=True, encoding='utf-8')
                    logs["steps"]["encoding"] = "UTF-8"
                except Exception:
                    try:
                        # Intento 2: Latin-1 (estándar histórico de INEGI/Windows)
                        df = pl.read_csv(raw_bytes, infer_schema_length=0, ignore_errors=True, encoding='latin-1')
                        logs["steps"]["encoding"] = "LATIN-1 (Fallback)"
                    except Exception:
                        # Intento 3: Decodificación con reemplazo (evita que el proceso muera por un carácter corrupto)
                        # Reemplaza bytes inválidos con el símbolo 
                        sanitized_data = raw_bytes.decode('latin-1', errors='replace').encode('utf-8')
                        df = pl.read_csv(sanitized_data, infer_schema_length=0, ignore_errors=True, encoding='utf-8')
                        logs["steps"]["encoding"] = "LATIN-1 (Sanitized)"

        logs["stats"]["rows_raw"] = df.height

        # 1. Normalización de cabeceras (limpieza de espacios, minúsculas y BOM)
        df = df.rename({c: c.lower().strip().replace('\ufeff', '') for c in df.columns})
        logs["steps"]["headers_cleanup"] = "OK"
        
        # 2. Aplicación de Aliases (Uso de COLUMN_ALIASES de constantes)
        applied_aliases = {k: v for k, v in COLUMN_ALIASES.items() if k in df.columns and v not in df.columns}
        if applied_aliases:
            df = df.rename(applied_aliases)
            logs["schema_changes"]["aliases_applied"] = applied_aliases
        
        # 3. Validación de REQUIRED_COLUMNS
        missing_req = [c for c in REQUIRED_COLUMNS if c not in df.columns]
        if missing_req:
            logs["steps"]["validation"] = f"FAILED: Missing {missing_req}"
            # Útil para debuguear: guardamos una muestra de las columnas que sí encontró
            logs["schema_changes"]["found_instead"] = df.columns[:5]
            raise ValueError(f"Required columns missing: {missing_req}")
        logs["steps"]["validation"] = "PASSED"

        # 4. Filtrado de filas (Entidad/Municipio nulos o vacíos)
        initial_count = df.height
        df = df.filter(
            pl.col("entidad").is_not_null() & 
            pl.col("municipio").is_not_null() &
            (pl.col("entidad").str.strip_chars() != "")
        )
        logs["stats"]["rows_dropped_nulls"] = initial_count - df.height

        # 5. Selección y Relleno de MASTER_COLUMNS (Llenar con nulos si no existen en el CSV)
        missing_master = [c for c in MASTER_COLUMNS if c not in df.columns]
        logs["schema_changes"]["filled_columns"] = missing_master
        
        selection = [pl.col(c) if c in df.columns else pl.lit(None).alias(c) for c in MASTER_COLUMNS]
        df = df.select(selection)

        # 6. Normalización de valores y Casts técnicos
        df = df.with_columns([
            pl.col('codigo_act').cast(pl.Utf8).str.strip_chars(),
            pl.col('cve_ent').cast(pl.Utf8).str.strip_chars().str.pad_start(2, "0"),
            pl.col('entidad').cast(pl.Utf8).str.to_uppercase()
              .replace(ENTITY_VALUE_NORMALIZATION, default=pl.col('entidad').str.to_uppercase()),
            pl.col('latitud').cast(pl.Float64, strict=False),
            pl.col('longitud').cast(pl.Float64, strict=False),
            pl.lit(period).alias('period')
        ])
        logs["steps"]["normalization"] = "OK"

        # 7. Generación de row_id (Llave primaria lógica para el Warehouse)
        df = df.with_columns(
            pl.concat_str([pl.col('id').fill_null('0'), pl.col('period')], separator="-").alias("row_id")
        )
        
        logs["stats"]["rows_final"] = df.height
        return df, logs

    def process_zip(self, zip_path: Path, period: str):
        """Coordina el flujo de un solo archivo y devuelve la telemetría."""
        try:
            df, logs = self._clean_raw_data(zip_path, period)
            
            # Sincronización Warehouse (Dimensiones y Hechos)
            self.activities.sync(df)
            self.entities.sync(df)
            self.datastore.upsert(df)
            
            logs["steps"]["warehouse_upsert"] = "SUCCESS"
            return "DONE", json.dumps(logs), df.height
            
        except Exception as e:
            error_payload = {"status": "FAILED", "error": str(e), "at": datetime.now().isoformat()}
            return "ERROR", json.dumps(error_payload), 0

    def run_pipeline(self, force: bool = False):
        """
        Orquestador con métricas de volumen y progreso limpio.
        """
        # 1. Selección de Tareas
        if force:
            print("⚠️ MODO FORCE ACTIVADO")
            import duckdb
            with duckdb.connect(str(self.data_root / "reports.db")) as conn:
                res = conn.execute("SELECT file_path, period, process_status FROM denue_raw").fetchall()
                tasks = [{"file_path": r[0], "period": r[1], "process_status": r[2]} for r in res]
        else:
            tasks = self.report_mgr.download_report.get_raw_tasks(retry_errors=True)
            tasks = [t for t in tasks if t.get('process_status') != 'DONE']

        total_files = len(tasks)
        if total_files == 0:
            print("✨ Warehouse actualizado.")
            return

        print(f"⚙️ Iniciando procesamiento de {total_files} archivos...")
        
        # Contador para el gran total de la sesión
        session_total_rows = 0

        # 2. Bucle de Procesamiento
        for idx, task in enumerate(tasks, start=1):
            zip_p = Path.cwd() / task['file_path']
            period = task['period']
            
            if not zip_p.exists():
                print(f"   ⚠️ [{idx}/{total_files}] Saltando: No existe {zip_p.name}")
                continue

            # El índice (fracción de archivos) solo aparece aquí
            print(f"📦 [{idx}/{total_files}] Procesando: {zip_p.name} ({period})...")
            
            status, msg, row_count = self.process_zip(zip_p, period)
            
            # Actualización del reporte
            self.report_mgr.download_report.update_raw_status_batch([{
                "file_path": task['file_path'],
                "process_status": status,
                "process_msg": msg
            }])
            
            if status == "DONE":
                session_total_rows += row_count
                # Formato limpio de registros procesados
                print(f"   ✅ Éxito: {row_count}/{row_count} registros integrados al Warehouse.")
            else:
                print(f"   ❌ Falló el procesamiento de este archivo.")

        # 3. Consolidación y Resumen Final
        print("\n🧹 Consolidando cambios (Checkpoint)...")
        with self.datastore._get_connection() as conn:
            conn.execute("CHECKPOINT;")
            
        print(f"✨ Proceso terminado. Se integraron {session_total_rows:,} registros en total.")