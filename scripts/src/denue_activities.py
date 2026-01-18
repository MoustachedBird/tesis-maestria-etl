import polars as pl
from scripts.utils.database import BaseWarehouse

class ActivityManager(BaseWarehouse):
    def __init__(self, warehouse_path):
        super().__init__(warehouse_path)
        self.table_id = "denue_actividades_economicas"

    def sync(self, df: pl.DataFrame):
        """Sincroniza el catálogo SCIAN con llave compuesta."""
        # Seleccionamos y limpiamos las columnas del catálogo
        catalog_df = df.select([
            pl.col("codigo_act").cast(pl.Utf8).str.strip_chars(),
            pl.col("nombre_act").cast(pl.Utf8).str.strip_chars()
        ]).unique().drop_nulls() # .unique() sin subset para considerar la pareja
        
        with self._get_connection() as conn:
            # Definición de Llave Compuesta (Composite Key)
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.table_id} (
                    codigo_act VARCHAR,
                    nombre_act VARCHAR,
                    PRIMARY KEY (codigo_act, nombre_act)
                )
            """)
            
            # El INSERT OR IGNORE evitará duplicados de la pareja exacta
            conn.execute(f"INSERT OR IGNORE INTO {self.table_id} SELECT * FROM catalog_df")