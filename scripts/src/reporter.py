import polars as pl
import duckdb
from pathlib import Path
from datetime import datetime

class ReportBase:    
    """Clase Base: Abstrae el motor de almacenamiento usando DuckDB."""
    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_root / "reports.db"
        
        self.common_types = {
            "period": pl.Utf8,       
            "last_updated": pl.Utf8, 
        }

    def _get_connection(self):
        return duckdb.connect(str(self.db_path))

    def _read_storage(self, table_name: str, schema: dict) -> pl.DataFrame:
        if not self.db_path.exists():
            return pl.DataFrame([], schema=schema)
        
        with self._get_connection() as conn:
            try:
                return conn.execute(f"SELECT * FROM {table_name}").pl()
            except duckdb.CatalogException:
                return pl.DataFrame([], schema=schema)

    def _write_storage(self, df: pl.DataFrame, table_name: str):
        with self._get_connection() as conn:
            conn.execute(f"CREATE OR REPLACE TABLE {table_name} AS SELECT * FROM df")

    def _get_timestamp(self):
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def _upsert(self, table_name: str, schema: dict, new_data: pl.DataFrame, key_col: str):
        df_existing = self._read_storage(table_name, schema)
        
        if df_existing.height > 0:
            df_final = pl.concat([
                df_existing.filter(pl.col(key_col).is_in(new_data[key_col]).not_()),
                new_data
            ], how="vertical")
        else:
            df_final = new_data
        
        self._write_storage(df_final, table_name)

class ReportRawFiles(ReportBase):
    """Capa denue_raw: Gestiona el manifiesto de descargas en DuckDB."""
    def __init__(self, data_root: Path):
        super().__init__(data_root)
        self.table_id = "denue_raw"
        self.report_schema = {
            "file_path": pl.Utf8, "download_url": pl.Utf8, **self.common_types,
            "download_status": pl.Utf8, "download_msg": pl.Utf8,
            "process_status": pl.Utf8, "process_msg": pl.Utf8,
        }

    def init_raw_manifest(self, raw_dir: Path):
        """Escanea disco para detectar archivos manuales (Fallback)."""
        print(f"🔍 Sincronizando RAW desde disco: {raw_dir}")
        physical_files = []
        project_root = Path.cwd()
        
        for f in raw_dir.glob("**/*.zip"):
            try:
                rel_path = str(f.resolve().relative_to(project_root))
                physical_files.append({
                    "file_path": rel_path, 
                    "period": f.parent.name,
                    "download_status": "OK",
                    "process_status": "NEW"
                })
            except: continue
        
        if not physical_files: return
        df_phys = pl.DataFrame(physical_files)
        df_existing = self._read_storage(self.table_id, self.report_schema)

        if df_existing.height > 0:
            # Solo agregar lo que NO existe en la base de datos
            df_new = df_phys.join(df_existing, on="file_path", how="anti").with_columns([
                pl.lit("Manual Discovery").alias("download_url"),
                pl.lit("Found on disk").alias("download_msg"),
                pl.lit("").alias("process_msg"),
                pl.lit(self._get_timestamp()).alias("last_updated")
            ]).select(self.report_schema.keys())
            
            if df_new.height > 0:
                df_final = pl.concat([df_existing, df_new], how="vertical")
                self._write_storage(df_final, self.table_id)
                print(f"📦 Bronze: {df_new.height} archivos nuevos detectados.")
        else:
            df_final = df_phys.with_columns([
                pl.lit("Manual Discovery").alias("download_url"),
                pl.lit("Found on disk").alias("download_msg"),
                pl.lit("").alias("process_msg"),
                pl.lit(self._get_timestamp()).alias("last_updated")
            ]).select(self.report_schema.keys())
            self._write_storage(df_final, self.table_id)

    def register_download(self, file_path: str, url: str, period: str, status: str, msg: str):
        proc_status = "NEW" if status == "OK" else "WAITING"
        new_row = pl.DataFrame([{
            "file_path": str(file_path), "download_url": url, "period": period,
            "download_status": status, "download_msg": str(msg),
            "process_status": proc_status, "process_msg": "", "last_updated": self._get_timestamp()
        }], schema=self.report_schema)
        self._upsert(self.table_id, self.report_schema, new_row, "file_path")

    def get_raw_tasks(self, retry_errors=False):
        df = self._read_storage(self.table_id, self.report_schema)
        if df.height == 0: return []
        target = ["NEW", "ERROR"] if retry_errors else ["NEW"]
        return df.filter((pl.col("process_status").is_in(target)) & (pl.col("download_status") == "OK")).to_dicts()

    def update_raw_status_batch(self, updates: list):
        if not updates: return
        df_updates = pl.DataFrame(updates).with_columns(pl.lit(self._get_timestamp()).alias("last_updated"))
        df_manifest = self._read_storage(self.table_id, self.report_schema)
        df_full = df_updates.join(
            df_manifest.select(["file_path", "period", "download_url", "download_status", "download_msg"]),
            on="file_path", how="left"
        ).select(self.report_schema.keys())
        self._upsert(self.table_id, self.report_schema, df_full, "file_path")

    def reset_raw_status(self, file_paths: list = None, only_errors: bool = False):
        df_manifest = self._read_storage(self.table_id, self.report_schema)
        if df_manifest.height == 0: return
        condition = pl.col("file_path").is_in(file_paths) if file_paths else pl.lit(True)
        if only_errors: condition = condition & (pl.col("process_status") == "ERROR")
        df_to_reset = df_manifest.filter(condition).with_columns([
            pl.lit("NEW").alias("process_status"), pl.lit("").alias("process_msg"),
            pl.lit(self._get_timestamp()).alias("last_updated")
        ])
        if df_to_reset.height > 0:
            self._upsert(self.table_id, self.report_schema, df_to_reset, "file_path")

    def get_failed_downloads(self):
        """Retorna una lista de dicts con las descargas que fallaron."""
        df = self._read_storage(self.table_id, self.report_schema)
        if df.height == 0: 
            return []
        return df.filter(pl.col("download_status") != "OK").to_dicts()

    def get_existing_downloads(self):
        """Retorna un SET con las URLs que ya se descargaron correctamente."""
        df = self._read_storage(self.table_id, self.report_schema)
        if df.height == 0: 
            return set()
        return set(df.filter(pl.col("download_status") == "OK")["download_url"].to_list())

class ReportProcessedFiles(ReportBase):
    """Capa denue_processed: Gestión de inventario Parquet en DuckDB."""
    def __init__(self, data_root: Path):
        super().__init__(data_root)
        self.table_id = "denue_processed"
        self.report_schema = {
            "partition_path": pl.Utf8, **self.common_types,
            "state": pl.Utf8, "municipality": pl.Utf8, "row_count": pl.Int64,
            "file_size_kb": pl.Float64, "status": pl.Utf8,
            "harmonized_at": pl.Utf8, "error_msg": pl.Utf8,
        }

    def rebuild_processed_manifest(self, processed_dir: Path):
        print(f"🔄 Sincronizando denue_processed en: {processed_dir}")
        physical_records = []
        project_root = Path.cwd()
        for f in processed_dir.glob("**/*.parquet"):
            try:
                parts = f.parts
                physical_records.append({
                    "partition_path": str(f.resolve().relative_to(project_root)),
                    "period": next(p for p in parts if "period=" in p).split("=")[1],
                    "state": next(p for p in parts if "state=" in p).split("=")[1],
                    "municipality": next(p for p in parts if "municipality=" in p).split("=")[1],
                    "row_count": pl.scan_parquet(f).select(pl.len()).collect().item(),
                    "file_size_kb": float(round(f.stat().st_size / 1024, 2)),
                    "status": "NEW", "harmonized_at": "", "error_msg": "", "last_updated": self._get_timestamp()
                })
            except: continue
        if physical_records:
            df_phys = pl.DataFrame(physical_records, schema=self.report_schema)
            self._write_storage(df_phys, self.table_id)

    def update_harmonization_status(self, updates: list):
        if not updates: return
        df_updates = pl.DataFrame(updates).with_columns([
             pl.lit(self._get_timestamp()).alias("harmonized_at"),
             pl.lit(self._get_timestamp()).alias("last_updated")
        ])
        df_manifest = self._read_storage(self.table_id, self.report_schema)
        df_full = df_updates.join(
            df_manifest.drop(["status", "harmonized_at", "error_msg", "last_updated"]),
            on="partition_path", how="left"
        ).select(self.report_schema.keys())
        self._upsert(self.table_id, self.report_schema, df_full, "partition_path")

class ReportManager:
    """Orquestador Maestro."""
    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self.download_report = ReportRawFiles(self.data_root)
        self.process_report = ReportProcessedFiles(self.data_root)

    def validate_integrity(self):
        df_download = self.download_report._read_storage(self.download_report.table_id, self.download_report.report_schema)
        if df_download.height == 0: return
        done_periods = df_download.filter(pl.col("process_status") == "DONE")["period"].unique().to_list()
        
        df_process = self.process_report._read_storage(self.process_report.table_id, self.process_report.report_schema)
        actual_periods = df_process["period"].unique().to_list() if df_process.height > 0 else []
        
        missing = set(done_periods) - set(actual_periods)
        if missing:
            print(f"⚠️ Integridad: {len(missing)} periodos reseteados.")
            self.download_report.reset_raw_status(file_paths=None, only_errors=False)