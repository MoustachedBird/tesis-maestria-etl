import polars as pl
from pathlib import Path
from datetime import datetime
import os

class ManifestManager:
    def __init__(self, data_root: Path):
        self.data_root = data_root
        self.raw_manifest_path = data_root / "manifest_download.csv"
        self.processed_manifest_path = data_root / "manifest_processed.csv"
        
        # Esquemas
        # 1. RAW (Descargas ZIP)
        self.raw_schema = {
            "file_path": pl.Utf8,    # ID Único (ruta relativa)
            "period": pl.Utf8,
            "status": pl.Utf8,       # NEW, DONE, ERROR
            "last_updated": pl.Utf8,
            "error_msg": pl.Utf8
        }
        
        # 2. PROCESSED (Parquets Silver) - ¡Mejorado!
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

    # ... [MANTENER MÉTODOS init_raw_manifest, get_raw_tasks, update_raw_status_batch IGUAL QUE ANTES] ...
    # Voy a resumir init_raw_manifest para no repetir todo el código, asumiendo que ya lo tienes
    # Si lo necesitas completo dímelo, pero solo cambió lo de abajo:

    def init_raw_manifest(self, raw_dir: Path):
        # ... (código existente de init_raw_manifest) ...
        # (Asegúrate de tener la versión corregida con Path.cwd() que te di antes)
        print(f"🔍 Sincronizando manifiesto RAW desde: {raw_dir}")
        physical_files = []
        project_root = Path.cwd()
        for f in raw_dir.glob("**/*.zip"):
            try:
                rel_path = str(f.resolve().relative_to(project_root))
                period = f.parent.name
                physical_files.append({"file_path": rel_path, "period": period})
            except ValueError: continue 
        if not physical_files: return
        
        df_physical = pl.DataFrame(physical_files)
        if self.raw_manifest_path.exists():
            df_manifest = pl.read_csv(self.raw_manifest_path, schema=self.raw_schema)
            df_new = df_physical.join(df_manifest, on="file_path", how="anti")
            if df_new.height > 0:
                df_new = df_new.with_columns([
                    pl.lit("NEW").alias("status"), pl.lit(self._get_timestamp()).alias("last_updated"), pl.lit("").alias("error_msg")
                ]).select(self.raw_schema.keys())
                pl.concat([df_manifest, df_new], how="vertical").write_csv(self.raw_manifest_path)
                print(f"📦 Manifiesto RAW: {df_new.height} archivos nuevos.")
        else:
            df_final = df_physical.with_columns([
                pl.lit("NEW").alias("status"), pl.lit(self._get_timestamp()).alias("last_updated"), pl.lit("").alias("error_msg")
            ]).select(self.raw_schema.keys())
            df_final.write_csv(self.raw_manifest_path)
            print(f"📦 Manifiesto RAW creado: {df_final.height} archivos.")

    def get_raw_tasks(self, retry_errors=False):
        if not self.raw_manifest_path.exists(): return []
        df = pl.read_csv(self.raw_manifest_path, schema=self.raw_schema)
        target = ["NEW", "ERROR"] if retry_errors else ["NEW"]
        return df.filter(pl.col("status").is_in(target)).to_dicts()

    def update_raw_status_batch(self, updates: list):
        if not updates: return
        df_updates = pl.DataFrame(updates).with_columns(pl.lit(self._get_timestamp()).alias("last_updated"))
        df_manifest = pl.read_csv(self.raw_manifest_path, schema=self.raw_schema)
        df_unchanged = df_manifest.join(df_updates, on="file_path", how="anti")
        df_updates_clean = df_updates.select(df_manifest.columns)
        pl.concat([df_unchanged, df_updates_clean], how="vertical").write_csv(self.raw_manifest_path)
        print(f"💾 RAW status actualizado ({len(updates)} items).")

    # ... [MANTENER rebuild_processed_manifest, get_harmonization_tasks, update_harmonization_status IGUAL] ...
    
    def rebuild_processed_manifest(self, processed_dir: Path):
        print(f"🔄 Sincronizando inventario PROCESSED en: {processed_dir}")
        
        abs_processed_dir = processed_dir.resolve()
        project_root = Path.cwd()

        if not abs_processed_dir.exists():
            print(f"❌ Error: No existe {abs_processed_dir}")
            return

        # 1. Escaneo Físico
        files = list(abs_processed_dir.glob("**/*.parquet"))
        physical_records = []

        for f in files:
            try:
                stats = f.stat()
                parts = f.parts
                try:
                    period = next(p for p in parts if "period=" in p).split("=")[1]
                    mun = next(p for p in parts if "municipality=" in p).split("=")[1]
                    state = next(p for p in parts if "state=" in p).split("=")[1]
                except StopIteration:
                    continue

                rel_path = str(f.resolve().relative_to(project_root))
                row_count = pl.scan_parquet(f).select(pl.len()).collect().item()
                
                physical_records.append({
                    "partition_path": rel_path,
                    "period": period,
                    "state": state,
                    "municipality": mun,
                    "row_count": int(row_count),
                    "file_size_kb": float(round(stats.st_size / 1024, 2)),
                    "last_updated": self._get_timestamp(),
                    "status": "NEW",
                    "harmonized_at": "",
                    "error_msg": ""
                })
            except Exception:
                continue

        if not physical_records:
            print("⚠️ No se encontraron parquets físicos.")
            return

        df_physical = pl.DataFrame(physical_records, schema=self.processed_schema)

        # 2. Fusión y Migración
        if self.processed_manifest_path.exists():
            try:
                # Leer sin esquema estricto para permitir migración
                df_history = pl.read_csv(self.processed_manifest_path, infer_schema_length=0)
                
                schema_migrated = False  # <--- NUEVA BANDERA DE CONTROL

                # A. Migración de Esquema
                for col, dtype in self.processed_schema.items():
                    if col not in df_history.columns:
                        default_val = "NEW" if col == "status" else ""
                        if dtype in [pl.Int64, pl.Float64]: default_val = 0
                        
                        print(f"🔧 Migrando esquema: Agregando columna '{col}' al historial.")
                        df_history = df_history.with_columns(pl.lit(default_val).alias(col))
                        schema_migrated = True

                # B. Normalización de Tipos
                df_history = df_history.select([
                    pl.col(c).cast(t) for c, t in self.processed_schema.items()
                ])

                # C. Detección de Nuevos
                df_new = df_physical.join(df_history, on="partition_path", how="anti")
                
                # D. Decisión de Guardado (CORREGIDA)
                # Guardamos si hay archivos nuevos O si hubo migración de esquema
                if df_new.height > 0 or schema_migrated:
                    df_final = pl.concat([df_history, df_new], how="vertical")
                    df_final.write_csv(self.processed_manifest_path)
                    
                    if schema_migrated and df_new.height == 0:
                        print(f"✅ Esquema migrado exitosamente (Se actualizaron headers).")
                    else:
                        print(f"✅ Inventario actualizado: {df_new.height} parquets nuevos.")
                else:
                    print("✨ Inventario al día (Esquema y Datos correctos).")

            except Exception as e:
                print(f"⚠️ Error leyendo historial ({e}). Regenerando.")
                df_physical.write_csv(self.processed_manifest_path)
        else:
            df_physical.write_csv(self.processed_manifest_path)
            print(f"✅ Inventario PROCESSED creado: {df_physical.height} parquets.")
    
    def validate_and_repair(self):
        """
        Verifica la integridad: Si RAW dice DONE, pero no hay Parquets para ese periodo,
        resetea el estado a NEW.
        """
        print("🕵️‍♂️ Ejecutando verificación de integridad (Reality Check)...")
        
        if not self.raw_manifest_path.exists():
            return

        # 1. Cargar RAW
        df_raw = pl.read_csv(self.raw_manifest_path, schema=self.raw_schema)
        
        # 2. Obtener Periodos que creemos que están listos (DONE)
        done_periods = df_raw.filter(pl.col("status") == "DONE")["period"].unique().to_list()
        
        if not done_periods:
            print("✨ No hay tareas marcadas como DONE para verificar.")
            return

        # 3. Obtener Periodos que REALMENTE existen en processed
        actual_periods = []
        if self.processed_manifest_path.exists():
            df_proc = pl.read_csv(self.processed_manifest_path, schema=self.processed_schema)
            if df_proc.height > 0:
                actual_periods = df_proc["period"].unique().to_list()

        # 4. Encontrar inconsistencias (Están en RAW como DONE, pero no en Processed)
        # Nota: Convertimos a sets para restar
        missing_periods = set(done_periods) - set(actual_periods)
        
        if missing_periods:
            print(f"⚠️ ALERTA DE INTEGRIDAD: Se detectaron {len(missing_periods)} periodos marcados como DONE pero vacíos en disco.")
            print(f"   Periodos afectados: {list(missing_periods)[:5]} ...")
            
            # 5. Reparar: Cambiar DONE -> NEW para esos periodos
            df_repaired = df_raw.with_columns(
                pl.when(
                    (pl.col("status") == "DONE") & 
                    (pl.col("period").is_in(list(missing_periods)))
                )
                .then(pl.lit("NEW")) # Resetear a NEW
                .otherwise(pl.col("status"))
            )
            
            df_repaired.write_csv(self.raw_manifest_path)
            print(f"✅ Reparación completada. Los archivos se volverán a procesar.")
        else:
            print("✨ Integridad verificada: El manifiesto coincide con el disco.")