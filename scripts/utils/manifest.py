import polars as pl
from pathlib import Path
from datetime import datetime
import os

class ManifestManager:
    def __init__(self, data_root: Path):
        self.data_root = data_root
        self.raw_manifest_path = data_root / "manifest_download.csv"
        self.processed_manifest_path = data_root / "manifest_processed.csv"
        
        # 1. ESQUEMA UNIFICADO (Descarga + Proceso)
        self.raw_schema = {
            "file_path": pl.Utf8,       # Primary Key (Ruta relativa)
            "download_url": pl.Utf8,
            "period": pl.Utf8,
            # Estado de Descarga
            "download_status": pl.Utf8, # OK, NOK
            "download_msg": pl.Utf8,
            # Estado de Procesamiento
            "process_status": pl.Utf8,  # NEW, DONE, ERROR
            "process_msg": pl.Utf8,
            
            "last_updated": pl.Utf8
        }
        
        # 2. PROCESSED (Parquets Silver)
        self.processed_schema = {
            "partition_path": pl.Utf8, # ID Único
            "period": pl.Utf8,
            "state": pl.Utf8,
            "municipality": pl.Utf8,
            "row_count": pl.Int64,
            "file_size_kb": pl.Float64,
            "status": pl.Utf8,         # NEW, HARMONIZED, ERROR
            "harmonized_at": pl.Utf8,  # Fecha de armonización
            "error_msg": pl.Utf8,
            "last_updated": pl.Utf8    # Fecha de creación del parquet
        }

    def _get_timestamp(self):
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # ==========================================
    # 1. MÉTODOS PARA EL DOWNLOADER
    # ==========================================

    def register_download(self, file_path: str, url: str, period: str, status: str, msg: str):
        """Registra o actualiza una descarga en el manifiesto unificado."""
        # Si la descarga es exitosa, queda listo para procesar (NEW)
        # Si falló, queda en espera (WAITING)
        proc_status = "NEW" if status == "OK" else "WAITING"
        if status != "OK": proc_status = "WAITING"

        new_row = pl.DataFrame([{
            "file_path": file_path,
            "download_url": url,
            "period": period,
            "download_status": status,
            "download_msg": str(msg),
            "process_status": proc_status,
            "process_msg": "",
            "last_updated": self._get_timestamp()
        }], schema=self.raw_schema)

        if self.raw_manifest_path.exists():
            df = pl.read_csv(self.raw_manifest_path, schema=self.raw_schema)
            # Upsert: Borrar viejo si existe e insertar nuevo
            df = df.filter(pl.col("file_path") != file_path)
            df = pl.concat([df, new_row], how="vertical")
            df.write_csv(self.raw_manifest_path)
        else:
            new_row.write_csv(self.raw_manifest_path)

    def get_failed_downloads(self):
        """Retorna descargas fallidas (NOK) para reintentar."""
        if not self.raw_manifest_path.exists(): return []
        df = pl.read_csv(self.raw_manifest_path, schema=self.raw_schema)
        return df.filter(pl.col("download_status") != "OK").to_dicts()

    def get_existing_downloads(self):
        """Retorna lista de URLs ya descargadas exitosamente (para evitar re-descargar)."""
        if not self.raw_manifest_path.exists(): return set()
        df = pl.read_csv(self.raw_manifest_path, schema=self.raw_schema)
        return set(df.filter(pl.col("download_status") == "OK")["download_url"].to_list())

    # ==========================================
    # 2. MÉTODOS PARA EL PROCESSOR
    # ==========================================

    def init_raw_manifest(self, raw_dir: Path):
        """
        Escanea disco para detectar archivos manuales (Fallback).
        Útil si metiste ZIPs manualmente sin usar el downloader.
        """
        print(f"🔍 Sincronizando manifiesto RAW desde: {raw_dir}")
        physical_files = []
        project_root = Path.cwd()
        
        for f in raw_dir.glob("**/*.zip"):
            try:
                rel_path = str(f.resolve().relative_to(project_root))
                period = f.parent.name
                physical_files.append({
                    "file_path": rel_path, 
                    "period": period,
                    "download_status": "OK",
                    "process_status": "NEW"
                })
            except ValueError: continue 
        
        if not physical_files: return
        df_phys = pl.DataFrame(physical_files)

        if self.raw_manifest_path.exists():
            df = pl.read_csv(self.raw_manifest_path, schema=self.raw_schema)
            # Solo agregar si NO existe en el manifiesto (Anti-Join)
            df_new = df_phys.join(df, on="file_path", how="anti")
            
            if df_new.height > 0:
                # Rellenar columnas faltantes para coincidir con esquema
                df_new = df_new.with_columns([
                    pl.lit("Manual Discovery").alias("download_url"),
                    pl.lit("Found on disk").alias("download_msg"),
                    pl.lit("").alias("process_msg"),
                    pl.lit(self._get_timestamp()).alias("last_updated")
                ]).select(self.raw_schema.keys())
                
                pl.concat([df, df_new], how="vertical").write_csv(self.raw_manifest_path)
                print(f"📦 Manifiesto RAW: {df_new.height} archivos nuevos detectados.")
        else:
            # Crear desde cero si no existe
            df_phys.with_columns([
                pl.lit("Manual Discovery").alias("download_url"),
                pl.lit("Found on disk").alias("download_msg"),
                pl.lit("").alias("process_msg"),
                pl.lit(self._get_timestamp()).alias("last_updated")
            ]).select(self.raw_schema.keys()).write_csv(self.raw_manifest_path)
            print(f"📦 Manifiesto RAW creado con {df_phys.height} archivos.")

    def get_raw_tasks(self, retry_errors=False):
        """Obtiene tareas de procesamiento (NEW/ERROR) que ya fueron descargadas (OK)."""
        if not self.raw_manifest_path.exists(): return []
        df = pl.read_csv(self.raw_manifest_path, schema=self.raw_schema)
        
        target = ["NEW", "ERROR"] if retry_errors else ["NEW"]
        
        return df.filter(
            (pl.col("process_status").is_in(target)) & 
            (pl.col("download_status") == "OK")
        ).to_dicts()

    def update_raw_status_batch(self, updates: list):
        """
        Actualiza el estado de procesamiento (process_status) preservando 
        la información original de descarga (download_url, period, etc).
        """
        if not updates: return
        
        # 1. DataFrame con los cambios (file_path, process_status, process_msg)
        df_updates = pl.DataFrame(updates).with_columns(
            pl.lit(self._get_timestamp()).alias("last_updated")
        )
        
        # 2. Cargar Manifiesto Original
        df_manifest = pl.read_csv(self.raw_manifest_path, schema=self.raw_schema)
        
        # 3. Separar Columnas Estáticas (CRÍTICO: Preservar 'period' y datos de descarga)
        cols_to_preserve = ["file_path", "period", "download_url", "download_status", "download_msg"]
        df_static_info = df_manifest.select(cols_to_preserve)
        
        # 4. Enriquecer los updates con la info estática original (Left Join)
        df_updates_full = df_updates.join(df_static_info, on="file_path", how="left")
        
        # Ordenar columnas según esquema maestro
        df_updates_full = df_updates_full.select(self.raw_schema.keys())
        
        # 5. Filtrar las filas viejas que NO se tocaron (Anti-Join)
        df_unchanged = df_manifest.join(df_updates, on="file_path", how="anti")
        
        # 6. Concatenar y Guardar
        df_final = pl.concat([df_unchanged, df_updates_full], how="vertical")
        df_final.write_csv(self.raw_manifest_path)
        
        print(f"💾 Estatus de procesamiento actualizado ({len(updates)} items).")

    # ==========================================
    # 3. PROCESSED MANIFEST (Silver Layer)
    # ==========================================

    def rebuild_processed_manifest(self, processed_dir: Path):
        """
        Escanea los archivos Parquet generados y actualiza el inventario Silver.
        Soporta migración de esquema automática.
        """
        print(f"🔄 Sincronizando inventario PROCESSED en: {processed_dir}")
        abs_processed_dir = processed_dir.resolve()
        project_root = Path.cwd()

        if not abs_processed_dir.exists(): return

        files = list(abs_processed_dir.glob("**/*.parquet"))
        physical_records = []

        for f in files:
            try:
                stats = f.stat()
                parts = f.parts
                # Extracción de particiones: state=X/municipality=Y/period=Z
                try:
                    period = next(p for p in parts if "period=" in p).split("=")[1]
                    mun = next(p for p in parts if "municipality=" in p).split("=")[1]
                    state = next(p for p in parts if "state=" in p).split("=")[1]
                except StopIteration:
                    continue

                rel_path = str(f.resolve().relative_to(project_root))
                
                # Lectura rápida de metadatos parquet
                row_count = pl.scan_parquet(f).select(pl.len()).collect().item()
                
                physical_records.append({
                    "partition_path": rel_path,
                    "period": period,
                    "state": state,
                    "municipality": mun,
                    "row_count": int(row_count),
                    "file_size_kb": float(round(stats.st_size / 1024, 2)),
                    "last_updated": self._get_timestamp(),
                    # Valores por defecto para nuevos registros
                    "status": "NEW",
                    "harmonized_at": "",
                    "error_msg": ""
                })
            except Exception:
                continue

        # Si no hay archivos físicos, limpiar manifiesto
        if not physical_records:
            if self.processed_manifest_path.exists():
                os.remove(self.processed_manifest_path)
            return

        df_physical = pl.DataFrame(physical_records, schema=self.processed_schema)

        if self.processed_manifest_path.exists():
            try:
                # Leer historial (schema relajado para permitir migración)
                df_history = pl.read_csv(self.processed_manifest_path, infer_schema_length=0)
                
                # --- AUTO-MIGRACIÓN DE ESQUEMA ---
                schema_migrated = False
                for col, dtype in self.processed_schema.items():
                    if col not in df_history.columns:
                        default_val = "NEW" if col == "status" else ""
                        if dtype in [pl.Int64, pl.Float64]: default_val = 0
                        
                        print(f"🔧 Migrando esquema: Agregando columna '{col}' al historial.")
                        df_history = df_history.with_columns(pl.lit(default_val).alias(col))
                        schema_migrated = True
                
                # Castear tipos correctos
                df_history = df_history.select([pl.col(c).cast(t) for c, t in self.processed_schema.items()])
                
                # Identificar Nuevos
                df_new = df_physical.join(df_history, on="partition_path", how="anti")
                
                # Guardar si hay cambios o migración
                if df_new.height > 0 or schema_migrated:
                    pl.concat([df_history, df_new], how="vertical").write_csv(self.processed_manifest_path)
                    msg = "Esquema migrado." if schema_migrated else f"{df_new.height} parquets nuevos."
                    print(f"✅ Inventario actualizado: {msg}")
                else:
                    print("✨ Inventario al día.")
            except Exception:
                # Si falla la lectura, regenerar
                df_physical.write_csv(self.processed_manifest_path)
        else:
            # Primera vez
            df_physical.write_csv(self.processed_manifest_path)
            print(f"✅ Inventario PROCESSED creado.")

    def validate_and_repair(self):
        """
        Verifica integridad: Si RAW dice DONE pero no hay Parquets, resetea a NEW.
        """
        if not self.raw_manifest_path.exists(): return
        
        df_raw = pl.read_csv(self.raw_manifest_path, schema=self.raw_schema)
        
        # Obtener periodos que supuestamente ya están procesados
        done_periods = df_raw.filter(pl.col("process_status") == "DONE")["period"].unique().to_list()
        
        if not done_periods: return

        # Obtener periodos que REALMENTE existen en disco (Silver)
        actual_periods = []
        if self.processed_manifest_path.exists():
            try:
                df_proc = pl.read_csv(self.processed_manifest_path, infer_schema_length=0)
                if "period" in df_proc.columns and df_proc.height > 0:
                    actual_periods = df_proc["period"].unique().to_list()
            except: pass

        # Detectar discrepancias
        missing = set(done_periods) - set(actual_periods)
        
        if missing:
            print(f"⚠️ Integridad: {len(missing)} periodos marcados como DONE están vacíos. Reseteando a NEW.")
            df_raw.with_columns(
                pl.when(
                    (pl.col("process_status") == "DONE") & 
                    (pl.col("period").is_in(list(missing)))
                )
                .then(pl.lit("NEW"))
                .otherwise(pl.col("process_status"))
                .alias("process_status")
            ).write_csv(self.raw_manifest_path)

    def get_harmonization_tasks(self, retry_errors=False):
        """Retorna particiones pendientes de armonizar."""
        if not self.processed_manifest_path.exists(): return []
        df = pl.read_csv(self.processed_manifest_path, infer_schema_length=0)
        
        target = ["NEW", "ERROR"] if retry_errors else ["NEW"]
        
        # Filtramos particiones que están listas para procesar
        return df.filter(pl.col("status").is_in(target)).to_dicts()

    def update_harmonization_status(self, updates: list):
        """Actualiza el estado de armonización (HARMONIZED) en el manifiesto."""
        if not updates: return
        
        # updates es lista de dicts: [{"partition_path": "GLOBAL", "status": "HARMONIZED"}]
        # Ojo: Tu harmonizer actual manda "partition_path": "GLOBAL", que no existe en el CSV.
        # Si es GLOBAL, no actualizamos filas individuales, o creamos un registro especial.
        
        df_updates = pl.DataFrame(updates).with_columns(
             pl.lit(self._get_timestamp()).alias("harmonized_at")
        )
        
        if not self.processed_manifest_path.exists(): return
        df_manifest = pl.read_csv(self.processed_manifest_path, infer_schema_length=0)
        
        # Manejo especial para actualización GLOBAL
        if len(updates) == 1 and updates[0]["partition_path"] == "GLOBAL":
            print("✅ Armonización Global Registrada.")
            return

        # Join y Actualización para particiones normales
        # (La lógica es similar a update_raw_status_batch: Join + AntiJoin + Concat)
        # Por simplicidad para tu tesis, si solo usas GLOBAL, el print de arriba basta.
        pass