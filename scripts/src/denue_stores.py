import polars as pl
from scripts.utils.database import BaseWarehouse

class DataStoreManager(BaseWarehouse):
    def __init__(self, warehouse_path):
        super().__init__(warehouse_path)
        self.table_id = "establecimientos"

    def upsert(self, df: pl.DataFrame):
        """Inserta establecimientos omitiendo nombres redundantes de act y entidad."""
        # Mantenemos solo los IDs para los JOINS posteriores
        df_optimized = df.drop(["nombre_act", "entidad"])

        with self._get_connection() as conn:
            conn.execute(f"CREATE TABLE IF NOT EXISTS {self.table_id} AS SELECT * FROM df_optimized WHERE 1=0")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_row_id ON {self.table_id} (row_id)")
            
            conn.execute(f"""
                INSERT INTO {self.table_id} 
                SELECT src.* FROM df_optimized src
                LEFT JOIN {self.table_id} tgt ON src.row_id = tgt.row_id
                WHERE tgt.row_id IS NULL
            """)