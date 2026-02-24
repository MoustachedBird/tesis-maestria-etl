import polars as pl
from src_etl.utils.database import BaseWarehouse
from src_etl.utils.constants import DB_FACT_COLUMNS, ENUM_CATALOGS

class DataStoreManager(BaseWarehouse):
    def __init__(self, warehouse_path):
        super().__init__(warehouse_path)
        self.table_id = "establecimientos"

    def validate_integrity(self, df: pl.DataFrame):
        """Validador Fail-fast en Polars."""
        for col, allowed in ENUM_CATALOGS.items():
            invalid = df.select(col).unique().filter(~pl.col(col).is_in(allowed)).to_series().drop_nulls().to_list()
            if invalid:
                raise ValueError(f"❌ Error de integridad en '{col}': valores {invalid} no permitidos.")

    def upsert(self, df: pl.DataFrame):
        self.validate_integrity(df)

        with self._get_connection() as conn:
            # DDL actualizado con nombres directos
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.table_id} (
                    id VARCHAR PRIMARY KEY,
                    clee VARCHAR, nom_estab VARCHAR, raz_social VARCHAR, codigo_act VARCHAR,
                    tipo_vial tipo_vial_type, 
                    tipo_asent tipo_asent_type,
                    tipounieco tipounieco_type,
                    per_ocu per_ocu_type,
                    nom_vial VARCHAR, tipo_v_e_1 VARCHAR, nom_v_e_1 VARCHAR,
                    tipo_v_e_2 VARCHAR, nom_v_e_2 VARCHAR, tipo_v_e_3 VARCHAR,
                    nom_v_e_3 VARCHAR, numero_ext VARCHAR, letra_ext VARCHAR,
                    edificio VARCHAR, edificio_e VARCHAR, numero_int VARCHAR,
                    letra_int VARCHAR, nomb_asent VARCHAR, nom_cencom VARCHAR,
                    num_local VARCHAR, cod_postal VARCHAR,
                    cve_ent VARCHAR, cve_mun VARCHAR, cve_loc VARCHAR,
                    ageb VARCHAR, manzana VARCHAR, telefono VARCHAR,
                    correoelec VARCHAR, www VARCHAR,
                    latitud DOUBLE, longitud DOUBLE, fecha_alta VARCHAR, period DATE,
                    FOREIGN KEY (cve_ent) REFERENCES denue_entidades(cve_ent),
                    FOREIGN KEY (cve_ent, cve_mun) REFERENCES denue_municipios(cve_ent, cve_mun),
                    FOREIGN KEY (cve_ent, cve_mun, cve_loc) REFERENCES denue_localidades(cve_ent, cve_mun, cve_loc)
                )
            """)

            cols_sql = ", ".join(DB_FACT_COLUMNS)
            # DuckDB ahora encontrará 'tipo_vial' en 'df' porque ya no buscamos 'id_tipo_vial'
            conn.execute(f"INSERT OR IGNORE INTO {self.table_id} ({cols_sql}) SELECT {cols_sql} FROM df")