"""
CLEE Resolution Stage: Entity Resolution sobre Silver

Resolves entity identities across temporal snapshots, matching
establishments over time and generating master identifiers (CLEEs).

Propagates CLEEs from the most recent period backward through time
(future -> past). Uses a sliding-window approach: each target period
is compared against one reference period at a time (instead of a
monolithic growing KB), keeping peak RAM to ~5-8 GB.

For periods that already have CLEE assigned by INEGI (2021+), resolution
is deterministic (clee_resuelto = clee). For older periods without CLEE
(2015-2020), a spatial tolerance radius (BallTree) pre-filter and a
conditional matching cascade resolve identities probabilistically.

Lee y escribe directamente sobre `silver.establecimientos`,
populating the resolution table of the active method.
"""

import gc
from datetime import date
from pathlib import Path

import polars as pl
from sqlalchemy import Engine, func, select, text

from src.etl.db.indexes import suspended_indexes
from src.etl.utils.clee_resolution_config import (
    ORIGEN_COLUMN,
    RESOLVED_COLUMN,
    SPATIAL_RADIUS_M,
    silver_read_query,
)
from src.etl.utils.entity_resolution import (
    assign_new_clees,
    build_single_period_kb,
    get_region_prefix,
    propagate_clees_h3,
    propagate_clees_radio,
    set_matching_logger,
)
from src.etl.utils.matching import clean_contact_columns
from src.meta.tracker import MetadataTracker
from src.shared.config import get_settings
from src.shared.db.base import get_table
from src.shared.db.copy import copy_dataframe
from src.shared.db.partitions import drop_partition, existing_partitions, partition_name
from src.shared.db.staging import staging_partition
from src.shared.enums import JobStatus, OrigenClee, TaskStatus
from src.shared.logging import get_stage_logger

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
    propagating the identity where spatial + fuzzy matching finds a hit.

    Peak memory: ~5-8 GB (1 reference period + 1 target period).

    Modes:
        force=True:  rebuild the method's table from scratch.
        force=False: only resolve observations without a row in it
                     (incremental, preserves already-assigned identities).
    """

    # Metodos disponibles: mapea method -> (columna destino, funcion de
    # propagacion, sufijo de prefijo de region). Un solo lugar para registrar
    # nuevos metodos de propagacion sin duplicar el orquestador.
    METHODS = {
        "radio": {
            "table": "clee_radio",
            "propagate": propagate_clees_radio,
            "prefix_suffix": "",
        },
        "h3": {
            "table": "clee_h3",
            "propagate": propagate_clees_h3,
            "prefix_suffix": "H3",
        },
    }

    def __init__(
        self,
        tracker: MetadataTracker,
        method: str = "radio",
        engine: Engine | None = None,
        kb_cache_dir: Path | None = None,
    ):
        """
        Initialize CLEE resolution stage.

        Args:
            tracker: MetadataTracker instance for job/task tracking
            method: "radio" (BallTree, tabla silver.clee_radio) o "h3"
                (blocking H3, tabla silver.clee_h3). Ambos metodos comparten
                TODO el motor de ventana deslizante; solo cambian la tabla
                destino y el generador de candidatos.
            engine: conexion a la base; por default la del proyecto.
            kb_cache_dir: donde cachear las KB de referencia; por default,
                `data/kb_cache/<metodo>`.
        """
        if method not in self.METHODS:
            raise ValueError(f"Metodo desconocido '{method}'. Opciones: {list(self.METHODS)}")

        self.tracker = tracker
        self.method = method
        self._engine = engine

        cfg = self.METHODS[method]
        self.clee_table: str = cfg["table"]
        self._propagate = cfg["propagate"]
        self._prefix_suffix: str = cfg["prefix_suffix"]

        # Log dedicado por metodo (archivo + stdout). La INYECCION al motor
        # de matching compartido (set_matching_logger) NO va aqui: el
        # orquestador construye un resolver por metodo al arrancar y el
        # ultimo constructor pisaria al resto -- se inyecta al INICIO de
        # cada corrida (run/process_national), cuando este metodo es el
        # unico activo.
        self.logger = get_stage_logger(
            f"src.etl.clee_resolution.{method}",
            Path(_LOG_FILE_TEMPLATE.format(method=method)),
        )

        # Spatial tolerance from config (easy to calibrate)
        self.spatial_tol = SPATIAL_RADIUS_M

        # KB cache por metodo: la identidad cacheada difiere entre radio y
        # h3, asi que cada metodo tiene su subdirectorio. Es cache en disco,
        # no almacenamiento: se puede borrar y se reconstruye.
        self.kb_cache_dir = (kb_cache_dir or get_settings().DATA_DIR / "kb_cache") / method

    @property
    def engine(self) -> Engine:
        """Engine de la base, resuelto la primera vez que se usa."""
        if self._engine is None:
            from src.shared.db.session import get_engine

            self._engine = get_engine()
        return self._engine

    def _count_unresolved(self) -> int:
        """Observaciones sin fila en la tabla de este metodo.

        No se pregunta por una columna NULL en `establecimientos`: la
        identidad ya no vive ahi. Ausencia de fila ES el pendiente.
        """
        with self.engine.connect() as conn:
            return conn.execute(
                text(
                    "SELECT COUNT(*) FROM silver.establecimientos e "
                    f"LEFT JOIN silver.{self.clee_table} r "
                    "  ON r.id = e.id AND r.period = e.period "
                    "WHERE r.id IS NULL"
                )
            ).scalar_one()

    def _get_sorted_periods(self) -> list[date]:
        """Cortes presentes en Silver, del mas antiguo al mas nuevo.

        Returns:
            list[date]: periodos ordenados.
        """
        estab = get_table("establecimientos")
        with self.engine.connect() as conn:
            return list(
                conn.execute(select(estab.c.period).distinct().order_by(estab.c.period)).scalars()
            )

    def _get_periods_with_clee_coverage(self) -> dict[date, bool]:
        """Que cortes traen CLEE de INEGI y cuales no.

        Los que lo traen se resuelven por copia directa; los que no, por
        matching probabilistico contra los anteriores.

        Returns:
            dict: periodo -> True si tiene al menos un CLEE no vacio.
        """
        estab = get_table("establecimientos")
        with self.engine.connect() as conn:
            filas = conn.execute(
                select(
                    estab.c.period,
                    func.count(estab.c.clee).filter(estab.c.clee != "") > 0,
                ).group_by(estab.c.period)
            ).all()
        return {period: bool(tiene) for period, tiene in filas}

    def _load_period(self, period: date) -> pl.DataFrame:
        """
        Carga las observaciones de un corte.

        Uses SILVER_READ_QUERY which JOINs with catalog tables to get
        `nombre_act` and casts ENUM columns to VARCHAR, filtered by period.

        Args:
            period: Date object representing the period (e.g. date(2024, 11, 1))

        Returns:
            Polars DataFrame with columns from COLS_NEEDED_FOR_MATCHING
        """
        # El filtro por periodo es lo que activa el pruning de la
        # particion: sin el, cada lectura barreria los veinte cortes.
        query = text(silver_read_query(self.clee_table) + "\n    WHERE e.period = :period")

        with self.engine.connect() as conn:
            filas = conn.execute(query, {"period": period})
            df = pl.DataFrame(filas.all(), schema=list(filas.keys()), orient="row")

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

        Primera vez: carga el periodo de la base (JOIN + limpieza), construye
        la KB de 10 columnas y la persiste en parquet (escritura atomica via
        tmp + rename). Siguientes veces: lee el parquet directo, evitando
        re-materializar el periodo completo (~19 columnas) en RAM.
        """
        cache_path = self._kb_cache_path(period)
        if cache_path.exists():
            self.logger.info(f"   KB de {period} desde cache ({cache_path.name})")
            return pl.read_parquet(cache_path)

        df_ref = self._load_period(period)
        ref_kb = build_single_period_kb(df_ref, RESOLVED_COLUMN)
        del df_ref
        gc.collect()

        self.kb_cache_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_suffix(".parquet.tmp")
        ref_kb.write_parquet(tmp_path)
        tmp_path.rename(cache_path)
        self.logger.info(f"   KB de {period} cacheada ({ref_kb.height:,} filas)")

        return ref_kb

    def _invalidate_kb_cache(self, period: date) -> None:
        """Borra la KB cacheada de un periodo (tras re-escribir su identidad)."""
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
        with self.engine.connect() as conn:
            return conn.execute(
                select(func.count()).select_from(get_table("establecimientos"))
            ).scalar_one()

    def _write_period_results(self, resueltas: pl.DataFrame, period: date) -> int:
        """
        Escribe las identidades de un corte, reemplazando la particion.

        La particion se construye entera en una tabla aparte y se engancha
        al final: hasta ese momento la anterior sigue siendo la vigente, asi
        que una corrida que muere a medias deja el resultado previo intacto.
        Es lo contrario de nulificar la columna antes de recalcular, que
        destruia el resultado bueno al arrancar.

        El DROP de la particion vieja y el ATTACH de la nueva ocurren en la
        MISMA transaccion, asi que desde afuera el corte nunca se ve sin
        identidades.

        Args:
            resueltas: columnas [id, period, clee_resuelto, origen].
            period: corte al que pertenecen.

        Returns:
            int: filas escritas.
        """
        if resueltas.is_empty():
            return 0

        columnas = ["id", "period", RESOLVED_COLUMN, ORIGEN_COLUMN]
        faltantes = [c for c in columnas if c not in resueltas.columns]
        if faltantes:
            raise ValueError(f"faltan columnas para escribir {self.clee_table}: {faltantes}")

        with self.engine.begin() as conn:
            if partition_name(period, self.clee_table) in existing_partitions(
                conn, table=self.clee_table
            ):
                drop_partition(conn, period, table=self.clee_table)

            with staging_partition(conn, period, parent=self.clee_table) as destino:
                copy_dataframe(
                    conn,
                    resueltas.select(columnas),
                    destino.table,
                    destino.schema,
                    columns=columnas,
                )

        return resueltas.height

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
        Resuelve por copia directa los cortes en que INEGI publico el CLEE.

        No hay BallTree ni matching difuso: la identidad viene del dato, asi
        que estas filas quedan con `origen = DIRECTO` y son la referencia de
        mayor confianza para la ventana deslizante de la fase 2.

        Se escribe corte por corte con reemplazo de particion, asi que el
        resultado de la corrida anterior sigue vigente hasta que el nuevo
        esta completo.

        Args:
            periods: todos los cortes, del mas nuevo al mas viejo.
            clee_coverage: corte -> si trae CLEE de INEGI.
            force: reconstruir la particion aunque ya este resuelta.

        Returns:
            list[date]: cortes resueltos, disponibles como referencia.
        """
        deterministic_periods = [p for p in periods if clee_coverage.get(p, False)]

        if not deterministic_periods:
            return []

        self.logger.info(
            f"[FASE 1] Resolucion deterministica: {len(deterministic_periods)} periodos con CLEE"
        )

        total = 0
        for period in deterministic_periods:
            if not force and self._ya_resuelto(period):
                self.logger.info(f"   {period}: ya resuelto, se conserva")
                continue

            escritas = self._write_period_results(self._filas_directas(period), period)
            self._invalidate_kb_cache(period)
            total += escritas
            self.logger.info(f"   {period}: {escritas:,} filas DIRECTO")

        self.logger.info(
            f"   {total:,} filas con identidad de INEGI ({len(deterministic_periods)} periodos)"
        )

        return deterministic_periods

    def _ya_resuelto(self, period: date) -> bool:
        """Si el corte ya tiene su particion en la tabla del metodo."""
        with self.engine.connect() as conn:
            return partition_name(period, self.clee_table) in existing_partitions(
                conn, table=self.clee_table
            )

    def _filas_directas(self, period: date) -> pl.DataFrame:
        """Filas de un corte cuya identidad es el `clee` que publico INEGI.

        Las filas sin `clee` quedan fuera: no tienen identidad que copiar y
        es la fase 2 la que decide si se propagan o se sintetizan. Su
        pendiente se expresa como ausencia de fila.

        Args:
            period: corte a leer.

        Returns:
            pl.DataFrame: columnas [id, period, clee_resuelto, origen].
        """
        with self.engine.connect() as conn:
            filas = conn.execute(
                text(
                    "SELECT e.id, e.period, e.clee AS clee_resuelto, :origen AS origen "
                    "FROM silver.establecimientos e "
                    "WHERE e.period = :period AND e.clee IS NOT NULL AND e.clee <> ''"
                ),
                {"period": period, "origen": OrigenClee.DIRECTO.value},
            )
            return pl.DataFrame(filas.all(), schema=list(filas.keys()), orient="row")

    # ------------------------------------------------------------------
    # Phase 2: Probabilistic resolution (sliding window)
    # ------------------------------------------------------------------

    def _resolve_probabilistic_periods(
        self,
        periods: list[date],
        clee_coverage: dict[date, bool],
        resolved_periods: list[date],
        region_prefix: str,
        force: bool,
    ) -> int:
        """
        Resolve periods without CLEE using sliding-window approach.

        For each target period P (no CLEE, newest-first):
          1. Carga P de la base (~3M filas, todas sin identidad)
          2. For each already-resolved period R NEWER than P (nearest-first:
             closest to P first; see docs/findings.md 2026-07-21):
             - Load R, build KB (scalar, ~6M rows max)
             - Run propagate_clees(P, KB_R) to assign matches
             - Free R from RAM
          3. Assign NEW_prefix-uuid to remaining unresolved rows
          4. Escribe los resultados
          5. Mark P as resolved (available as reference for next periods)

        Args:
            periods: All periods sorted newest-first.
            clee_coverage: Dict mapping period -> has_full_clee.
            resolved_periods: Periods already resolved (from Phase 1).
            region_prefix: Prefix for generating temporal CLEEs.
            force: descartar la identidad previa del corte y resolverlo de
                cero, en vez de conservar lo ya resuelto.

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

            # El corte llega con la identidad que ya tuviera (LEFT JOIN
            # contra la tabla del metodo): en modo incremental eso conserva
            # lo ya resuelto, y con force la tabla se reconstruye entera.
            df_target = self._load_period(target_period)
            if df_target.is_empty():
                self.logger.info(f"   {target_period}: vacio, saltando.")
                resolved_periods.append(target_period)
                continue

            target_total = df_target.height
            col = RESOLVED_COLUMN

            # Cadena vacia y NULL son el mismo pendiente; unificarlos evita
            # que una fila con "" se cuente como resuelta.
            df_target = df_target.with_columns(
                pl.when(pl.col(col) == "")
                .then(pl.lit(None).cast(pl.Utf8))
                .otherwise(pl.col(col))
                .alias(col)
            )
            if force:
                df_target = df_target.with_columns(
                    pl.lit(None).cast(pl.Utf8).alias(col),
                    pl.lit(None).cast(pl.Utf8).alias(ORIGEN_COLUMN),
                )
            ya_resueltas = df_target.filter(pl.col(col).is_not_null()).height

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

            # Lo que la ventana deslizante resolvio en esta pasada: las que
            # llegaron sin identidad y ahora la tienen. Se marca ANTES de
            # sintetizar, para no confundir propagado con nuevo.
            df_target = df_target.with_columns(
                pl.when(pl.col(ORIGEN_COLUMN).is_not_null())
                .then(pl.col(ORIGEN_COLUMN))
                .when(pl.col(col).is_not_null())
                .then(pl.lit(OrigenClee.PROPAGADO.value))
                .otherwise(None)
                .alias(ORIGEN_COLUMN)
            )

            # Assign NEW CLEEs to remaining unresolved rows
            still_missing = df_target.filter(pl.col(col).is_null()).height
            if still_missing > 0:
                self.logger.info(f"   Asignando {still_missing:,} NEW_{region_prefix}-* CLEEs")
                df_target = assign_new_clees(df_target, region_prefix, col)
                df_target = df_target.with_columns(
                    pl.col(ORIGEN_COLUMN).fill_null(OrigenClee.NUEVO.value)
                )

            # Se escribe de inmediato: no se acumula nada en RAM
            mapping = df_target.select(["id", "period", col, ORIGEN_COLUMN])
            rows_written = self._write_period_results(mapping, target_period)
            total_resolved += rows_written

            # Su identidad cambio: invalidar KB cacheada (si existia)
            self._invalidate_kb_cache(target_period)

            self.logger.info(
                f"   ✅ {target_period} resuelto: {rows_written:,} filas escritas "
                f"({ya_resueltas:,} ya resueltas, "
                f"{target_total - still_missing - ya_resueltas:,} propagadas, "
                f"{still_missing:,} nuevas)"
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

        Phase 1 (deterministic): Periods with CLEE get clee_resuelto = clee
        Phase 2 (probabilistic): Periods without CLEE are resolved via
        sliding-window comparison against all already-resolved periods.

        Args:
            force: If True, rebuild the method's table from scratch.

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

        # Prefijo de region, derivado de las entidades presentes
        estab = get_table("establecimientos")
        with self.engine.connect() as conn:
            entidades = list(
                conn.execute(select(estab.c.cve_ent).distinct().order_by(estab.c.cve_ent)).scalars()
            )
        state_list = entidades or ["NATIONAL"]
        prefix = get_region_prefix(state_list) + self._prefix_suffix
        self.logger.info(f"   Prefijo de region (namespace): {prefix}")

        # --- Phase 1: Deterministic resolution ---
        resolved_periods = self._resolve_deterministic_periods(periods, clee_coverage, force)

        # --- Phase 2: Probabilistic resolution (sliding window) ---
        total_prob = self._resolve_probabilistic_periods(
            periods, clee_coverage, resolved_periods, prefix, force
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

        Lee Silver corte por corte, resuelve identidades
        identities, and writes them to the method's resolution table.

        Args:
            force: If True, rebuild the method's table from scratch.
                   If False, only resolve rows without a row in it (incremental).

        Returns:
            True if successful, False otherwise
        """
        if force:
            self.logger.warning(
                f"MODO FORCE ACTIVADO: se reconstruye silver.{self.clee_table} entera."
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
                f"Iniciando resolucion CLEE [metodo={self.method} -> silver.{self.clee_table}]: "
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
            # millones de filas, y mantener sus indices durante un UPDATE
            # masivo es ordenes de magnitud mas lento. Se suspenden aqui y
            # se restituyen al terminar, tambien si falla.
            with self.engine.begin() as index_conn, suspended_indexes(index_conn):
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
                output_path="silver.establecimientos",
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
