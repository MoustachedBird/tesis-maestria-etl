"""
Transform SESNSP: CSV consolidado (Bronze) -> Silver DuckDB.

Toma el CSV de incidencia delictiva municipal del corte mas reciente en
data/bronze/sesnsp/ (formato ancho: 12 columnas de mes) y lo carga en
Silver en formato largo:

- `sesnsp_delitos`: catalogo append-only de la jerarquia del delito,
  sincronizado con data/seeds/seed_sesnsp_delitos.parquet.
- `sesnsp_incidencia_municipal`: una fila por municipio x delito x mes,
  `fecha` DATE YYYY-MM-01 y claves geograficas zero-padded compatibles con
  DENUE (cve_ent '09', cve_mun '015').

Cada corte mensual del SESNSP REVISA cifras historicas (las carpetas de un
mes pasado pueden corregirse), por lo que la tabla de hechos se
reconstruye completa en cada ingesta en vez de hacerse upsert incremental.
Todo el unpivot corre dentro de DuckDB (out-of-core); Polars solo se usa
para el catalogo (~98 filas).

Tracking: job `sesnsp_transform`, una process_task por archivo de corte.
Incremental: si el mismo archivo ya se proceso con SUCCESS se omite
(los cortes nuevos traen nombre de archivo nuevo); --force reprocesa.
"""

import re
from datetime import date
from pathlib import Path

from src_etl.core.config import settings
from src_etl.core.logging import get_stage_logger
from src_etl.loaders.geography import load_seeds as load_geography_seeds
from src_etl.loaders.sesnsp import (
    DELITO_KEY_COLUMNS,
    ensure_tables,
    load_delitos_catalog,
    sync_delitos_seed,
    sync_municipios_from_seed,
)
from src_etl.utils.warehouse import connect_configured
from src_metadata.models import JobStatus, TaskStatus
from src_metadata.tracker import MetadataTracker

logger = get_stage_logger(__name__, Path("./data/etl_sesnsp.log"))

MONTH_COLUMNS = [
    ("Enero", 1),
    ("Febrero", 2),
    ("Marzo", 3),
    ("Abril", 4),
    ("Mayo", 5),
    ("Junio", 6),
    ("Julio", 7),
    ("Agosto", 8),
    ("Septiembre", 9),
    ("Octubre", 10),
    ("Noviembre", 11),
    ("Diciembre", 12),
]
"""Columnas de mes del CSV ancho del SESNSP, en orden, con su numero de mes."""

CSV_DELITO_COLUMNS = {
    "Bien jurídico afectado": "bien_juridico",
    "Tipo de delito": "tipo",
    "Subtipo de delito": "subtipo",
    "Modalidad": "modalidad",
}
"""Mapa columna del CSV -> columna del catalogo sesnsp_delitos."""


SPANISH_MONTH_ABBR = {
    "ene": 1,
    "feb": 2,
    "mar": 3,
    "abr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "ago": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dic": 12,
}
"""Abreviaturas de mes en espanol usadas en los nombres de archivo del
SESNSP (IDM_NM_dic25.csv, Municipal-Delitos-..._corte_jun26.csv)."""

_CUT_TOKEN_RE = re.compile(
    r"(" + "|".join(SPANISH_MONTH_ABBR) + r")(\d{4}|\d{2})",
    re.IGNORECASE,
)
"""Token mes-anio en nombres de archivo: 'dic25', 'jun26', 'jun2026'."""


def cut_sort_key(csv_path: Path) -> tuple[date, float]:
    """
    Clave de orden para elegir el CSV del corte mas reciente.

    Extrae TODOS los tokens mes-anio del nombre (un archivo puede traer
    varios: '...dic25_corte_jun26.csv' tiene el fin de datos Y el corte)
    y usa el MAXIMO como fecha del corte; empates (o nombres sin token)
    se resuelven por mtime. Asi un corte manual jun26 le gana a una
    descarga automatica dic25 aunque esta ultima sea mas nueva en disco.

    Returns:
        (fecha_de_corte, mtime); date.min si el nombre no trae token.
    """
    cuts = []
    for month_abbr, year_str in _CUT_TOKEN_RE.findall(csv_path.name):
        year = int(year_str)
        if year < 100:
            year += 2000
        cuts.append(date(year, SPANISH_MONTH_ABBR[month_abbr.lower()], 1))
    return (max(cuts) if cuts else date.min, csv_path.stat().st_mtime)


def detect_csv_encoding(csv_path: Path) -> str:
    """
    Detecta si el CSV es utf-8 o latin-1 (los dos formatos que publica
    SESNSP; los cortes actuales vienen en latin-1).

    Decodifica el primer MB como utf-8 estricto: si falla, es latin-1
    (que por construccion acepta cualquier byte).
    """
    sample = csv_path.open("rb").read(1_048_576)
    try:
        sample.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "latin-1"


class SesnspProcessor:
    """
    Procesa el CSV consolidado del SESNSP hacia las tablas Silver.

    Example:
        >>> processor = SesnspProcessor(data_root=Path("data"))
        >>> processor.run()
    """

    JOB_NAME = "sesnsp_transform"
    """Nombre del job en metadata (job_runs.job_name)."""

    TARGET_TABLE = "sesnsp_incidencia_municipal"
    """Tabla de hechos destino en Silver (tambien usada en process_tasks)."""

    def __init__(
        self,
        data_root: Path,
        tracker: MetadataTracker | None = None,
        silver_db_path: Path | None = None,
    ):
        """
        Args:
            data_root: Raiz de datos (contiene bronze/, silver/, seeds/).
            tracker: MetadataTracker (default: data/metadata.duckdb).
            silver_db_path: Silver DuckDB (default: settings).
        """
        self.data_root = Path(data_root)
        self.bronze_dir = self.data_root / "bronze" / "sesnsp"
        self.seeds_dir = self.data_root / "seeds"
        self.tracker = tracker or MetadataTracker(self.data_root / "metadata.duckdb")
        self.warehouse_path = Path(silver_db_path or settings.SILVER_DUCKDB_PATH)

    def _find_latest_csv(self) -> Path | None:
        """CSV del corte mas reciente en Bronze.

        Acepta tanto el nombre de datos.gob.mx (IDM_NM_*.csv) como el del
        ZIP de SharePoint descargado a mano (Municipal-Delitos*.csv). El
        corte se decide por el token mes-anio mas nuevo del NOMBRE del
        archivo (`cut_sort_key`), no por mtime: una descarga automatica
        del espejo publico (que va cortes atras) no debe ganarle a un
        corte manual mas nuevo solo por ser un archivo mas reciente en
        disco. mtime solo desempata cortes iguales o nombres sin token."""
        candidates = [p for p in self.bronze_dir.glob("*.csv") if p.is_file()]
        if not candidates:
            return None
        return max(candidates, key=cut_sort_key)

    def _already_processed(self, csv_path: Path) -> bool:
        """True si este mismo archivo ya tiene una process_task SUCCESS."""
        with self.tracker._get_connection() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM process_tasks
                WHERE task_name = ? AND target_table = ? AND status = 'SUCCESS'
                LIMIT 1
                """,
                [csv_path.name, self.TARGET_TABLE],
            ).fetchone()
        return row is not None

    def _load_csv(self, conn, csv_path: Path) -> tuple[int, int]:
        """
        Carga el CSV en Silver: catalogo (append-only) + hechos (rebuild).

        Returns:
            (filas_catalogo_nuevas, filas_hechos_cargadas)
        """
        encoding = detect_csv_encoding(csv_path)
        logger.info(f"   Encoding detectado: {encoding}")
        csv_relation = (
            f"read_csv('{csv_path}', encoding='{encoding}', header=true, sample_size=20000)"
        )

        # 1. Catalogo de delitos: distinct del CSV -> seed parquet -> tabla.
        delito_select = ", ".join(
            f'trim("{src}") AS {dst}' for src, dst in CSV_DELITO_COLUMNS.items()
        )
        distinct_delitos = conn.execute(f"SELECT DISTINCT {delito_select} FROM {csv_relation}").pl()
        sync_delitos_seed(distinct_delitos, self.seeds_dir)
        new_delitos = load_delitos_catalog(conn, self.seeds_dir)
        if new_delitos:
            logger.info(f"   Catalogo delitos: +{new_delitos} filas nuevas")

        # 2. Hechos: rebuild completo (cada corte revisa cifras historicas).
        month_casts = ", ".join(
            f'CAST("{name}" AS INTEGER) AS "{name}"' for name, _ in MONTH_COLUMNS
        )
        month_names = ", ".join(f'"{name}"' for name, _ in MONTH_COLUMNS)
        month_map_values = ", ".join(f"('{name}', {num})" for name, num in MONTH_COLUMNS)

        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute(f"DELETE FROM {self.TARGET_TABLE}")
            conn.execute(
                f"""
                INSERT INTO {self.TARGET_TABLE}
                WITH ancho AS (
                    SELECT
                        CAST("Año" AS INTEGER) AS anio,
                        lpad(CAST(CAST("Clave_Ent" AS INTEGER) AS VARCHAR), 2, '0') AS cve_ent,
                        lpad(CAST(CAST("Cve. Municipio" AS INTEGER) % 1000 AS VARCHAR), 3, '0')
                            AS cve_mun,
                        {delito_select},
                        {month_casts}
                    FROM {csv_relation}
                ),
                largo AS (
                    UNPIVOT ancho ON {month_names} INTO NAME mes_nombre VALUE carpetas
                )
                SELECT
                    l.cve_ent,
                    l.cve_mun,
                    d.id AS delito_id,
                    make_date(l.anio, m.mes, 1) AS fecha,
                    l.carpetas
                FROM largo l
                JOIN sesnsp_delitos d USING ({", ".join(DELITO_KEY_COLUMNS)})
                JOIN (VALUES {month_map_values}) AS m(mes_nombre, mes) USING (mes_nombre)
                WHERE l.carpetas IS NOT NULL
                """
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        rows_loaded = conn.execute(f"SELECT COUNT(*) FROM {self.TARGET_TABLE}").fetchone()[0]
        return new_delitos, rows_loaded

    def run(self, force: bool = False) -> bool:
        """
        Ejecuta la ingesta SESNSP (Bronze -> Silver) con tracking.

        Args:
            force: Reprocesar aunque el archivo ya se haya cargado.

        Returns:
            True si no hubo errores (incluye el caso "nada que hacer").
        """
        csv_path = self._find_latest_csv()
        if csv_path is None:
            logger.warning(
                f"⚠️ No hay CSV de SESNSP en {self.bronze_dir}. "
                "Corre primero: extract --source sesnsp"
            )
            return False

        if not force and self._already_processed(csv_path):
            logger.info(f"✨ Corte ya procesado: {csv_path.name}. Nada por transformar.")
            return True

        run_id = self.tracker.create_run(
            job_name=self.JOB_NAME,
            job_type="TRANSFORM",
            trigger_type="MANUAL",
        )
        task_id = self.tracker.create_process_task(
            run_id=run_id,
            task_name=csv_path.name,
            source_path=str(
                csv_path.relative_to(Path.cwd()) if csv_path.is_absolute() else csv_path
            ),
            target_table=self.TARGET_TABLE,
        )
        self.tracker.start_process_task(task_id)
        logger.info(f"📦 Procesando corte SESNSP: {csv_path.name}...")

        conn = connect_configured(self.warehouse_path)
        try:
            # Bootstrap: geografia (si Silver es nuevo) + municipios
            # sinteticos 998/999 (si Silver es previo a este seed).
            load_geography_seeds(conn, self.seeds_dir)
            added_muns = sync_municipios_from_seed(conn, self.seeds_dir)
            if added_muns:
                logger.info(f"   Municipios agregados desde seed: {added_muns}")
            ensure_tables(conn)

            new_delitos, rows_loaded = self._load_csv(conn, csv_path)

            self.tracker.complete_process_task(
                task_id,
                TaskStatus.SUCCESS,
                rows_loaded=rows_loaded,
                file_size_bytes=csv_path.stat().st_size,
            )
            self.tracker.record_lineage(
                run_id=run_id,
                task_id=task_id,
                source_location=str(csv_path),
                target_location=f"{self.warehouse_path}::{self.TARGET_TABLE}",
                source_type="FILE",
                target_type="TABLE",
                bytes_processed=csv_path.stat().st_size,
            )
            self.tracker.complete_run(run_id, JobStatus.SUCCESS)
            logger.info(f"✅ SESNSP cargado: {rows_loaded:,} filas en {self.TARGET_TABLE}")
            return True

        except Exception as e:
            logger.error(f"❌ Error procesando {csv_path.name}: {e}")
            self.tracker.complete_process_task(task_id, TaskStatus.FAILED, error_message=str(e))
            self.tracker.complete_run(run_id, JobStatus.FAILED, error_summary=str(e))
            return False
        finally:
            conn.close()
