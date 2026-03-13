"""
Normalize-Dates Stage: fecha_alta_str -> fecha_alta_date in Silver DuckDB

Parses the raw fecha_alta_str column (6 known formats) into a proper
DATE column (fecha_alta_date) with format YYYY-MM-01. Operates directly
on the Silver DuckDB via UPDATE statements, tracked per-period in the
metadata database.

Pipeline position: ingest -> normalize-dates -> clee-resolution
"""

import logging
import sys
from datetime import date
from pathlib import Path

import duckdb

from src_metadata.models import JobStatus, TaskStatus
from src_metadata.tracker import MetadataTracker

# Setup logging
log_file = Path("./data/etl_normalize.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Spanish month names -> two-digit month numbers
SPANISH_MONTHS: dict[str, str] = {
    "ENERO": "01",
    "FEBRERO": "02",
    "MARZO": "03",
    "ABRIL": "04",
    "MAYO": "05",
    "JUNIO": "06",
    "JULIO": "07",
    "AGOSTO": "08",
    "SEPTIEMBRE": "09",
    "OCTUBRE": "10",
    "NOVIEMBRE": "11",
    "DICIEMBRE": "12",
}


def _build_spanish_month_case() -> str:
    """
    Build a SQL CASE expression that maps Spanish month names to numbers.

    Returns:
        SQL fragment: CASE UPPER(...) WHEN 'ENERO' THEN '01' ... END
    """
    clauses = "\n".join(
        f"                WHEN '{name}' THEN '{num}'" for name, num in SPANISH_MONTHS.items()
    )
    return f"""CASE UPPER(SPLIT_PART(TRIM(fecha_alta_str), ' ', 1))
{clauses}
                ELSE NULL
            END"""


# Pre-built SQL CASE for Spanish months
_SPANISH_MONTH_CASE_SQL = _build_spanish_month_case()

# The core UPDATE SQL template.
# Handles 6 known fecha_alta_str formats:
#   1. YYYY-MM        (most common, ~68M rows)
#   2. YYYY-MM<space> (trailing space, 15K rows)
#   3. YYYY MM        (space separator, 7K rows)
#   4. YYYY_MM        (underscore, 10 rows)
#   5. MES_ESPANOL ANNO (e.g., "JULIO 2010", ~14.5M rows)
#   6. DD/MM/YYYY     (12 rows)
#
# All formats are normalized to DATE YYYY-MM-01.
_UPDATE_SQL_TEMPLATE = """
UPDATE establecimientos
SET fecha_alta_date = CASE
    -- Format 5: MES_ESPANOL ANNO (e.g., "JULIO 2010", "FEBRERO 2015")
    WHEN regexp_matches(TRIM(fecha_alta_str), '^[A-Z]+ \\d{{4}}$')
    THEN CAST(
        SPLIT_PART(TRIM(fecha_alta_str), ' ', 2) || '-' ||
        {spanish_month_case} || '-01'
        AS DATE)

    -- Format 6: DD/MM/YYYY (e.g., "06/10/2023")
    WHEN regexp_matches(TRIM(fecha_alta_str), '^\\d{{2}}/\\d{{2}}/\\d{{4}}$')
    THEN CAST(
        SPLIT_PART(TRIM(fecha_alta_str), '/', 3) || '-' ||
        SPLIT_PART(TRIM(fecha_alta_str), '/', 2) || '-01'
        AS DATE)

    -- Formats 1-4: YYYY-MM variants (trim first, then normalize separators)
    WHEN regexp_matches(
        REPLACE(REPLACE(TRIM(fecha_alta_str), '_', '-'), ' ', '-'),
        '^\\d{{4}}-\\d{{2}}$')
    THEN CAST(
        REPLACE(REPLACE(TRIM(fecha_alta_str), '_', '-'), ' ', '-') || '-01'
        AS DATE)

    ELSE NULL
END
WHERE period = ?
  AND fecha_alta_str IS NOT NULL
  {force_clause}
"""


class DateNormalizer:
    """
    Normalizes fecha_alta_str -> fecha_alta_date in Silver DuckDB.

    Operates directly on the establecimientos table via SQL UPDATE,
    one period at a time, with full metadata tracking.

    Handles:
    - 6 known fecha_alta_str formats
    - Incremental processing (skip already-normalized periods)
    - Force mode (re-normalize all periods)
    - Unrecognized format detection and reporting
    """

    def __init__(
        self,
        tracker: MetadataTracker,
        silver_db_path: Path,
    ):
        """
        Initialize date normalizer.

        Args:
            tracker: MetadataTracker instance for job/task tracking
            silver_db_path: Path to Silver DuckDB warehouse
        """
        self.tracker = tracker
        self.silver_db_path = Path(silver_db_path)

    def _get_all_periods(self) -> list[date]:
        """
        Get all distinct periods from Silver DuckDB.

        Returns:
            Sorted list of period dates
        """
        conn = duckdb.connect(str(self.silver_db_path), read_only=True)
        try:
            result = conn.execute(
                "SELECT DISTINCT period FROM establecimientos ORDER BY period"
            ).fetchall()
            return [row[0] for row in result]
        finally:
            conn.close()

    def _get_completed_periods(self) -> set[date]:
        """
        Get periods already successfully normalized from metadata DB.

        Returns:
            Set of period dates with SUCCESS status
        """
        with self.tracker._get_connection() as conn:
            result = conn.execute(
                """
                SELECT DISTINCT period FROM normalize_tasks
                WHERE status = 'SUCCESS'
                """
            ).fetchall()
            return {row[0] for row in result}

    def _get_pending_periods(self, force: bool) -> list[date]:
        """
        Determine which periods need normalization.

        Args:
            force: If True, return all periods. If False, exclude SUCCESS periods.

        Returns:
            Sorted list of period dates to process
        """
        all_periods = self._get_all_periods()

        if force:
            return all_periods

        completed = self._get_completed_periods()
        pending = [p for p in all_periods if p not in completed]
        return sorted(pending)

    def _build_update_sql(self, force: bool) -> str:
        """
        Build the UPDATE SQL statement.

        Args:
            force: If True, update all rows (including previously normalized).
                   If False, only update rows where fecha_alta_date IS NULL.

        Returns:
            SQL UPDATE statement with parameter placeholder for period
        """
        force_clause = "" if force else "AND fecha_alta_date IS NULL"
        return _UPDATE_SQL_TEMPLATE.format(
            spanish_month_case=_SPANISH_MONTH_CASE_SQL,
            force_clause=force_clause,
        )

    def _normalize_period(
        self,
        conn: duckdb.DuckDBPyConnection,
        period: date,
        force: bool,
    ) -> tuple[int, int, int, list[str]]:
        """
        Normalize dates for a single period.

        Args:
            conn: DuckDB connection to Silver DB
            period: Period to normalize
            force: Whether to re-normalize already-normalized rows

        Returns:
            Tuple of (rows_total, rows_normalized, rows_unrecognized, unrecognized_values)
        """
        # Count total rows for this period
        rows_total = conn.execute(
            "SELECT COUNT(*) FROM establecimientos WHERE period = ?", [period]
        ).fetchone()[0]

        # Execute the UPDATE
        update_sql = self._build_update_sql(force=force)
        conn.execute(update_sql, [period])

        # Count rows that now have fecha_alta_date populated
        rows_with_date = conn.execute(
            "SELECT COUNT(*) FROM establecimientos WHERE period = ? AND fecha_alta_date IS NOT NULL",
            [period],
        ).fetchone()[0]

        rows_normalized = rows_with_date
        rows_unrecognized = rows_total - rows_with_date

        # Find unrecognized values (if any)
        unrecognized_values: list[str] = []
        if rows_unrecognized > 0:
            unrecognized_result = conn.execute(
                """
                SELECT DISTINCT fecha_alta_str
                FROM establecimientos
                WHERE period = ?
                  AND fecha_alta_date IS NULL
                  AND fecha_alta_str IS NOT NULL
                ORDER BY fecha_alta_str
                """,
                [period],
            ).fetchall()
            unrecognized_values = [row[0] for row in unrecognized_result]

        return rows_total, rows_normalized, rows_unrecognized, unrecognized_values

    def run(self, force: bool = False) -> bool:
        """
        Run date normalization pipeline.

        Processes each pending period: UPDATE fecha_alta_date from fecha_alta_str
        using regex-based format detection in SQL.

        Args:
            force: If True, re-normalize all periods regardless of status.
                   If False, only normalize periods not yet marked SUCCESS.

        Returns:
            True if all periods normalized successfully, False otherwise
        """
        if force:
            logger.warning("⚠️ MODO FORCE ACTIVADO: Se re-normalizarán todas las fechas.")

        # Create job run
        run_id = self.tracker.create_run(
            job_name="denue_normalize_dates",
            job_type="NORMALIZE",
            trigger_type="MANUAL",
        )

        # Get pending periods
        periods = self._get_pending_periods(force=force)

        if not periods:
            logger.info("✨ Fechas ya normalizadas. Nada por procesar.")
            self.tracker.complete_run(run_id, JobStatus.SUCCESS)
            return True

        logger.info(f"⚙️ Iniciando normalización de fechas para {len(periods)} periodos...")

        tasks_succeeded = 0
        tasks_failed = 0
        session_total_normalized = 0

        # Open a single connection for all periods (efficiency)
        conn = duckdb.connect(str(self.silver_db_path))
        try:
            for idx, period in enumerate(periods, start=1):
                period_str = str(period)

                # Create normalize task
                task_id = self.tracker.create_normalize_task(
                    run_id=run_id,
                    task_name=f"normalize_dates_{period_str}",
                    period=period_str,
                )

                logger.info(
                    f"📅 [{idx}/{len(periods)}] Normalizando fechas: periodo {period_str}..."
                )

                self.tracker.start_normalize_task(task_id)

                try:
                    rows_total, rows_normalized, rows_unrecognized, unrecognized_values = (
                        self._normalize_period(conn, period, force=force)
                    )

                    # Log results
                    logger.info(
                        f"   ✅ {rows_normalized:,} fechas normalizadas de {rows_total:,} filas."
                    )

                    if unrecognized_values:
                        # Show only the values, not every row
                        values_str = ", ".join(f'"{v}"' for v in unrecognized_values[:10])
                        extra = (
                            f" (+{len(unrecognized_values) - 10} mas)"
                            if len(unrecognized_values) > 10
                            else ""
                        )
                        logger.error(
                            f"   ❌ {rows_unrecognized:,} fechas no reconocidas: {values_str}{extra}"
                        )

                    self.tracker.complete_normalize_task(
                        task_id=task_id,
                        status=TaskStatus.SUCCESS,
                        rows_total=rows_total,
                        rows_normalized=rows_normalized,
                        rows_unrecognized=rows_unrecognized,
                        error_message=(
                            f"Unrecognized formats: {', '.join(unrecognized_values[:10])}"
                            if unrecognized_values
                            else None
                        ),
                    )
                    tasks_succeeded += 1
                    session_total_normalized += rows_normalized

                except Exception as e:
                    logger.error(f"   ❌ Error normalizando periodo {period_str}: {e}")
                    self.tracker.complete_normalize_task(
                        task_id=task_id,
                        status=TaskStatus.FAILED,
                        error_type="PROCESSING",
                        error_message=str(e),
                    )
                    tasks_failed += 1

            # Checkpoint to flush all changes to disk
            conn.execute("CHECKPOINT;")

        finally:
            conn.close()

        # Complete job run
        final_status = JobStatus.SUCCESS if tasks_failed == 0 else JobStatus.PARTIAL
        self.tracker.complete_run(run_id, final_status)

        logger.info(
            f"✨ Normalización terminada. Total: {session_total_normalized:,} fechas normalizadas."
        )
        logger.info(f"   📊 Resumen: {tasks_succeeded} exitosos, {tasks_failed} fallidos")

        return tasks_failed == 0
