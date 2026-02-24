"""
Processing Stage: ZIP → Warehouse

Extracts data from raw ZIP files, cleans and normalizes it,
and loads it into the DuckDB warehouse.

This module contains the core processing logic extracted from the
original DenueProcessor class, now integrated with MetadataTracker
for comprehensive observability.
"""

import logging
import sys
import zipfile
from pathlib import Path
from typing import Any

import polars as pl

from src_etl.loaders.denue import DataStoreManager
from src_etl.loaders.geography import load_seeds as load_geography_seeds
from src_etl.loaders.scian import load_seeds as load_scian_seeds
from src_etl.utils.denue_schema import (
    COLUMN_ALIASES,
    ENTITY_VALUE_NORMALIZATION,
    ENUM_CATALOGS,
    MASTER_COLUMNS,
    REQUIRED_COLUMNS,
)
from src_metadata.models import JobStatus, TaskStatus
from src_metadata.tracker import MetadataTracker

# Setup logging
log_file = Path("./data/etl_process.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


class ProcessorStage:
    """
    Processes raw DENUE ZIP files into the DuckDB warehouse.

    Handles:
    - ZIP file extraction
    - Data cleaning and normalization
    - Schema validation
    - Warehouse loading
    - Seed catalog bootstrapping
    """

    def __init__(
        self,
        data_root: Path,
        tracker: MetadataTracker,
        output_dir: Path | None = None,
    ):
        """
        Initialize processor stage.

        Args:
            data_root: Root data directory
            tracker: MetadataTracker instance for job/task tracking
            output_dir: Optional output directory (currently unused, for future Parquet output)
        """
        self.data_root = Path(data_root)
        self.tracker = tracker
        self.output_dir = Path(output_dir) if output_dir else None
        self.warehouse_path = self.data_root / "denue_warehouse.db"

        self.datastore = DataStoreManager(self.warehouse_path)
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
        """
        Bootstrap seed catalog tables for referential integrity.

        Delegates to domain-specific loaders:
        - geography: entidades, municipios, localidades
        - scian: actividades_economicas
        """
        seeds_dir = self.data_root / "seeds"

        with self.datastore._get_connection() as conn:
            if force:
                logger.warning("Dropping fact table for integrity constraint reset...")
                conn.execute(f"DROP TABLE IF EXISTS {self.datastore.table_id} CASCADE")

            # Create ENUM types
            for name, values in ENUM_CATALOGS.items():
                type_name = f"{name}_type"
                if not conn.execute(
                    f"SELECT 1 FROM pg_type WHERE typname = '{type_name}'"
                ).fetchone():
                    vals_sql = ", ".join([f"'{v}'" for v in values])
                    conn.execute(f"CREATE TYPE {type_name} AS ENUM ({vals_sql})")

            # Load seed catalogs via loaders
            load_geography_seeds(conn, seeds_dir, force=force)
            load_scian_seeds(conn, seeds_dir, force=force)

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
            csv_name = z.namelist()[0]
            logs["stats"]["csv_file"] = csv_name

            with z.open(csv_name) as f:
                # Try UTF-8 first, fallback to latin1
                try:
                    df = pl.read_csv(f, encoding="utf-8")
                except:
                    f.seek(0)
                    df = pl.read_csv(f, encoding="latin1")

        logs["stats"]["rows_raw"] = df.height

        # Rename columns using aliases
        rename_map = {old: new for old, new in COLUMN_ALIASES.items() if old in df.columns}
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

        # Apply entity normalization
        for col, norm_map in ENTITY_VALUE_NORMALIZATION.items():
            if col in df.columns:
                df = df.with_columns(pl.col(col).replace(norm_map))

        # Normalize text columns
        text_cols = [
            "tipo_vial",
            "tipo_asentamiento",
            "nombre_asentamiento",
            "tipo_vial_1",
            "tipo_vial_2",
            "tipo_vial_3",
        ]
        for col in text_cols:
            if col in df.columns:
                df = df.with_columns(self._normalize_text_col(col))

        # Add period column
        df = df.with_columns(pl.lit(period).alias("period"))

        # Select only needed columns
        df = df.select([c for c in MASTER_COLUMNS if c in df.columns])

        # Generate unique ID
        df = df.with_columns(
            pl.concat_str(
                [
                    pl.col("clee").cast(pl.Utf8),
                    pl.col("nom_estab"),
                    pl.col("raz_social"),
                    pl.col("codigo_act"),
                    pl.col("period"),
                ],
                separator="|",
            )
            .hash()
            .cast(pl.Utf8)
            .alias("id")
        )

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
        Run processing pipeline using MetadataTracker.

        Args:
            force: If True, reprocess all files regardless of status

        Returns:
            True if successful, False otherwise
        """
        if force:
            logger.warning("⚠️ MODO FORCE ACTIVADO: Se re-procesarán todos los archivos en disco.")
            self._bootstrap_seeds(force=True)

        # Create job run
        run_id = self.tracker.create_run(
            job_name="denue_processor",
            job_type="PROCESS",
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
            logger.info("✨ Warehouse actualizado. Nada por procesar.")
            self.tracker.complete_run(run_id, JobStatus.SUCCESS)
            return True

        logger.info(f"⚙️ Iniciando pipeline para {len(tasks)} archivos...")
        session_total_rows = 0
        tasks_succeeded = 0
        tasks_failed = 0

        for idx, task_info in enumerate(tasks, start=1):
            zip_path = Path.cwd() / task_info["source_path"]

            if not zip_path.exists():
                logger.warning(
                    f"   ⚠️ [{idx}/{len(tasks)}] Archivo no encontrado físicamente: {zip_path.name}"
                )
                continue

            # Create process task
            task_id = self.tracker.create_process_task(
                run_id=run_id,
                task_name=zip_path.name,
                source_path=str(zip_path),
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
            f"✨ Proceso terminado. Total sesión: {session_total_rows:,} registros integrados en el Warehouse."
        )
        logger.info(f"   📊 Resumen: {tasks_succeeded} exitosos, {tasks_failed} fallidos")

        return tasks_failed == 0
