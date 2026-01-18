import polars as pl
from scripts.utils.database import BaseWarehouse
from scripts.utils.constants import DB_FACT_COLUMNS

class DataStoreManager(BaseWarehouse):
    def __init__(self, warehouse_path):
        super().__init__(warehouse_path)
        self.table_id = "establecimientos"

    def upsert(self, df: pl.DataFrame):
        """
        Inserta hechos utilizando únicamente las columnas definidas en DB_FACT_COLUMNS.
        Garantiza un Warehouse ligero y normalizado.
        """
        # Filtramos el DataFrame usando la constante que ahora debe contener 'id'
        df_optimized = df.select([
            c for c in DB_FACT_COLUMNS if c in df.columns
        ])

        with self._get_connection() as conn:
            # 1. Crear la tabla si no existe con el esquema de df_optimized
            conn.execute(f"CREATE TABLE IF NOT EXISTS {self.table_id} AS SELECT * FROM df_optimized WHERE 1=0")
            
            # 2. Crear el índice sobre la nueva columna 'id' (UUID lógico)
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_id ON {self.table_id} (id)")
            
            # 3. Inserción de registros nuevos mediante LEFT JOIN usando 'id'
            # Se usa src.id porque 'row_id' ya no existe en el DataFrame
            conn.execute(f"""
                INSERT INTO {self.table_id} 
                SELECT src.* FROM df_optimized src
                LEFT JOIN {self.table_id} tgt ON src.id = tgt.id
                WHERE tgt.id IS NULL
            """)

class EstratoManager(BaseWarehouse):
    def __init__(self, warehouse_path):
        super().__init__(warehouse_path)
        self.table_id = "denue_estratos_personal"

    def sync(self, df: pl.DataFrame):
        """Crea catálogo de personal ocupado."""
        estratos_df = df.select([
            pl.col("per_ocu").cast(pl.Utf8).str.strip_chars()
        ]).unique().drop_nulls()

        with self._get_connection() as conn:
            conn.execute(f"""
                CREATE SEQUENCE IF NOT EXISTS seq_estrato_id;
                CREATE TABLE IF NOT EXISTS {self.table_id} (
                    id_estrato INTEGER PRIMARY KEY DEFAULT nextval('seq_estrato_id'),
                    per_ocu VARCHAR UNIQUE
                )
            """)
            conn.execute(f"INSERT OR IGNORE INTO {self.table_id} (per_ocu) SELECT per_ocu FROM estratos_df")

class RoadTypeManager(BaseWarehouse):
    def __init__(self, warehouse_path):
        super().__init__(warehouse_path)
        self.table_id = "denue_tipos_vialidad"

    def sync(self, df: pl.DataFrame):
        items_df = df.select([
            pl.col("tipo_vial").cast(pl.Utf8).str.strip_chars().str.to_uppercase()
        ]).unique().drop_nulls()

        with self._get_connection() as conn:
            conn.execute(f"""
                CREATE SEQUENCE IF NOT EXISTS seq_{self.table_id}_id;
                CREATE TABLE IF NOT EXISTS {self.table_id} (
                    id_tipo_vial INTEGER PRIMARY KEY DEFAULT nextval('seq_{self.table_id}_id'),
                    tipo_vial VARCHAR UNIQUE
                )
            """)
            conn.execute(f"INSERT OR IGNORE INTO {self.table_id} (tipo_vial) SELECT * FROM items_df")

class SettlementTypeManager(BaseWarehouse):
    def __init__(self, warehouse_path):
        super().__init__(warehouse_path)
        self.table_id = "denue_tipos_asentamiento"

    def sync(self, df: pl.DataFrame):
        items_df = df.select([
            pl.col("tipo_asent").cast(pl.Utf8).str.strip_chars().str.to_uppercase()
        ]).unique().drop_nulls()

        with self._get_connection() as conn:
            conn.execute(f"""
                CREATE SEQUENCE IF NOT EXISTS seq_{self.table_id}_id;
                CREATE TABLE IF NOT EXISTS {self.table_id} (
                    id_tipo_asent INTEGER PRIMARY KEY DEFAULT nextval('seq_{self.table_id}_id'),
                    tipo_asent VARCHAR UNIQUE
                )
            """)
            conn.execute(f"INSERT OR IGNORE INTO {self.table_id} (tipo_asent) SELECT * FROM items_df")

class BusinessUnitTypeManager(BaseWarehouse):
    def __init__(self, warehouse_path):
        super().__init__(warehouse_path)
        self.table_id = "denue_tipos_unidad"

    def sync(self, df: pl.DataFrame):
        items_df = df.select([
            pl.col("tipounieco").cast(pl.Utf8).str.strip_chars().str.to_uppercase()
        ]).unique().drop_nulls()

        with self._get_connection() as conn:
            conn.execute(f"""
                CREATE SEQUENCE IF NOT EXISTS seq_{self.table_id}_id;
                CREATE TABLE IF NOT EXISTS {self.table_id} (
                    id_tipo_unidad INTEGER PRIMARY KEY DEFAULT nextval('seq_{self.table_id}_id'),
                    tipounieco VARCHAR UNIQUE
                )
            """)
            conn.execute(f"INSERT OR IGNORE INTO {self.table_id} (tipounieco) SELECT * FROM items_df")