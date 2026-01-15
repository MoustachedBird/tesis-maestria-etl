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
        self.raw_schema = {
            "file_path": pl.Utf8,    # ID Único (ruta relativa)
            "period": pl.Utf8,
            "status": pl.Utf8,       # NEW, DONE, ERROR
            "last_updated": pl.Utf8,
            "error_msg": pl.Utf8
        }
        
        self.processed_schema = {
            "partition_path": pl.Utf8, # ID Único
            "period": pl.Utf8,
            "state": pl.Utf8,
            "municipality": pl.Utf8,
            "row_count": pl.Int64,
            "file_size_kb": pl.Float64,
            "last_updated": pl.Utf8
        }

    def _get_timestamp(self):
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # ==========================================
    # 1. RAW MANIFEST (Control de Proceso)
    # ==========================================

    def init_raw_manifest(self, raw_dir: Path):
        """Escanea disco y sincroniza manifest_download.csv"""
        
        # A. Escanear archivos físicos
        physical_files = []
        for f in raw_dir.glob("**/*.zip"):
            try:
                rel_path = str(f.relative_to(self.data_root))
                period = f.parent.name
                physical_files.append({"file_path": rel_path, "period": period})
            except ValueError:
                continue # Ignorar archivos fuera de estructura
        
        if not physical_files:
            return

        df_physical = pl.DataFrame(physical_files)

        # B. Cargar Manifiesto existente
        if self.raw_manifest_path.exists():
            df_manifest = pl.read_csv(self.raw_manifest_path)
            
            # Detectar Nuevos: Están en físico pero no en manifiesto
            # Anti-Join
            df_new = df_physical.join(df_manifest, on="file_path", how="anti")
            
            if df_new.height > 0:
                df_new = df_new.with_columns([
                    pl.lit("NEW").alias("status"),
                    pl.lit(self._get_timestamp()).alias("last_updated"),
                    pl.lit("").alias("error_msg")
                ])
                # Concatenar y guardar
                df_final = pl.concat([df_manifest, df_new], how="vertical")
                df_final.write_csv(self.raw_manifest_path)
                print(f"📦 Manifiesto RAW: {df_new.height} archivos nuevos detectados.")
        else:
            # Crear desde cero
            df_final = df_physical.with_columns([
                pl.lit("NEW").alias("status"),
                pl.lit(self._get_timestamp()).alias("last_updated"),
                pl.lit("").alias("error_msg")
            ])
            df_final.write_csv(self.raw_manifest_path)
            print(f"📦 Manifiesto RAW creado con {df_final.height} archivos.")

    def get_raw_tasks(self, retry_errors=False):
        """Retorna lista de diccionarios con tareas pendientes"""
        if not self.raw_manifest_path.exists():
            return []
            
        df = pl.read_csv(self.raw_manifest_path)
        
        target_status = ["NEW"]
        if retry_errors:
            target_status.append("ERROR")
            
        tasks = df.filter(pl.col("status").is_in(target_status))
        return tasks.to_dicts()

    def update_raw_status_batch(self, updates: list):
        """
        Recibe lista de dicts: [{'file_path': '...', 'status': 'DONE', 'error_msg': ''}]
        Y actualiza el CSV de golpe.
        """
        if not updates: return
        
        df_updates = pl.DataFrame(updates).with_columns(
            pl.lit(self._get_timestamp()).alias("last_updated")
        )
        
        df_manifest = pl.read_csv(self.raw_manifest_path)
        
        # Lógica de Actualización en Polars:
        # 1. Eliminar del original las filas que vamos a actualizar
        df_manifest = df_manifest.join(df_updates, on="file_path", how="anti")
        
        # 2. Agregar las filas actualizadas
        # Aseguramos que tengan las mismas columnas
        df_updates = df_updates.select(df_manifest.columns)
        
        df_final = pl.concat([df_manifest, df_updates], how="vertical")
        df_final.write_csv(self.raw_manifest_path)
        print(f"💾 Manifiesto RAW actualizado ({len(updates)} registros).")

    # ==========================================
    # 2. PROCESSED MANIFEST (Catálogo Silver)
    # ==========================================

    def rebuild_processed_manifest(self, processed_dir: Path):
        print(f"🔄 Escaneando directorio: {processed_dir}")
        records = []
        
        # 1. Asegurar rutas absolutas para evitar errores de subpath
        abs_processed_dir = processed_dir.resolve()
        abs_data_root = self.data_root.resolve()

        if not abs_processed_dir.exists():
            print(f"❌ Error: El directorio {abs_processed_dir} no existe.")
            return

        # 2. Glob pattern sobre la ruta absoluta
        files = list(abs_processed_dir.glob("**/*.parquet"))
        print(f"📊 Archivos encontrados: {len(files)}")

        for f in files:
            try:
                stats = f.stat()
                # Parsear estructura de carpetas (state=X/municipality=Y/period=Z)
                parts = f.parts
                
                # Búsqueda robusta de particiones
                try:
                    period = next(p for p in parts if "period=" in p).split("=")[1]
                    mun = next(p for p in parts if "municipality=" in p).split("=")[1]
                    state = next(p for p in parts if "state=" in p).split("=")[1]
                except StopIteration:
                    continue

                # --- FIX: Calcular ruta relativa usando absolutos ---
                # Esto soluciona el error "is not in the subpath of"
                rel_path = str(f.resolve().relative_to(abs_data_root))
                
                # Lazy count (rápido)
                row_count = pl.scan_parquet(f).select(pl.len()).collect().item()
                
                records.append({
                    "partition_path": rel_path,
                    "period": period,
                    "state": state,
                    "municipality": mun,
                    "row_count": row_count,
                    "file_size_kb": round(stats.st_size / 1024, 2),
                    "last_updated": self._get_timestamp()
                })
            except Exception as e:
                # print(f"⚠️ Error menor leyendo {f.name}: {e}")
                continue

        if records:
            df = pl.DataFrame(records)
            df.write_csv(self.processed_manifest_path)
            print(f"✅ Manifiesto PROCESSED guardado en: {self.processed_manifest_path}")
            print(f"📚 Total de archivos indexados: {len(records)}")
        else:
            print("⚠️ No se encontraron archivos Parquet para indexar.")