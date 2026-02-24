import logging
import sys
import time
import zipfile
from pathlib import Path

import polars as pl

from src_etl.loaders.denue import DataStoreManager
from src_etl.reports.reporter import ReportManager
from src_etl.utils.denue_schema import (
    COLUMN_ALIASES,
    ENTITY_VALUE_NORMALIZATION,
    ENUM_CATALOGS,
    MASTER_COLUMNS,
    REQUIRED_COLUMNS,
)

log_file = Path("./data/etl_process.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


class DenueProcessor:
    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self.warehouse_path = self.data_root / "denue_warehouse.db"

        self.datastore = DataStoreManager(self.warehouse_path)
        self.report_mgr = ReportManager(self.data_root)

        self._bootstrap_seeds()

    def _normalize_text_col(self, col_name: str):
        """
        Normalización estricta basada en el catálogo oficial del INEGI.

        Referencia oficial:
        - Norma Técnica sobre Domicilios Geográficos (DOF 22/11/2024)
        - Censo de Alojamientos de Asistencia Social (INEGI)

        El catálogo oficial incluye "Otro" para valores no previstos.
        """
        return (
            pl.col(col_name)
            .cast(pl.Utf8)
            .str.to_uppercase()
            .str.strip_chars()
            # ==========================================
            # PASO 0: ESTANDARIZACIÓN DE CARACTERES (ANTES de los reemplazos)
            # ==========================================
            .str.replace_all(r"[ÁÀÄÂÃ]", "A")
            .str.replace_all(r"[ÉÈËÊ]", "E")
            .str.replace_all(r"[ÍÌÏÎĨ]", "I")
            .str.replace_all(r"[ÓÒÖÔÕ]", "O")
            .str.replace_all(r"[ÚÙÜÛ]", "U")
            .str.replace_all(r"Ñ", "N")
            # ELIMINAR BASURA AQUÍ: Así "RANCHERA." o "RANCHERA\x00" se vuelven "RANCHERA"
            .str.replace_all(r"[^A-Z0-9\s\(\)]", "")
            # ==========================================
            # PASO 1: CORRECCIÓN DE TRUNCAMIENTOS Y CATÁLOGO
            # ==========================================
            .str.replace_all(r"\bCALLEJEN\b", "CALLEJON")
            .str.replace_all(r"\bPROLONGACIEN\b", "PROLONGACION")
            .str.replace_all(r"\bCONTINUACIEN\b", "CONTINUACION")
            .str.replace_all(r"\bCIRCUNVALACIEN\b", "CIRCUNVALACION")
            .str.replace_all(r"\bAMPLIACIEN\b", "AMPLIACION")
            .str.replace_all(r"\bPERIFRIEN\b", "PERIFERICO")
            .str.replace_all(r"\bCOLONI\b", "COLONIA")
            .str.replace_all(r"\bCOLONIAA\b", "COLONIA")
            .str.replace_all(r"\bSECCIEN\b", "SECCION")
            .str.replace_all(r"\bFRACCIEN\b", "FRACCION")
            .str.replace_all(r"\bRANCHEROA\b", "RANCHERIA")
            .str.replace_all(r"\bRANCHERA\b", "RANCHERIA")  # Ahora sí hará match
            # ==========================================
            # PASO 2: TYPOS Y MAPEOS
            # ==========================================
            .str.replace_all(r"\bAEREOPUERTO\b", "AEROPUERTO")
            .str.replace_all(r"\bNUNGUNO\b", "NINGUNO")
            .str.replace_all(r"\bTERRACERA\b", "TERRACERIA")
            .str.replace_all(r"\bZONA COMERCIAL\b", "OTRA (ESPECIFICAR)")
            .str.replace_all(r"\bAREA RURAL\b", "OTRA (ESPECIFICAR)")
            .str.replace_all(r"\bLOCALIDAD URBANA\b", "OTRA (ESPECIFICAR)")
            .str.replace_all(r"\bSEGUNDA PRIVADA\b", "PRIVADA")
            .str.replace_all(r"^HIDALGO$", "OTRA (ESPECIFICAR)")
            .str.replace_all(r"OTR[OA]\s*\(?\s*ESPECIF.*", "OTRA (ESPECIFICAR)")
            # ==========================================
            # PASO 3: CIERRE Y NULOS
            # ==========================================
            .fill_null("NINGUNO")
            .map_elements(
                lambda x: "NINGUNO" if x in ["", " ", None, "NAN", "NULL"] else x,
                return_dtype=pl.Utf8,
            )
            .str.strip_chars()
        )

    def _bootstrap_seeds(self, force: bool = False):
        """Carga semillas y asegura integridad referencial con validación estricta."""
        seeds = {
            "denue_entidades": "seed_entidades.parquet",
            "denue_municipios": "seed_municipios.parquet",
            "denue_localidades": "seed_localidades.parquet",
            "denue_actividades_economicas": "seed_actividades.parquet",
        }

        with self.datastore._get_connection() as conn:
            if force:
                logger.warning("🗑️ Eliminando tabla de hechos por restricción de integridad...")
                conn.execute(f"DROP TABLE IF EXISTS {self.datastore.table_id} CASCADE")

            # Creación de tipos ENUM
            for name, values in ENUM_CATALOGS.items():
                type_name = f"{name}_type"
                if not conn.execute(
                    f"SELECT 1 FROM pg_type WHERE typname = '{type_name}'"
                ).fetchone():
                    vals_sql = ", ".join([f"'{v}'" for v in values])
                    conn.execute(f"CREATE TYPE {type_name} AS ENUM ({vals_sql})")

            # Validación y Carga de Dimensiones
            for table, file_name in seeds.items():
                seed_path = self.data_root / "seeds" / file_name

                if not seed_path.exists():
                    logger.error(f"❌ Archivo no encontrado en: {seed_path}")
                    raise FileNotFoundError(
                        f"Error Crítico: La semilla '{file_name}' es obligatoria para la integridad referencial."
                    )

                try:
                    row_count = pl.scan_parquet(seed_path).select(pl.len()).collect().item()
                    if row_count == 0:
                        raise ValueError(
                            f"Error Crítico: El archivo '{file_name}' tiene 0 registros."
                        )
                    logger.info(f"✅ Semilla validada: {file_name} ({row_count:,} registros).")
                except Exception as e:
                    raise RuntimeError(f"Error al leer la semilla {file_name}: {str(e)}")

                if force:
                    conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

                if not conn.execute(
                    f"SELECT 1 FROM information_schema.tables WHERE table_name = '{table}'"
                ).fetchone():
                    if table == "denue_entidades":
                        conn.execute(
                            f"CREATE TABLE {table} (cve_ent VARCHAR PRIMARY KEY, entidad VARCHAR)"
                        )
                    elif table == "denue_municipios":
                        conn.execute(
                            f"CREATE TABLE {table} (cve_ent VARCHAR, cve_mun VARCHAR, municipio VARCHAR, PRIMARY KEY (cve_ent, cve_mun))"
                        )
                    elif table == "denue_localidades":
                        conn.execute(
                            f"CREATE TABLE {table} (cve_ent VARCHAR, cve_mun VARCHAR, cve_loc VARCHAR, localidad VARCHAR, PRIMARY KEY (cve_ent, cve_mun, cve_loc))"
                        )
                    elif table == "denue_actividades_economicas":
                        conn.execute(
                            f"CREATE TABLE {table} (codigo_act VARCHAR, nombre_act VARCHAR, PRIMARY KEY (codigo_act, nombre_act))"
                        )

                    conn.execute(f"INSERT INTO {table} SELECT * FROM read_parquet('{seed_path}')")

            conn.execute("CHECKPOINT;")

    def _clean_raw_data(self, zip_path: Path, period: str):
        """
        Motor de limpieza robusto con soporte para periodos anuales (2015),
        detección de columnas duplicadas, filtrado estricto y normalización de catálogos.
        """
        logs = {"steps": {}, "stats": {"period": period}, "schema_changes": {}}

        # Manejo de periodos con formato solo año (ej. "2015")
        # Se normaliza a "01_YYYY" para cumplir con el formato "%m_%Y"
        clean_period = f"01_{period}" if len(period) == 4 and period.isdigit() else period

        with zipfile.ZipFile(zip_path, "r") as z:
            csv_candidates = [
                f for f in z.namelist() if f.endswith(".csv") and "conjunto_de_datos" in f.lower()
            ]
            if not csv_candidates:
                csv_candidates = [
                    f for f in z.namelist() if f.endswith(".csv") and "metadatos" not in f.lower()
                ]

            csv_name = csv_candidates[0]
            with z.open(csv_name) as f:
                raw_bytes = f.read()
                try:
                    df = pl.read_csv(
                        raw_bytes,
                        infer_schema_length=0,
                        ignore_errors=True,
                        encoding="utf-8",
                    )
                    logs["steps"]["encoding"] = "UTF-8"
                except Exception:
                    try:
                        df = pl.read_csv(
                            raw_bytes,
                            infer_schema_length=0,
                            ignore_errors=True,
                            encoding="latin-1",
                        )
                        logs["steps"]["encoding"] = "LATIN-1 (Fallback)"
                    except Exception:
                        sanitized_data = raw_bytes.decode("latin-1", errors="replace").encode(
                            "utf-8"
                        )
                        df = pl.read_csv(
                            sanitized_data,
                            infer_schema_length=0,
                            ignore_errors=True,
                            encoding="utf-8",
                        )
                        logs["steps"]["encoding"] = "LATIN-1 (Sanitized)"

        # 1. Normalización de cabeceras y Manejo de Duplicados
        df = df.rename({c: c.lower().strip().replace("\ufeff", "") for c in df.columns})

        # Lógica de des-duplicación integrada
        if len(df.columns) != len(set(df.columns)):
            new_cols = []
            seen = {}
            renamed_list = []
            for c in df.columns:
                count = seen.get(c, 0) + 1
                seen[c] = count
                if count > 1:
                    new_name = f"{c}_{count}"
                    new_cols.append(new_name)
                    renamed_list.append(f"{c}->{new_name}")
                else:
                    new_cols.append(c)
            df.columns = new_cols
            logs["schema_changes"]["duplicates_renamed"] = renamed_list

        # 2. Aliases y Validación de estructura
        applied_aliases = {
            k: v for k, v in COLUMN_ALIASES.items() if k in df.columns and v not in df.columns
        }
        if applied_aliases:
            df = df.rename(applied_aliases)
            logs["schema_changes"]["aliases_applied"] = list(applied_aliases.keys())

        if missing_req_cols := [c for c in REQUIRED_COLUMNS if c not in df.columns]:
            raise ValueError(f"Required columns missing in CSV: {missing_req_cols}")

        # 4. Filtrado estricto
        initial_count = df.height
        required_mask = pl.all_horizontal(
            [
                pl.col(c).is_not_null() & (pl.col(c).cast(pl.Utf8).str.strip_chars() != "")
                for c in REQUIRED_COLUMNS
            ]
        )

        df = df.filter(required_mask)
        logs["stats"]["rows_dropped_invalid_req"] = initial_count - df.height

        # 5. Selección de MASTER_COLUMNS
        selection = [
            pl.col(c) if c in df.columns else pl.lit(None).alias(c) for c in MASTER_COLUMNS
        ]
        df = df.select(selection)

        # 6. Transformación de Tipos y Normalización de valores
        # Nota: Aquí usamos tus funciones de normalización para las columnas de catálogo
        df = df.with_columns(
            [
                pl.col("cve_ent").cast(pl.Utf8).str.strip_chars().str.pad_start(2, "0"),
                pl.col("cve_mun").cast(pl.Utf8).str.strip_chars().str.pad_start(3, "0"),
                pl.col("cve_loc").cast(pl.Utf8).str.strip_chars().str.pad_start(4, "0"),
                pl.col("latitud").cast(pl.Float64, strict=False),
                pl.col("longitud").cast(pl.Float64, strict=False),
                # Uso de clean_period para evitar errores de parseo en años como 2015
                pl.lit(clean_period).str.to_date(format="%m_%Y").alias("period"),
                pl.col("entidad")
                .cast(pl.Utf8)
                .str.to_uppercase()
                .replace(
                    ENTITY_VALUE_NORMALIZATION,
                    default=pl.col("entidad").str.to_uppercase(),
                ),
                pl.col("municipio").str.to_uppercase().str.strip_chars(),
                pl.col("localidad").str.to_uppercase().str.strip_chars(),
                self._normalize_text_col("tipo_vial"),
                self._normalize_text_col("tipo_asent"),
                self._normalize_text_col("tipounieco"),
                self._normalize_text_col("per_ocu"),
            ]
        )

        # 7. Generación de ID Único vía Hash (Fingerprinting)
        df = df.with_columns(
            pl.concat_str(
                [
                    pl.col("id").fill_null("no_id").cast(pl.Utf8).str.strip_chars(),
                    pl.col("nom_estab").fill_null("no_name").str.to_uppercase(),
                    pl.col("codigo_act").fill_null("000000"),
                    pl.col("period").dt.strftime("%Y%m"),
                ],
                separator="|",
            )
            .hash()
            .cast(pl.Utf8)
            .alias("id")
        )

        logs["stats"]["rows_final"] = df.height
        return df, logs

    def process_zip(self, zip_path: Path, period: str):
        """Retorna estatus, mensaje (incluyendo logs de limpieza) y conteo."""
        try:
            # DESEMPAQUETAR LA TUPLA: df (DataFrame) y logs (dict)
            df, logs = self._clean_raw_data(zip_path, period)

            # Ahora enviamos solo el DataFrame al upsert
            self.datastore.upsert(df)

            # Construcción del mensaje dinámico para el reporte
            msg = f"Exito: {df.height} registros."
            if renamed := logs["schema_changes"].get("duplicates_renamed"):
                msg += f" | Renombrados: {', '.join(renamed)}"
            if dropped := logs["stats"].get("rows_dropped_invalid_req"):
                msg += f" | Descartados: {dropped} filas por columnas requeridas vacías"

            return "DONE", msg, df.height

        except Exception as e:
            error_msg = str(e)
            return "ERROR", f"Error en procesamiento: {error_msg}", 0

    def run_pipeline(self, force: bool = False):
        """Orquestador con log detallado y métricas de sesión."""
        if force:
            logger.warning("⚠️ MODO FORCE ACTIVADO: Se re-procesarán todos los archivos en disco.")
            self._bootstrap_seeds(force=True)
            time.sleep(3)
            tasks = self.report_mgr.download_report.get_raw_tasks(force=True)
        else:
            tasks = self.report_mgr.download_report.get_raw_tasks(retry_errors=True)

        if not tasks:
            logger.info("✨ Warehouse actualizado. Nada por procesar.")
            return

        logger.info(f"⚙️ Iniciando pipeline para {len(tasks)} archivos...")
        session_total_rows = 0

        for idx, task in enumerate(tasks, start=1):
            zip_p = Path.cwd() / task["file_path"]

            if not zip_p.exists():
                logger.warning(
                    f"   ⚠️ [{idx}/{len(tasks)}] Archivo no encontrado físicamente: {zip_p.name}"
                )
                continue

            logger.info(
                f"📦 [{idx}/{len(tasks)}] Procesando: {zip_p.name} (Periodo: {task['period']})..."
            )

            status, message, row_count = self.process_zip(zip_p, task["period"])

            self.report_mgr.download_report.update_raw_status_batch(
                [
                    {
                        "file_path": task["file_path"],
                        "process_status": status,
                        "process_msg": message,
                    }
                ]
            )

            if status == "DONE":
                session_total_rows += row_count
                logger.info(f"   ✅ Éxito: {row_count:,} registros integrados.")
            else:
                logger.error(f"   ❌ Fallo en archivo {zip_p.name}: {message}")

        with self.datastore._get_connection() as conn:
            conn.execute("CHECKPOINT;")

        logger.info(
            f"✨ Proceso terminado. Total sesión: {session_total_rows:,} registros integrados en el Warehouse."
        )
