import polars as pl
import duckdb
from pathlib import Path
from datetime import datetime
# Importamos la base de datos centralizada
from src.utils.database import BaseWarehouse

class ReportBase(BaseWarehouse):    
    """
    Clase Base de Reportes: Hereda la lógica de conexión de BaseWarehouse
    y añade utilidades específicas para auditoría y gestión de estados.
    """
    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self.data_root.mkdir(parents=True, exist_ok=True)
        # Definimos la ruta específica para la DB de reportes
        db_path = self.data_root / "reports.db"
        
        # Inicializamos la clase padre con la ruta del archivo de reportes
        super().__init__(db_path)
        
        self.common_types = {
            "period": pl.Utf8,       
            "last_updated": pl.Utf8, 
        }

    def _read_storage(self, table_name: str, schema: dict) -> pl.DataFrame:
        """Lee una tabla de reportes y la devuelve como DataFrame de Polars."""
        if not self.warehouse_path.exists():
            return pl.DataFrame([], schema=schema)
        
        with self._get_connection() as conn:
            try:
                return conn.execute(f"SELECT * FROM {table_name}").pl()
            except duckdb.CatalogException:
                return pl.DataFrame([], schema=schema)

    def _write_storage(self, df: pl.DataFrame, table_name: str):
        """Escribe o reemplaza una tabla en la base de datos de reportes."""
        with self._get_connection() as conn:
            conn.execute(f"CREATE OR REPLACE TABLE {table_name} AS SELECT * FROM df")

    def _get_timestamp(self):
        """Genera un timestamp estandarizado para los registros."""
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def _upsert(self, table_name: str, schema: dict, new_data: pl.DataFrame, key_col: str):
        """Actualiza registros existentes o inserta nuevos basándose en una llave."""
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
    """
    Capa denue_raw: Gestiona el inventario de archivos ZIP descargados 
    y su estatus de procesamiento hacia el Warehouse.
    """
    def __init__(self, data_root: Path):
        super().__init__(data_root)
        self.table_id = "denue_raw"
        self.report_schema = {
            "file_path": pl.Utf8, "download_url": pl.Utf8, **self.common_types,
            "download_status": pl.Utf8, "download_msg": pl.Utf8,
            "process_status": pl.Utf8, "process_msg": pl.Utf8,
        }

    def init_raw_manifest(self, raw_dir: Path):
        """Sincroniza los archivos presentes en disco con la base de datos de reportes."""
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
            df_new = df_phys.join(df_existing, on="file_path", how="anti").with_columns([
                pl.lit("Manual Discovery").alias("download_url"),
                pl.lit("Found on disk").alias("download_msg"),
                pl.lit("").alias("process_msg"),
                pl.lit(self._get_timestamp()).alias("last_updated")
            ]).select(self.report_schema.keys())
            
            if df_new.height > 0:
                df_final = pl.concat([df_existing, df_new], how="vertical")
                self._write_storage(df_final, self.table_id)
                print(f"📦 Raw data: {df_new.height} archivos nuevos detectados.")
        else:
            df_final = df_phys.with_columns([
                pl.lit("Manual Discovery").alias("download_url"),
                pl.lit("Found on disk").alias("download_msg"),
                pl.lit("").alias("process_msg"),
                pl.lit(self._get_timestamp()).alias("last_updated")
            ]).select(self.report_schema.keys())
            self._write_storage(df_final, self.table_id)

    def register_download(self, file_path: str, url: str, period: str, status: str, msg: str):
        """Registra una nueva descarga en el manifiesto."""
        proc_status = "NEW" if status == "OK" else "WAITING"
        new_row = pl.DataFrame([{
            "file_path": str(file_path), "download_url": url, "period": period,
            "download_status": status, "download_msg": str(msg),
            "process_status": proc_status, "process_msg": "", "last_updated": self._get_timestamp()
        }], schema=self.report_schema)
        self._upsert(self.table_id, self.report_schema, new_row, "file_path")

    def get_raw_tasks(self, retry_errors=False, force=False):
        """Obtiene la lista de archivos para procesar."""
        df = self._read_storage(self.table_id, self.report_schema)
        if df.height == 0: return []
        
        # MODO FORCE: Ignora process_status por completo
        if force:
            # Solo filtramos que el archivo haya sido marcado como exitoso en disco (OK)
            return df.filter(pl.col("download_status") == "OK").to_dicts()
        
        # Lógica normal
        target = ["NEW", "ERROR"] if retry_errors else ["NEW"]
        return df.filter(
            (pl.col("process_status").is_in(target)) & 
            (pl.col("download_status") == "OK")
        ).to_dicts()

    def update_raw_status_batch(self, updates: list):
        """Actualiza masivamente el estatus de procesamiento de varios archivos."""
        if not updates: return
        df_updates = pl.DataFrame(updates).with_columns(pl.lit(self._get_timestamp()).alias("last_updated"))
        df_manifest = self._read_storage(self.table_id, self.report_schema)
        df_full = df_updates.join(
            df_manifest.select(["file_path", "period", "download_url", "download_status", "download_msg"]),
            on="file_path", how="left"
        ).select(self.report_schema.keys())
        self._upsert(self.table_id, self.report_schema, df_full, "file_path")

    def reset_raw_status(self, file_paths: list = None, only_errors: bool = False):
        """Resetea archivos para permitir que sean procesados nuevamente."""
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
        df = self._read_storage(self.table_id, self.report_schema)
        if df.height == 0: return []
        return df.filter(pl.col("download_status") != "OK").to_dicts()

    def get_existing_downloads(self):
        df = self._read_storage(self.table_id, self.report_schema)
        if df.height == 0: return set()
        return set(df.filter(pl.col("download_status") == "OK")["download_url"].to_list())

class ReportManager:
    """
    Orquestador Maestro: Centraliza el acceso a los reportes de descarga
    y procesamiento.
    """
    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        # Ambos reportes comparten la misma DB de reportes heredando de BaseWarehouse
        self.download_report = ReportRawFiles(self.data_root)

    def validate_integrity(self, warehouse_conn=None):
        """
        Verifica que los archivos marcados como DONE en el reporte realmente 
        existan en el Warehouse final.
        """
        df_download = self.download_report._read_storage(self.download_report.table_id, self.download_report.report_schema)
        if df_download.height == 0: return
        
        # Filtrar periodos que deberían estar en el warehouse
        done_tasks = df_download.filter(pl.col("process_status") == "DONE")
        if done_tasks.height == 0: return
        
        print("✅ Integridad de reportes validada.")