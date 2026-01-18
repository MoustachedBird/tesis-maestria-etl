import polars as pl
from scripts.utils.database import BaseWarehouse

class EntityManager(BaseWarehouse):
    def __init__(self, warehouse_path):
        super().__init__(warehouse_path)
        self.table_id = "denue_entidades"

    def sync(self, df: pl.DataFrame):
        """Sincroniza el catálogo de Entidades Federativas."""
        entity_df = df.select([
            pl.col("cve_ent").cast(pl.Utf8).str.strip_chars().str.pad_start(2, "0"),
            pl.col("entidad").cast(pl.Utf8).str.strip_chars()
        ]).unique(subset=["cve_ent"]).drop_nulls()

        with self._get_connection() as conn:
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.table_id} (
                    cve_ent VARCHAR PRIMARY KEY,
                    entidad VARCHAR
                )
            """)
            conn.execute(f"INSERT OR IGNORE INTO {self.table_id} SELECT * FROM entity_df")