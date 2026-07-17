import polars as pl

from src_etl.utils.clee_resolution_config import H3_BLOCKING_RES, H3_PARTITION_RES
from src_etl.utils.denue_schema import DB_FACT_COLUMNS, DOMAIN_CATALOGS
from src_etl.utils.warehouse import DuckDBWarehouse, load_h3_extension


class EstablecimientoWriter(DuckDBWarehouse):
    def __init__(self, warehouse_path):
        super().__init__(warehouse_path)
        self.table_id = "establecimientos"

    def validate_integrity(self, df: pl.DataFrame):
        """Validador fail-fast en Polars contra los dominios de catalogo.

        Es la garantia de integridad referencial de las columnas de dominio
        (tipo_vial/tipo_asent/tipounieco/per_ocu): se valida ANTES de
        insertar en vez de con FKs fisicos sobre la tabla de hechos (cada FK
        en DuckDB crea un indice ART implicito sobre 77M filas -- costo de
        escritura medido ~1000x en este repo; ver AGENTS.md).
        """
        for col, allowed in DOMAIN_CATALOGS.items():
            if col not in df.columns:
                continue
            invalid = (
                df.select(col)
                .unique()
                .filter(~pl.col(col).is_in(allowed))
                .to_series()
                .drop_nulls()
                .to_list()
            )
            if invalid:
                raise ValueError(
                    f"❌ Error de integridad en '{col}': valores {invalid} no permitidos."
                )

    def upsert(self, df: pl.DataFrame):
        self.validate_integrity(df)

        with self._get_connection() as conn:
            # Columnas de dominio como VARCHAR: su dominio vive en los
            # catalogos denue_tipos_* / denue_estratos_personal (sembrados
            # de los mismos enums que valida validate_integrity), no en
            # tipos ENUM de DuckDB. Sin FK fisico hacia esos catalogos a
            # proposito (indice ART implicito por FK = escrituras masivas
            # lentas); la integridad la da validate_integrity() pre-insert.
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.table_id} (
                    id VARCHAR PRIMARY KEY,
                    clee VARCHAR, clee_propagado_radio VARCHAR, clee_propagado_h3 VARCHAR,
                    nom_estab VARCHAR, raz_social VARCHAR, codigo_act VARCHAR,
                    tipo_vial VARCHAR,
                    tipo_asent VARCHAR,
                    tipounieco VARCHAR,
                    per_ocu VARCHAR,
                    nom_vial VARCHAR, tipo_v_e_1 VARCHAR, nom_v_e_1 VARCHAR,
                    tipo_v_e_2 VARCHAR, nom_v_e_2 VARCHAR, tipo_v_e_3 VARCHAR,
                    nom_v_e_3 VARCHAR, numero_ext VARCHAR, letra_ext VARCHAR,
                    edificio VARCHAR, edificio_e VARCHAR, numero_int VARCHAR,
                    letra_int VARCHAR, nomb_asent VARCHAR, nom_cencom VARCHAR,
                    num_local VARCHAR, cod_postal VARCHAR,
                    cve_ent VARCHAR, cve_mun VARCHAR, cve_loc VARCHAR,
                    ageb VARCHAR, manzana VARCHAR, telefono VARCHAR,
                    correoelec VARCHAR, www VARCHAR,
                    latitud DOUBLE, longitud DOUBLE,
                    fecha_alta_str VARCHAR, fecha_alta_date DATE, period DATE NOT NULL,
                    h3_cell_9 BIGINT, h3_cell_5 BIGINT,
                    FOREIGN KEY (cve_ent) REFERENCES denue_entidades(cve_ent),
                    FOREIGN KEY (cve_ent, cve_mun) REFERENCES denue_municipios(cve_ent, cve_mun),
                    FOREIGN KEY (cve_ent, cve_mun, cve_loc) REFERENCES denue_localidades(cve_ent, cve_mun, cve_loc)
                )
            """)

            # Build column list and SELECT expressions, using NULL for missing columns
            select_exprs = [
                col if col in df.columns else f"NULL AS {col}" for col in DB_FACT_COLUMNS
            ]

            # Celdas H3 calculadas EN el INSERT (vectorizado por DuckDB):
            # las filas nacen completas y el stage h3-cells queda solo como
            # red de seguridad incremental. Evita el write-path lento de
            # UPDATE (sink single-thread + mantenimiento de indice ART).
            # h3_cell_5 = padre de h3_cell_9 (la jerarquia H3 no anida
            # perfecto; el padre garantiza co-particion en Fase 4).
            if "latitud" in df.columns and "longitud" in df.columns:
                load_h3_extension(conn)
                coords_ok = (
                    "latitud BETWEEN -90 AND 90 AND longitud BETWEEN -180 AND 180"
                )
                cell9 = f"h3_latlng_to_cell(latitud, longitud, {H3_BLOCKING_RES})"
                h3_exprs = [
                    f"CASE WHEN {coords_ok} THEN CAST({cell9} AS BIGINT) END AS h3_cell_9",
                    f"CASE WHEN {coords_ok} THEN CAST(h3_cell_to_parent({cell9}, "
                    f"{H3_PARTITION_RES}) AS BIGINT) END AS h3_cell_5",
                ]
            else:
                h3_exprs = ["NULL AS h3_cell_9", "NULL AS h3_cell_5"]

            cols_sql = ", ".join(DB_FACT_COLUMNS + ["h3_cell_9", "h3_cell_5"])
            select_sql = ", ".join(select_exprs + h3_exprs)
            conn.execute(
                f"INSERT OR IGNORE INTO {self.table_id} ({cols_sql}) SELECT {select_sql} FROM df"
            )
