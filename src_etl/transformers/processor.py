"""
Transform Stage: ZIP → Silver DuckDB

Extracts data from raw ZIP files (Bronze), cleans and normalizes it,
and loads it into the Silver DuckDB warehouse.

This module contains the core transformation logic, integrated with
MetadataTracker for comprehensive observability.
"""

import zipfile
from pathlib import Path
from typing import Any

import polars as pl

from src_etl.core.config import settings
from src_etl.core.logging import get_stage_logger
from src_etl.db.indexes import suspended_indexes
from src_etl.loaders.denue import EstablecimientoWriter
from src_etl.utils.denue_schema import (
    COLUMN_ALIASES,
    DOMAIN_VALUE_ALIASES,
    ENTITY_VALUE_NORMALIZATION,
    MASTER_COLUMNS,
    REQUIRED_COLUMNS,
)
from src_metadata.models import JobStatus, TaskStatus
from src_metadata.tracker import MetadataTracker

# Logging de stage centralizado (sin basicConfig: no toca el root logger)
logger = get_stage_logger(__name__, Path("./data/etl_process.log"))


class BronzeToSilverProcessor:
    """
    Transforms raw DENUE ZIP files (Bronze) into the Silver DuckDB warehouse.

    Handles:
    - ZIP file extraction
    - Data cleaning and normalization
    - Schema validation
    - Silver warehouse loading
    - Seed catalog bootstrapping
    """

    def __init__(
        self,
        data_root: Path,
        tracker: MetadataTracker,
        silver_db_path: Path | None = None,
    ):
        """
        Initialize transform stage.

        Args:
            data_root: Root data directory
            tracker: MetadataTracker instance for job/task tracking
            silver_db_path: Path to Silver DuckDB (default: from settings)
        """
        self.data_root = Path(data_root)
        self.tracker = tracker

        if silver_db_path is not None:
            self.warehouse_path = Path(silver_db_path)
        else:
            self.warehouse_path = settings.SILVER_DUCKDB_PATH  # type: ignore[assignment]

        # Ensure Silver directory exists
        self.warehouse_path.parent.mkdir(parents=True, exist_ok=True)

        self.datastore = EstablecimientoWriter(self.warehouse_path)

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

    def _reset_fact_table(self, force: bool = False):
        """Vacia la tabla de hechos cuando se pide reprocesar todo.

        NO siembra catalogos ni crea tablas: el esquema y los seeds son
        responsabilidad de las migraciones (`make db-migrate`), que es el
        unico lugar donde se cargan. Aqui solo se limpia el contenido para
        que `--force` reprocese los 943 archivos sobre una tabla vacia.

        Se usa DELETE y no DROP a proposito: dropear la tabla la recrearia
        sin los indices que definio la migracion.
        """
        if not force:
            return
        logger.warning("MODO FORCE: vaciando la tabla de hechos...")
        with self.datastore._get_connection() as conn:
            conn.execute(f"DELETE FROM {self.datastore.table_id}")

    def _clean_raw_data(self, zip_path: Path, period: str) -> tuple[pl.DataFrame, dict[str, Any]]:
        """
        Clean and normalize data from a ZIP file.

        Args:
            zip_path: Path to source ZIP file
            period: Data period (e.g., "2024_11")

        Returns:
            Tuple of (cleaned DataFrame, processing logs dict)
        """
        logs = {
            "schema_changes": {"duplicates_renamed": [], "missing_filled": []},
            "stats": {},
        }

        # Extract and read CSV
        with zipfile.ZipFile(zip_path) as z:
            # Prefer 'conjunto_de_datos' CSVs, exclude metadata files
            csv_candidates = [
                f for f in z.namelist() if f.endswith(".csv") and "conjunto_de_datos" in f.lower()
            ]
            if not csv_candidates:
                csv_candidates = [
                    f for f in z.namelist() if f.endswith(".csv") and "metadatos" not in f.lower()
                ]
            if not csv_candidates:
                csv_candidates = z.namelist()[:1]

            csv_name = csv_candidates[0]
            logs["stats"]["csv_file"] = csv_name

            with z.open(csv_name) as f:
                raw_bytes = f.read()

            # Fix INEGI 01_2016 bug: 5 CSVs have a missing newline between
            # the header row and the first data row, causing the header to
            # fuse with the first record and produce 81 columns instead of 41.
            if b'"fecha_alta""' in raw_bytes[:2000]:
                raw_bytes = raw_bytes.replace(b'"fecha_alta""', b'"fecha_alta"\n"', 1)

            # Read all columns as Utf8 (infer_schema_length=0) to match
            # the ORM model where every column is String except latitud/longitud.
            # Three encoding fallbacks: UTF-8 -> Latin-1 -> Sanitized Latin-1
            try:
                df = pl.read_csv(
                    raw_bytes,
                    infer_schema_length=0,
                    ignore_errors=True,
                    encoding="utf-8",
                )
            except Exception:
                try:
                    df = pl.read_csv(
                        raw_bytes,
                        infer_schema_length=0,
                        ignore_errors=True,
                        encoding="latin-1",
                    )
                except Exception:
                    sanitized = raw_bytes.decode("latin-1", errors="replace").encode("utf-8")
                    df = pl.read_csv(
                        sanitized,
                        infer_schema_length=0,
                        ignore_errors=True,
                        encoding="utf-8",
                    )

        logs["stats"]["rows_raw"] = df.height

        # Normalize column headers: lowercase, strip whitespace and BOM
        df = df.rename({c: c.lower().strip().replace("\ufeff", "") for c in df.columns})

        # Handle duplicate column names (e.g., from encoding issues)
        if len(df.columns) != len(set(df.columns)):
            new_cols: list[str] = []
            seen: dict[str, int] = {}
            for c in df.columns:
                count = seen.get(c, 0) + 1
                seen[c] = count
                new_cols.append(f"{c}_{count}" if count > 1 else c)
            df.columns = new_cols

        # Rename columns using aliases (skip if target already exists to avoid conflicts)
        rename_map = {
            old: new
            for old, new in COLUMN_ALIASES.items()
            if old in df.columns and new not in df.columns
        }
        if rename_map:
            df = df.rename(rename_map)
            logs["schema_changes"]["duplicates_renamed"] = list(rename_map.keys())

        # Add missing required columns
        for col in REQUIRED_COLUMNS:
            if col not in df.columns:
                df = df.with_columns(pl.lit(None).alias(col))
                logs["schema_changes"]["missing_filled"].append(col)

        # Drop rows with missing required columns
        initial_height = df.height
        df = df.drop_nulls(subset=REQUIRED_COLUMNS)
        logs["stats"]["rows_dropped_invalid_req"] = initial_height - df.height

        # Nombres de entidad federativa al canonico (DISTRITO FEDERAL ->
        # CIUDAD DE MEXICO, MEXICO -> ESTADO DE MEXICO). El mapa es
        # {valor: canonico} y aplica a UNA columna, `entidad`.
        if "entidad" in df.columns:
            df = df.with_columns(pl.col("entidad").replace(ENTITY_VALUE_NORMALIZATION))

        # Sinonimos de dominio -> valor canonico del catalogo (p.ej. INEGI
        # empezo a usar tipo_asent "OTRO" por "OTRA (ESPECIFICAR)" en el
        # corte 2026-05). Va ANTES de la validacion contra la BD.
        for col, aliases in DOMAIN_VALUE_ALIASES.items():
            if col in df.columns:
                df = df.with_columns(pl.col(col).replace(aliases))

        # Normalize ENUM text columns (names must match post-alias MASTER_COLUMNS)
        text_cols = [
            "tipo_vial",
            "tipo_asent",
            "tipounieco",
            "per_ocu",
            "tipo_v_e_1",
            "tipo_v_e_2",
            "tipo_v_e_3",
        ]
        for col in text_cols:
            if col in df.columns:
                df = df.with_columns(self._normalize_text_col(col))

        # Pad geographic codes to match seed catalogs (e.g., "1" -> "01")
        for col, width in [("cve_ent", 2), ("cve_mun", 3), ("cve_loc", 4)]:
            if col in df.columns:
                df = df.with_columns(
                    pl.col(col).cast(pl.Utf8).str.strip_chars().str.pad_start(width, "0")
                )

        # Coordenadas a Float64: el CSV se lee 100% como Utf8
        # (infer_schema_length=0), pero el modelo/DDL las define DOUBLE y el
        # calculo H3 del INSERT exige tipo numerico (BETWEEN contra
        # literales). strict=False: texto invalido o vacio -> NULL.
        for col in ("latitud", "longitud"):
            if col in df.columns:
                df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))

        # Parse fecha_alta_str -> fecha_alta_date (YYYY-MM format only, others -> NULL)
        if "fecha_alta_str" in df.columns:
            df = df.with_columns(
                pl.col("fecha_alta_str")
                .str.strip_chars()
                .str.to_date("%Y-%m", strict=False)
                .alias("fecha_alta_date")
            )

        # Add period column as DATE (period format: "MM_YYYY" or "YYYY")
        if "_" in period:
            month, year = period.split("_", 1)
            period_date = f"{year}-{month.zfill(2)}-01"
        else:
            period_date = f"{period}-01-01"
        df = df.with_columns(pl.lit(period_date).str.to_date("%Y-%m-%d").alias("period"))

        # Select only needed columns
        df = df.select([c for c in MASTER_COLUMNS if c in df.columns])

        # Generate unique ID (use available columns, with fallbacks for missing ones)
        id_components = ["clee", "nom_estab", "raz_social", "codigo_act", "period"]
        id_exprs = [
            pl.col(c).fill_null(f"no_{c}").cast(pl.Utf8) if c in df.columns else pl.lit(f"no_{c}")
            for c in id_components
        ]

        df = df.with_columns(
            pl.concat_str(id_exprs, separator="|").hash().cast(pl.Utf8).alias("id")
        )

        # Dedupe DENTRO del archivo: INSERT OR IGNORE solo protege contra
        # duplicados vs la tabla, no dentro del mismo batch (DuckDB truena
        # con PK duplicada intra-INSERT; visto en comercio_al_por_menor
        # 03_2017). Filas con los mismos 5 componentes de id son el mismo
        # registro para el pipeline: se conserva la primera.
        pre_dedupe = df.height
        df = df.unique(subset=["id"], keep="first")
        if pre_dedupe != df.height:
            logs["stats"]["rows_dropped_dup_id"] = pre_dedupe - df.height

        logs["stats"]["rows_final"] = df.height
        return df, logs

    def process_zip(self, zip_path: Path, period: str) -> tuple[str, str, int]:
        """
        Process a single ZIP file.

        Args:
            zip_path: Path to ZIP file
            period: Data period

        Returns:
            Tuple of (status, message, row_count)
        """
        try:
            df, logs = self._clean_raw_data(zip_path, period)
            self.datastore.upsert(df)

            # Build message
            msg = f"Exito: {df.height} registros."
            if renamed := logs["schema_changes"].get("duplicates_renamed"):
                msg += f" | Renombrados: {', '.join(renamed)}"
            if dropped := logs["stats"].get("rows_dropped_invalid_req"):
                msg += f" | Descartados: {dropped} filas por columnas requeridas vacías"

            return "DONE", msg, df.height

        except Exception as e:
            error_msg = str(e)
            return "ERROR", f"Error en procesamiento: {error_msg}", 0

    def run(self, force: bool = False) -> bool:
        """
        Run transform pipeline using MetadataTracker.

        Processes Bronze ZIPs into Silver DuckDB warehouse.

        Args:
            force: If True, reprocess all files regardless of status

        Returns:
            True if successful, False otherwise
        """
        if force:
            logger.warning("⚠️ MODO FORCE ACTIVADO: Se re-procesarán todos los archivos en disco.")
            self._reset_fact_table(force=True)

        # Create job run
        run_id = self.tracker.create_run(
            job_name="denue_transform",
            job_type="TRANSFORM",
            trigger_type="MANUAL",
        )

        # Get pending tasks
        tasks = self.tracker.get_pending_process_tasks(include_failed=True) if not force else []

        # If force mode, get all successful downloads
        if force:
            with self.tracker._get_connection() as conn:
                result = conn.execute("""
                    SELECT target_path as source_path, period, task_id as download_task_id
                    FROM download_tasks
                    WHERE status = 'SUCCESS'
                    ORDER BY period DESC
                """).fetchall()
                tasks = [
                    {"source_path": r[0], "period": r[1], "download_task_id": r[2]} for r in result
                ]

        if not tasks:
            logger.info("✨ Silver actualizado. Nada por transformar.")
            self.tracker.complete_run(run_id, JobStatus.SUCCESS)
            return True

        logger.info(f"⚙️ Iniciando pipeline para {len(tasks)} archivos...")
        session_total_rows = 0
        tasks_succeeded = 0
        tasks_failed = 0

        # Los indices de la tabla de hechos se suspenden mientras dura la
        # carga y se restituyen al terminar (tambien si falla): mantener
        # ART fila por fila sobre decenas de millones de INSERTs es ordenes
        # de magnitud mas lento. No hay paso manual que recordar.
        with self.datastore._get_connection() as index_conn, suspended_indexes(index_conn):
            for idx, task_info in enumerate(tasks, start=1):
                zip_path = Path.cwd() / task_info["source_path"]

                if not zip_path.exists():
                    logger.warning(
                        f"   ⚠️ [{idx}/{len(tasks)}] Archivo no encontrado físicamente: {zip_path.name}"
                    )
                    continue

                # Create process task (use relative path to match download_tasks.target_path for JOIN)
                task_id = self.tracker.create_process_task(
                    run_id=run_id,
                    task_name=zip_path.name,
                    source_path=task_info["source_path"],
                    period=task_info.get("period"),
                    target_table="denue_establecimientos",
                )

                logger.info(
                    f"📦 [{idx}/{len(tasks)}] Procesando: {zip_path.name} (Periodo: {task_info.get('period')})..."
                )

                self.tracker.start_process_task(task_id)

                # Process the file
                status, message, row_count = self.process_zip(zip_path, task_info.get("period", ""))

                # Update task
                if status == "DONE":
                    self.tracker.complete_process_task(
                        task_id=task_id,
                        status=TaskStatus.SUCCESS,
                        rows_loaded=row_count,
                        file_size_bytes=zip_path.stat().st_size if zip_path.exists() else None,
                    )
                    session_total_rows += row_count
                    tasks_succeeded += 1
                    logger.info(f"   ✅ Éxito: {row_count:,} registros integrados.")
                else:
                    self.tracker.complete_process_task(
                        task_id=task_id,
                        status=TaskStatus.FAILED,
                        error_message=message,
                    )
                    tasks_failed += 1
                    logger.error(f"   ❌ Fallo en archivo {zip_path.name}: {message}")

            # Checkpoint warehouse
            with self.datastore._get_connection() as conn:
                conn.execute("CHECKPOINT;")

        # Complete job run
        final_status = JobStatus.SUCCESS if tasks_failed == 0 else JobStatus.PARTIAL
        self.tracker.complete_run(run_id, final_status)

        logger.info(
            f"✨ Transform terminado. Total sesión: {session_total_rows:,} registros integrados en Silver."
        )
        logger.info(f"   📊 Resumen: {tasks_succeeded} exitosos, {tasks_failed} fallidos")

        return tasks_failed == 0
