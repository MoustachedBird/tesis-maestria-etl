import polars as pl
from src_etl.utils.database import BaseWarehouse

class StateManager(BaseWarehouse):
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

class MunicipalityManager(BaseWarehouse):
    def __init__(self, warehouse_path):
        super().__init__(warehouse_path)
        self.table_id = "denue_municipios"

    def sync(self, df: pl.DataFrame):
        """Sincroniza el catálogo de Municipios con llave compuesta."""
        # Se requiere cve_ent para asegurar unicidad nacional de la clave municipal
        mun_df = df.select([
            pl.col("cve_ent").cast(pl.Utf8).str.strip_chars().str.pad_start(2, "0"),
            pl.col("cve_mun").cast(pl.Utf8).str.strip_chars().str.pad_start(3, "0"),
            pl.col("municipio").cast(pl.Utf8).str.strip_chars().str.to_uppercase()
        ]).unique(subset=["cve_ent", "cve_mun"]).drop_nulls()

        with self._get_connection() as conn:
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.table_id} (
                    cve_ent VARCHAR,
                    cve_mun VARCHAR,
                    municipio VARCHAR,
                    PRIMARY KEY (cve_ent, cve_mun)
                )
            """)
            conn.execute(f"INSERT OR IGNORE INTO {self.table_id} SELECT * FROM mun_df")

class LocalityManager(BaseWarehouse):
    def __init__(self, warehouse_path):
        super().__init__(warehouse_path)
        self.table_id = "denue_localidades"

    def sync(self, df: pl.DataFrame):
        """Sincroniza el catálogo de Localidades con jerarquía completa."""
        loc_df = df.select([
            pl.col("cve_ent").cast(pl.Utf8).str.strip_chars().str.pad_start(2, "0"),
            pl.col("cve_mun").cast(pl.Utf8).str.strip_chars().str.pad_start(3, "0"),
            pl.col("cve_loc").cast(pl.Utf8).str.strip_chars().str.pad_start(4, "0"),
            pl.col("localidad").cast(pl.Utf8).str.strip_chars().str.to_uppercase()
        ]).unique(subset=["cve_ent", "cve_mun", "cve_loc"]).drop_nulls()

        with self._get_connection() as conn:
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.table_id} (
                    cve_ent VARCHAR,
                    cve_mun VARCHAR,
                    cve_loc VARCHAR,
                    localidad VARCHAR,
                    PRIMARY KEY (cve_ent, cve_mun, cve_loc)
                )
            """)
            conn.execute(f"INSERT OR IGNORE INTO {self.table_id} SELECT * FROM loc_df")