"""
CLEE Resolution Stage: Entity Resolution (Silver DuckDB)

Resolves entity identities across temporal snapshots, matching
establishments over time and generating master identifiers (CLEEs).

Propagates CLEEs from the most recent period backward through time
(future -> past). Uses a sliding-window approach: each target period
is compared against one reference period at a time (instead of a
monolithic growing KB), keeping peak RAM to ~5-8 GB.

For periods that already have CLEE assigned by INEGI (2021+), resolution
is deterministic (clee_propagado_radio = clee). For older periods without CLEE
(2015-2020), a spatial tolerance radius (BallTree) pre-filter and a
conditional matching cascade resolve identities probabilistically.

Reads from and writes back to the Silver DuckDB warehouse directly,
populating the `clee_propagado_radio` column on the `establecimientos` table.
"""

import gc
from datetime import date
from pathlib import Path

import duckdb
import polars as pl

from src_etl.core.logging import get_stage_logger
from src_etl.db.indexes import suspended_indexes
from src_etl.utils.clee_resolution_config import (
    SILVER_READ_QUERY,
    SPATIAL_RADIUS_M,
)
from src_etl.utils.entity_resolution import (
    assign_new_clees,
    build_single_period_kb,
    get_region_prefix,
    propagate_clees_h3,
    propagate_clees_radio,
    set_matching_logger,
)
from src_etl.utils.matching import clean_contact_columns
from src_etl.utils.warehouse import connect_configured, get_distinct_periods
from src_metadata.models import JobStatus, TaskStatus
from src_metadata.tracker import MetadataTracker

_LOG_FILE_TEMPLATE = "./data/etl_clee_resolution_{method}.log"
"""Plantilla del archivo de log POR METODO de propagacion (radio ->
etl_clee_resolution_radio.log, h3 -> etl_clee_resolution_h3.log). Cada
metodo registrado en CLEEResolver.METHODS obtiene su archivo
automaticamente al instanciarse el resolver -- agregar un metodo nuevo al
registro NO requiere tocar el logging. Mismo patron de stage-log que
data/etl_h3_backfill.log y data/etl_normalize.log."""


class CLEEResolver:
    """
    Resolves entity identities across temporal snapshots.

    Uses a sliding-window approach: for each target period without CLEE,
    iterates over all already-resolved periods one at a time as reference,
    propagating clee_propagado_radio where spatial + fuzzy matching finds a hit.

    Peak memory: ~5-8 GB (1 reference period + 1 target period).

    Modes:
        force=True:  Reset all clee_propagado_radio, re-resolve from scratch.
        force=False: Only process records where clee_propagado_radio IS NULL
                     (incremental, preserves already-assigned values).
    """

    # Metodos disponibles: mapea method -> (columna destino, funcion de
    # propagacion, sufijo de prefijo de region). Un solo lugar para registrar
    # nuevos metodos de propagacion sin duplicar el orquestador.
    METHODS = {
        "radio": {
            "column": "clee_propagado_radio",
            "propagate": propagate_clees_radio,
            "prefix_suffix": "",
        },
        "h3": {
            "column": "clee_propagado_h3",
            "propagate": propagate_clees_h3,
            "prefix_suffix": "H3",
        },
    }

    def __init__(
        self,
        silver_db_path: Path,
        tracker: MetadataTracker,
        method: str = "radio",
    ):
        """
        Initialize CLEE resolution stage.

        Args:
            silver_db_path: Path to Silver DuckDB warehouse
            tracker: MetadataTracker instance for job/task tracking
            method: "radio" (BallTree, columna clee_propagado_radio) o
                "h3" (blocking H3, columna clee_propagado_h3). Ambos metodos
                comparten TODO el motor de ventana deslizante; solo cambian
                la columna destino y el generador de candidatos.
        """
        if method not in self.METHODS:
            raise ValueError(f"Metodo desconocido '{method}'. Opciones: {list(self.METHODS)}")

        self.silver_db_path = Path(silver_db_path)
        self.tracker = tracker
        self.method = method

        cfg = self.METHODS[method]
        self.master_column: str = cfg["column"]
        self._propagate = cfg["propagate"]
        self._prefix_suffix: str = cfg["prefix_suffix"]

        # Log dedicado por metodo (archivo + stdout). La INYECCION al motor
        # de matching compartido (set_matching_logger) NO va aqui: el
        # orquestador construye un resolver por metodo al arrancar y el
        # ultimo constructor pisaria al resto -- se inyecta al INICIO de
        # cada corrida (run/process_national), cuando este metodo es el
        # unico activo.
        self.logger = get_stage_logger(
            f"src_etl.clee_resolution.{method}",
            Path(_LOG_FILE_TEMPLATE.format(method=method)),
        )

        # Spatial tolerance from config (easy to calibrate)
        self.spatial_tol = SPATIAL_RADIUS_M

        # KB cache por metodo: la identidad cacheada difiere entre radio y h3,
        # asi que cada metodo tiene su subdirectorio.
        self.kb_cache_dir = self.silver_db_path.parent / "kb_cache" / method

    def _get_connection(self) -> duckdb.DuckDBPyConnection:
        """Get a DuckDB connection to Silver warehouse (settings limits applied)."""
        return connect_configured(self.silver_db_path)

    def _count_unresolved(self) -> int:
        """Count rows where the method's master column IS NULL."""
        with self._get_connection() as conn:
            return conn.execute(f"""
                SELECT COUNT(*) FROM establecimientos
                WHERE {self.master_column} IS NULL
            """).fetchone()[0]

    def _get_sorted_periods(self) -> list[date]:
        """
        Get all periods from Silver DuckDB sorted newest-first.

        Returns:
            List of date objects sorted chronologically descending.
            Example: [date(2024, 11, 1), date(2024, 5, 1), ...]
        """
        with self._get_connection() as conn:
            return get_distinct_periods(conn, descending=True)

    def _get_periods_with_clee_coverage(self) -> dict[date, bool]:
        """
        Classify each period as having full CLEE coverage or not.

        Returns:
            Dict mapping period -> True if 100% rows have CLEE, False otherwise.
        """
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT
                    period,
                    COUNT(*) AS total,
                    COUNT(CASE WHEN clee IS NOT NULL AND clee != '' THEN 1 END) AS with_clee
                FROM establecimientos
                WHERE period IS NOT NULL
                GROUP BY period
                ORDER BY period DESC
            """).fetchall()

        return {r[0]: (r[2] == r[1]) for r in rows}

    def _load_period(self, period: date) -> pl.DataFrame:
        """
        Load a single period's data from Silver DuckDB.

        Uses SILVER_READ_QUERY which JOINs with catalog tables to get
        `nombre_act` and casts ENUM columns to VARCHAR, filtered by period.

        Args:
            period: Date object representing the period (e.g. date(2024, 11, 1))

        Returns:
            Polars DataFrame with columns from COLS_NEEDED_FOR_MATCHING
        """
        query = SILVER_READ_QUERY + "\n    WHERE e.period = ?"

        with self._get_connection() as conn:
            result = conn.execute(query, [period])
            df = pl.from_arrow(result.fetch_arrow_table())

        if df.is_empty():
            return df

        # clean_contact_columns es el punto unico de sanitizacion
        # (rellena nulos de texto + deriva tel/email/www _clean)
        return clean_contact_columns(df)

    # ------------------------------------------------------------------
    # KB cache (parquet por periodo de referencia)
    # ------------------------------------------------------------------

    def _kb_cache_path(self, period: date) -> Path:
        """Path del parquet de KB cacheada para un periodo."""
        return self.kb_cache_dir / f"{period.isoformat()}.parquet"

    def _get_ref_kb(self, period: date) -> pl.DataFrame:
        """
        Obtiene la KB de un periodo de referencia, usando cache en disco.

        Primera vez: carga el periodo de DuckDB (JOIN + limpieza), construye
        la KB de 10 columnas y la persiste en parquet (escritura atomica via
        tmp + rename). Siguientes veces: lee el parquet directo, evitando
        re-materializar el periodo completo (~19 columnas) en RAM.
        """
        cache_path = self._kb_cache_path(period)
        if cache_path.exists():
            self.logger.info(f"   KB de {period} desde cache ({cache_path.name})")
            return pl.read_parquet(cache_path)

        df_ref = self._load_period(period)
        ref_kb = build_single_period_kb(df_ref, self.master_column)
        del df_ref
        gc.collect()

        self.kb_cache_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_suffix(".parquet.tmp")
        ref_kb.write_parquet(tmp_path)
        tmp_path.rename(cache_path)
        self.logger.info(f"   KB de {period} cacheada ({ref_kb.height:,} filas)")

        return ref_kb

    def _invalidate_kb_cache(self, period: date) -> None:
        """Borra la KB cacheada de un periodo (tras re-escribir sus clee_propagado_radio)."""
        cache_path = self._kb_cache_path(period)
        if cache_path.exists():
            cache_path.unlink()

    def _clear_kb_cache(self) -> None:
        """Borra todo el cache de KBs (modo --force)."""
        if not self.kb_cache_dir.exists():
            return
        removed = 0
        for f in self.kb_cache_dir.glob("*.parquet"):
            f.unlink()
            removed += 1
        for f in self.kb_cache_dir.glob("*.parquet.tmp"):
            f.unlink()
        if removed:
            self.logger.info(f"   KB cache limpiado ({removed} archivos)")

    def _get_total_rows(self) -> int:
        """Get total row count in establecimientos."""
        with self._get_connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM establecimientos").fetchone()[0]

    def _write_period_results(self, mapping: pl.DataFrame) -> int:
        """
        Write master-column assignments for a single period to Silver DuckDB.

        Uses batched UPDATE statements via temp table for efficiency.

        Args:
            mapping: DataFrame with columns [id, <master_column>]

        Returns:
            Number of rows updated
        """
        if mapping.is_empty():
            return 0

        col = self.master_column
        total = mapping.height
        batch_size = 50_000
        updated = 0

        with self._get_connection() as conn:
            for offset in range(0, total, batch_size):
                batch = mapping.slice(offset, batch_size)

                conn.execute(
                    f"CREATE OR REPLACE TEMP TABLE _clee_updates (id VARCHAR, {col} VARCHAR)"
                )
                conn.execute("INSERT INTO _clee_updates SELECT * FROM batch")
                result = conn.execute(f"""
                    UPDATE establecimientos e
                    SET {col} = u.{col}
                    FROM _clee_updates u
                    WHERE e.id = u.id
                """)
                batch_updated = result.fetchone()[0] if result.description else batch.height
                updated += batch_updated
                conn.execute("DROP TABLE IF EXISTS _clee_updates")

        return updated

    # ------------------------------------------------------------------
    # Phase 1: Deterministic resolution (periods with CLEE from INEGI)
    # ------------------------------------------------------------------

    def _resolve_deterministic_periods(
        self,
        periods: list[date],
        clee_coverage: dict[date, bool],
        force: bool,
    ) -> list[date]:
        """
        Resolve periods that have 100% CLEE coverage deterministically.

        For these periods, clee_propagado_radio = clee (direct copy via SQL UPDATE).
        No BallTree, no fuzzy matching needed.

        Args:
            periods: All periods sorted newest-first.
            clee_coverage: Dict mapping period -> has_full_clee.
            force: If True, overwrite existing clee_propagado_radio values.

        Returns:
            List of periods that were resolved (available as references).
        """
        deterministic_periods = [p for p in periods if clee_coverage.get(p, False)]

        if not deterministic_periods:
            return []

        self.logger.info(
            f"[FASE 1] Resolucion deterministica: {len(deterministic_periods)} periodos con CLEE"
        )

        col = self.master_column
        with self._get_connection() as conn:
            if force:
                # Reset SOLO la columna de este metodo (no toca la del otro)
                conn.execute(f"UPDATE establecimientos SET {col} = NULL")

            # Copy clee -> master_column for all rows with CLEE.
            # IS DISTINCT FROM = no-op guard: en corridas incrementales las
            # filas ya sembradas no se reescriben (sin el guard, DuckDB
            # reescribia ~45M filas identicas en CADA corrida del transform).
            conn.execute(f"""
                UPDATE establecimientos
                SET {col} = clee
                WHERE clee IS NOT NULL AND clee != ''
                  AND {col} IS DISTINCT FROM clee
            """)

            # Count seeded rows
            count = conn.execute(f"""
                SELECT COUNT(*) FROM establecimientos
                WHERE {col} IS NOT NULL
            """).fetchone()[0]

        self.logger.info(
            f"   {col} = clee para {count:,} filas ({len(deterministic_periods)} periodos)"
        )

        return deterministic_periods

    # ------------------------------------------------------------------
    # Phase 2: Probabilistic resolution (sliding window)
    # ------------------------------------------------------------------

    def _resolve_probabilistic_periods(
        self,
        periods: list[date],
        clee_coverage: dict[date, bool],
        resolved_periods: list[date],
        region_prefix: str,
    ) -> int:
        """
        Resolve periods without CLEE using sliding-window approach.

        For each target period P (no CLEE, newest-first):
          1. Load P from DuckDB (~3M rows, all clee_propagado_radio NULL)
          2. For each already-resolved period R NEWER than P (nearest-first:
             closest to P first; see docs/findings.md 2026-07-21):
             - Load R, build KB (scalar, ~6M rows max)
             - Run propagate_clees(P, KB_R) to assign matches
             - Free R from RAM
          3. Assign NEW_prefix-uuid to remaining unresolved rows
          4. Write results to DuckDB
          5. Mark P as resolved (available as reference for next periods)

        Args:
            periods: All periods sorted newest-first.
            clee_coverage: Dict mapping period -> has_full_clee.
            resolved_periods: Periods already resolved (from Phase 1).
            region_prefix: Prefix for generating temporal CLEEs.

        Returns:
            Total number of rows resolved in this phase.
        """
        probabilistic_periods = [p for p in periods if not clee_coverage.get(p, False)]

        if not probabilistic_periods:
            self.logger.info("[FASE 2] No hay periodos sin CLEE. Nada por resolver.")
            return 0

        self.logger.info(
            f"[FASE 2] Resolucion probabilistica: "
            f"{len(probabilistic_periods)} periodos sin CLEE "
            f"(ventana deslizante, radio={self.spatial_tol}m)"
        )

        total_resolved = 0

        for p_idx, target_period in enumerate(probabilistic_periods):
            self.logger.info(
                f"🔗 [{p_idx + 1}/{len(probabilistic_periods)}] "
                f"Resolviendo periodo {target_period}..."
            )

            # Load target period (all rows, clee_propagado_radio will be NULL)
            df_target = self._load_period(target_period)
            if df_target.is_empty():
                self.logger.info(f"   {target_period}: vacio, saltando.")
                resolved_periods.append(target_period)
                continue

            target_total = df_target.height
            col = self.master_column

            # Ensure master_column exists and is NULL for this period
            if col not in df_target.columns:
                df_target = df_target.with_columns(pl.lit(None).cast(pl.Utf8).alias(col))
            else:
                df_target = df_target.with_columns(
                    pl.when(pl.col(col) == "")
                    .then(pl.lit(None).cast(pl.Utf8))
                    .otherwise(pl.col(col))
                    .alias(col)
                )

            # Ventana deslizante NEAREST-FIRST: solo referencias futuras al
            # target (la propagacion va futuro -> pasado), de la mas CERCANA
            # temporalmente a la mas lejana. Fase 7 (docs/findings.md,
            # 2026-07-21) midio que la distancia temporal target-referencia
            # domina la precision con los mismos umbrales: 95.9% nearest vs
            # 71.3% newest (la referencia mas lejana acaparaba el 79% del
            # volumen con ~65% de precision), y encadenar a traves de
            # periodos resueltos probabilisticamente cuesta ~0.15 pts por
            # salto — ordenes de magnitud menos que saltar años de distancia.
            usable_refs = sorted(r for r in resolved_periods if r > target_period)

            for ref_period in usable_refs:
                # Count how many still need resolution
                still_missing = df_target.filter(pl.col(col).is_null()).height
                if still_missing == 0:
                    break

                self.logger.info(f"   vs {ref_period} ({still_missing:,} pendientes)...")

                # Load reference KB (from parquet cache after first build)
                ref_kb = self._get_ref_kb(ref_period)

                # Propagate identities from reference to target (metodo activo)
                df_target = self._propagate(df_target, ref_kb, self.spatial_tol)

                del ref_kb
                gc.collect()

            # Assign NEW CLEEs to remaining unresolved rows
            still_missing = df_target.filter(pl.col(col).is_null()).height
            if still_missing > 0:
                self.logger.info(f"   Asignando {still_missing:,} NEW_{region_prefix}-* CLEEs")
                df_target = assign_new_clees(df_target, region_prefix, col)

            # Write results to DuckDB immediately (no accumulation in RAM)
            mapping = df_target.select(["id", col])
            rows_written = self._write_period_results(mapping)
            total_resolved += rows_written

            # Su identidad cambio: invalidar KB cacheada (si existia)
            self._invalidate_kb_cache(target_period)

            self.logger.info(
                f"   ✅ {target_period} resuelto: {rows_written:,} filas escritas "
                f"({target_total - still_missing:,} propagados, "
                f"{still_missing:,} nuevos)"
            )

            # Mark this period as resolved (available as reference)
            resolved_periods.append(target_period)

            # Free memory
            del df_target, mapping
            gc.collect()

        return total_resolved

    # ------------------------------------------------------------------
    # Main orchestrator
    # ------------------------------------------------------------------

    def process_national(self, force: bool = False) -> dict:
        """
        Process all periods using sliding-window entity resolution.

        Phase 1 (deterministic): Periods with CLEE get clee_propagado_radio = clee
        Phase 2 (probabilistic): Periods without CLEE are resolved via
        sliding-window comparison against all already-resolved periods.

        Args:
            force: If True, reset all clee_propagado_radio and re-resolve from scratch.

        Returns:
            Dict with resolution statistics.
        """
        # Redirigir el motor de matching compartido al log de ESTE metodo
        # (el orquestador instancia ambos resolvers; el activo se declara
        # al correr, no al construirse).
        set_matching_logger(self.logger)

        periods = self._get_sorted_periods()
        if not periods:
            return {
                "total_resolved": 0,
                "periods_processed": 0,
                "deterministic_periods": 0,
                "probabilistic_periods": 0,
                "region_prefix": "",
            }

        self.logger.info(f"   Periodos detectados ({len(periods)}): {periods}")

        # Classify periods by CLEE coverage
        clee_coverage = self._get_periods_with_clee_coverage()

        # Get region prefix
        with self._get_connection() as conn:
            state_rows = conn.execute(
                "SELECT DISTINCT cve_ent FROM establecimientos ORDER BY cve_ent"
            ).fetchall()
        state_list = [r[0] for r in state_rows] if state_rows else ["NATIONAL"]
        prefix = get_region_prefix(state_list) + self._prefix_suffix
        self.logger.info(f"   Prefijo de region (namespace): {prefix}")

        # --- Phase 1: Deterministic resolution ---
        resolved_periods = self._resolve_deterministic_periods(periods, clee_coverage, force)

        # --- Phase 2: Probabilistic resolution (sliding window) ---
        total_prob = self._resolve_probabilistic_periods(
            periods, clee_coverage, resolved_periods, prefix
        )

        return {
            "total_resolved": total_prob,
            "periods_processed": len(periods),
            "deterministic_periods": len([p for p in periods if clee_coverage.get(p, False)]),
            "probabilistic_periods": len([p for p in periods if not clee_coverage.get(p, False)]),
            "region_prefix": prefix,
        }

    def run(self, force: bool = False) -> bool:
        """
        Run CLEE resolution pipeline.

        Reads from Silver DuckDB period by period, resolves entity
        identities, and writes `clee_propagado_radio` back to the same table.

        Args:
            force: If True, reset all clee_propagado_radio values and re-run from scratch.
                   If False, only process rows where clee_propagado_radio IS NULL (incremental).

        Returns:
            True if successful, False otherwise
        """
        if force:
            self.logger.warning(
                f"MODO FORCE ACTIVADO: Se resetearan y re-resolveran todos los {self.master_column}."
            )
            self._clear_kb_cache()

        # Create job run (job_name distingue el metodo en metadata)
        run_id = self.tracker.create_run(
            job_name=f"denue_clee_resolver_{self.method}",
            job_type="CLEE_RESOLUTION",
            trigger_type="MANUAL",
        )

        # Create a single CLEE resolution task for the national run
        task_id = self.tracker.create_clee_resolution_task(
            run_id=run_id,
            task_name=f"clee_resolution_national_{self.method}",
            region_group="NATIONAL",
            state_list=["ALL"],
        )

        self.tracker.start_clee_resolution_task(task_id)

        try:
            # Check how many rows need resolution before starting
            total_rows = self._get_total_rows()
            unresolved_before = self._count_unresolved() if not force else total_rows

            self.logger.info(
                f"Iniciando resolucion CLEE [metodo={self.method} -> {self.master_column}]: "
                f"{total_rows:,} filas totales, "
                f"{unresolved_before:,} sin resolver, radio={self.spatial_tol}m"
            )

            if unresolved_before == 0 and not force:
                self.logger.info("Todas las entidades ya resueltas. Nada por procesar.")
                self.tracker.complete_clee_resolution_task(
                    task_id=task_id,
                    status=TaskStatus.SKIPPED,
                    error_message="No unresolved rows",
                )
                self.tracker.complete_run(run_id, JobStatus.SUCCESS)
                return True

            # La resolucion reescribe la columna maestra sobre decenas de
            # millones de filas; con su indice ART vivo cada UPDATE lo
            # mantiene fila por fila. Se suspende aqui y se restituye al
            # terminar (tambien si falla), sin paso manual.
            with self._get_connection() as index_conn, suspended_indexes(index_conn):
                stats = self.process_national(force=force)

            # Count remaining unresolved
            remaining = self._count_unresolved()

            # Complete task
            self.tracker.complete_clee_resolution_task(
                task_id=task_id,
                status=TaskStatus.SUCCESS,
                entities_input=total_rows,
                entities_matched=total_rows - remaining,
                master_clees_created=total_rows - remaining,
                periods_processed=stats["periods_processed"],
                output_path=str(self.silver_db_path),
            )

            self.logger.info(
                f"Resolucion CLEE terminada. "
                f"{stats['total_resolved']:,} filas actualizadas (prob.), "
                f"{remaining:,} pendientes."
            )
            self.logger.info(
                f"   Resumen: {stats['deterministic_periods']} periodos deterministicos, "
                f"{stats['probabilistic_periods']} periodos probabilisticos"
            )

            # Cleanup
            gc.collect()

            self.tracker.complete_run(run_id, JobStatus.SUCCESS)
            return True

        except Exception as e:
            self.logger.error(f"   Error en resolucion CLEE: {e}")
            self.tracker.complete_clee_resolution_task(
                task_id=task_id,
                status=TaskStatus.FAILED,
                error_message=str(e),
            )
            self.tracker.complete_run(run_id, JobStatus.FAILED)
            return False
