"""
Writer de establecimientos hacia el warehouse DuckDB (Silver).

Esquema RELACIONAL con FKs reales (decision del usuario 2026-07-17): los
dominios de tipo se guardan como columnas `*_id` INTEGER referenciando los
catalogos `denue_tipos_*` / `denue_estratos_personal`, y `codigo_act` /
geografia llevan FK a sus catalogos. Los CSV de INEGI traen labels: el
INSERT los convierte a ids via JOIN con los catalogos (DOMAIN_ID_COLUMNS).

Costo asumido: cada FK implica lookups/mantenimiento de indice en DuckDB
durante escrituras masivas (documentado en AGENTS.md); a cambio la
integridad referencial la garantiza el motor, no solo la validacion.
"""

import polars as pl

from src_etl.utils.clee_resolution_config import H3_BLOCKING_RES, H3_PARTITION_RES
from src_etl.utils.denue_schema import (
    DB_FACT_COLUMNS,
    DOMAIN_CATALOGS,
    DOMAIN_ID_COLUMNS,
)
from src_etl.utils.warehouse import DuckDBWarehouse, load_h3_extension

ESTABLECIMIENTOS_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    id VARCHAR PRIMARY KEY,
    clee VARCHAR, clee_propagado_radio VARCHAR, clee_propagado_h3 VARCHAR,
    nom_estab VARCHAR, raz_social VARCHAR, codigo_act VARCHAR,
    tipo_vial_id INTEGER,
    tipo_asent_id INTEGER,
    tipounieco_id INTEGER,
    per_ocu_id INTEGER,
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
    FOREIGN KEY (codigo_act) REFERENCES denue_actividades_economicas(codigo_act),
    FOREIGN KEY (tipo_vial_id) REFERENCES denue_tipos_vialidad(id),
    FOREIGN KEY (tipo_asent_id) REFERENCES denue_tipos_asentamiento(id),
    FOREIGN KEY (tipounieco_id) REFERENCES denue_tipos_unidad_economica(id),
    FOREIGN KEY (per_ocu_id) REFERENCES denue_estratos_personal(id),
    FOREIGN KEY (cve_ent) REFERENCES denue_entidades(cve_ent),
    FOREIGN KEY (cve_ent, cve_mun) REFERENCES denue_municipios(cve_ent, cve_mun),
    FOREIGN KEY (cve_ent, cve_mun, cve_loc) REFERENCES denue_localidades(cve_ent, cve_mun, cve_loc)
)
"""
"""DDL canonico de la tabla de hechos (PK + FKs reales). Fuente unica:
lo usan el writer (tablas nuevas), la migracion (rebuild de tablas vacias)
y el script de reorganizacion del Silver historico. Requiere los catalogos
ya creados/sembrados (bootstrap de seeds corre antes)."""


class EstablecimientoWriter(DuckDBWarehouse):
    def __init__(self, warehouse_path):
        super().__init__(warehouse_path)
        self.table_id = "establecimientos"

    def validate_integrity(self, df: pl.DataFrame):
        """Validador fail-fast en Polars contra los dominios de catalogo.

        Corre ANTES del INSERT para que un valor fuera de dominio de un CSV
        nuevo de INEGI produzca un error claro (columna + valores) en vez
        de una violacion de FK criptica a media carga.
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

    def _validate_codigo_act(self, conn, df: pl.DataFrame):
        """Fail-fast de codigo_act contra el catalogo SCIAN (tiene FK real).

        Un codigo nuevo en un CSV (SCIAN evoluciona entre periodos) debe
        detenerse aqui con mensaje accionable: actualizar el seed del
        catalogo de actividades y re-correr.
        """
        if "codigo_act" not in df.columns:
            return
        # "usada" via replacement scan de DuckDB (FROM codigos), no por Python
        codigos = df.select("codigo_act").unique().drop_nulls()  # noqa: F841
        missing = conn.execute(
            "SELECT c.codigo_act FROM codigos c "
            "LEFT JOIN denue_actividades_economicas a USING (codigo_act) "
            "WHERE a.codigo_act IS NULL LIMIT 10"
        ).fetchall()
        if missing:
            raise ValueError(
                f"❌ codigo_act fuera del catalogo SCIAN: {[m[0] for m in missing]} "
                "(actualizar seed de denue_actividades_economicas y re-correr)."
            )

    def upsert(self, df: pl.DataFrame):
        self.validate_integrity(df)

        with self._get_connection() as conn:
            conn.execute(ESTABLECIMIENTOS_DDL.format(table=self.table_id))
            self._validate_codigo_act(conn, df)

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

            # Conversion label -> id: el df trae labels de dominio (CSV);
            # la tabla guarda FKs enteros. LEFT JOIN con cada catalogo
            # (labels NULL -> id NULL; los invalidos ya fallaron en
            # validate_integrity, nunca llegan como id NULL silencioso).
            inner_sql = f"SELECT {', '.join(select_exprs + h3_exprs)} FROM df"
            target_cols, outer_select, joins = [], [], []
            for col in DB_FACT_COLUMNS:
                if col in DOMAIN_ID_COLUMNS:
                    id_col, cat_table, cat_label = DOMAIN_ID_COLUMNS[col]
                    alias = f"cat_{col}"
                    joins.append(f"LEFT JOIN {cat_table} {alias} ON s.{col} = {alias}.{cat_label}")
                    target_cols.append(id_col)
                    outer_select.append(f"{alias}.id AS {id_col}")
                else:
                    target_cols.append(col)
                    outer_select.append(f"s.{col}")
            target_cols += ["h3_cell_9", "h3_cell_5"]
            outer_select += ["s.h3_cell_9", "s.h3_cell_5"]

            conn.execute(
                f"INSERT OR IGNORE INTO {self.table_id} ({', '.join(target_cols)}) "
                f"SELECT {', '.join(outer_select)} FROM ({inner_sql}) s {' '.join(joins)}"
            )
