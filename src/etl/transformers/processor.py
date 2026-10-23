"""Ingest: los ZIP de Bronze se vuelven una particion de Silver.

**La unidad de carga es el corte, no el archivo.** Un corte del DENUE son
decenas de ZIP (uno por sector y entidad) que juntos forman un periodo, y
ese periodo es una particion de `silver.establecimientos`. Se leen todos,
se concatenan, se cargan en una tabla aparte y esa tabla se engancha de
golpe. Mientras dura, Silver no ve nada; al terminar, lo ve todo.

Eso hace del corte la unidad de recuperacion: uno que salio mal se
desengancha y se rehace sin tocar los otros diecinueve. El precio es que
`--force` ya no opera por archivo --reprocesar un ZIP suelto no tiene
sentido si la particion se construye entera-- sino por periodo.

La fecha de alta y las celdas H3 vienen calculadas desde `enrich.py`, antes
del COPY. Eran dos etapas aparte que hacian UPDATE sobre la tabla ya
cargada; aqui salen gratis.
"""

import zipfile
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
from sqlalchemy import Engine

from src.etl.transformers.catalogs import resolve as resolve_catalogs
from src.etl.transformers.enrich import add_h3_cells, normalize_fecha_alta
from src.etl.utils.denue_schema import (
    COLUMN_ALIASES,
    DOMAIN_VALUE_ALIASES,
    ENTITY_VALUE_NORMALIZATION,
    MASTER_COLUMNS,
    REQUIRED_COLUMNS,
)
from src.meta.tracker import MetadataTracker
from src.shared.db.base import get_table
from src.shared.db.copy import copy_dataframe
from src.shared.db.partitions import drop_partition, existing_partitions, partition_name
from src.shared.db.staging import staging_partition
from src.shared.enums import JobStatus, TaskStatus
from src.shared.logging import get_stage_logger

# Logging de stage centralizado (sin basicConfig: no toca el root logger)
logger = get_stage_logger(__name__, Path("./data/etl_process.log"))

#: Tabla que el ingest escribe. Se registra en `process_tasks.target_table`.
TARGET_TABLE = "establecimientos"


def period_to_date(period: str) -> date:
    """Fecha del corte a partir de como INEGI nombra sus directorios.

    Los cortes vienen como `MM_AAAA` --mes primero-- y alguno antiguo como
    `AAAA` suelto. Ambos se llevan al primer dia del mes, que es el grano
    real del dato y la clave de particion.

    Args:
        period: nombre del corte, p. ej. "11_2024" o "2015".

    Returns:
        date: primer dia del corte.

    Raises:
        ValueError: si el nombre no es ninguno de los dos formatos.
    """
    if "_" in period:
        mes, ano = period.split("_", 1)
        return date(int(ano), int(mes), 1)
    return date(int(period), 1, 1)


class BronzeToSilverProcessor:
    """
    Convierte los ZIP de Bronze en particiones de `silver.establecimientos`.

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
        engine: Engine | None = None,
    ):
        """
        Args:
            data_root: raiz de datos; de ahi cuelga `bronze/`.
            tracker: registro de corridas y tareas.
            engine: conexion a la base; por default la del proyecto. Se
                inyecta en los tests para apuntar a la de pruebas.
        """
        self.data_root = Path(data_root)
        self.tracker = tracker
        self._engine = engine

    @property
    def engine(self) -> Engine:
        """Engine de la base, resuelto la primera vez que se usa."""
        if self._engine is None:
            from src.shared.db.session import get_engine

            self._engine = get_engine()
        return self._engine

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
        # duplicados vs la tabla, no dentro del mismo lote (el COPY falla
        # con PK duplicada intra-INSERT; visto en comercio_al_por_menor
        # 03_2017). Filas con los mismos 5 componentes de id son el mismo
        # registro para el pipeline: se conserva la primera.
        pre_dedupe = df.height
        df = df.unique(subset=["id"], keep="first")
        if pre_dedupe != df.height:
            logs["stats"]["rows_dropped_dup_id"] = pre_dedupe - df.height

        # La fecha y las celdas H3 se derivan aqui, con los datos todavia
        # en memoria: eran dos etapas que hacian UPDATE sobre la tabla ya
        # cargada (ver enrich.py).
        df = add_h3_cells(normalize_fecha_alta(df))

        logs["stats"]["rows_final"] = df.height
        return df, logs

    def read_zip(self, zip_path: Path, period: str) -> pl.DataFrame:
        """Lee un ZIP y devuelve sus filas limpias y enriquecidas.

        No escribe nada: quien carga es `ingest_period`, una vez por corte
        con los ZIP de todo el periodo juntos.

        Args:
            zip_path: archivo a leer.
            period: nombre del corte al que pertenece.

        Returns:
            pl.DataFrame: filas listas para el COPY.
        """
        df, logs = self._clean_raw_data(zip_path, period)
        if renombrados := logs["schema_changes"].get("duplicates_renamed"):
            # Solo el conteo y el nombre del archivo: los cortes viejos de
            # INEGI traen encabezados en prosa y listar los 35 nombres por
            # archivo llenaba el log de lineas de 900 caracteres, que es lo
            # que hace imposible ver el progreso real. Los nombres siguen
            # disponibles en DEBUG.
            logger.info(f"      {zip_path.name}: {len(renombrados)} columnas renombradas")
            logger.debug(f"      renombradas: {', '.join(renombrados)}")
        if descartadas := logs["stats"].get("rows_dropped_invalid_req"):
            logger.warning(f"      {descartadas} filas sin columnas requeridas, descartadas")
        return df

    def ingest_period(self, run_id: str, period: str, descargas: list[dict]) -> int:
        """Carga un corte completo como una particion nueva.

        Lee todos los ZIP del periodo, los concatena y los engancha de una
        sola vez. Cada archivo deja su propia `process_task` --ahi vive el
        linaje y el conteo por archivo-- pero la escritura es una.

        Si el corte ya tenia particion, se elimina antes: la particion se
        construye entera, no se completa.

        Args:
            run_id: corrida a la que pertenece.
            period: nombre del corte.
            descargas: descargas exitosas del corte, con `source_path`.

        Returns:
            int: filas cargadas.

        Raises:
            Exception: lo que falle al leer o cargar. El contexto de
                staging deja Silver intacta antes de propagarlo.
        """
        fecha = period_to_date(period)
        marcos: list[pl.DataFrame] = []

        for descarga in descargas:
            zip_path = Path.cwd() / descarga["source_path"]
            task_id = self.tracker.create_process_task(
                run_id=run_id,
                task_name=zip_path.name,
                source_path=descarga["source_path"],
                period=period,
                target_table=TARGET_TABLE,
            )
            self.tracker.start_process_task(task_id)

            if not zip_path.exists():
                self.tracker.complete_process_task(
                    task_id=task_id,
                    status=TaskStatus.FAILED,
                    error_message=f"el archivo no esta en disco: {zip_path}",
                )
                logger.warning(f"   falta en disco: {zip_path.name}")
                continue

            try:
                df = self.read_zip(zip_path, period)
            except Exception as exc:
                self.tracker.complete_process_task(
                    task_id=task_id, status=TaskStatus.FAILED, error_message=str(exc)
                )
                raise

            marcos.append(df)
            self.tracker.complete_process_task(
                task_id=task_id,
                status=TaskStatus.SUCCESS,
                rows_extracted=df.height,
                file_size_bytes=zip_path.stat().st_size,
            )

        if not marcos:
            logger.warning(f"   corte {period} sin archivos legibles, se omite")
            return 0

        corte = pl.concat(marcos, how="diagonal_relaxed")

        # Deduplicar DESPUES de juntar los archivos, no dentro de cada uno.
        # `id` es un hash del contenido de la fila, asi que dos ZIP del mismo
        # corte pueden traer la misma fila --un establecimiento que aparece
        # en dos cortes sectoriales-- y producir el mismo id. Deduplicar solo
        # por archivo dejaba pasar esos choques, y ahora que la particion se
        # carga de una vez, un id repetido rompe la PK y tumba el corte
        # entero. Antes no se notaba porque el INSERT los ignoraba en
        # silencio.
        antes = corte.height
        corte = corte.unique(subset=["id"], keep="first")
        if repetidos := antes - corte.height:
            logger.info(f"   {repetidos:,} filas repetidas entre archivos del corte")

        logger.info(f"   {corte.height:,} filas de {len(marcos)} archivos, cargando...")

        with self.engine.begin() as conn:
            # Validar y traducir dentro de la transaccion: los catalogos se
            # leen del mismo estado al que se va a escribir.
            corte = resolve_catalogs(conn, corte)

            # La particion se construye entera: si habia una, se va.
            if partition_name(fecha) in existing_partitions(conn):
                logger.info(f"   la particion de {period} ya existia, se reconstruye")
                drop_partition(conn, fecha)

            # Solo las columnas que la tabla tiene. La limpieza produce
            # columnas intermedias --nombres de catalogo resueltos para
            # validar, por ejemplo-- que no son parte del esquema; mandarlas
            # al COPY lo hace fallar con "column ... does not exist".
            del_modelo = [c.name for c in get_table(TARGET_TABLE).columns]
            columnas = [c for c in del_modelo if c in corte.columns]

            with staging_partition(conn, fecha) as destino:
                copy_dataframe(conn, corte, destino.table, destino.schema, columns=columnas)

        return corte.height

    def run(self, force: bool = False, periods: list[str] | None = None) -> bool:
        """Carga los cortes que falten, o los que se pidan.

        `periods` existe para no estrenar el ingest con los veinte cortes de
        golpe: cargar uno, revisarlo contra la base y seguir. Un corte que no
        tenga descargas se reporta en vez de ignorarse en silencio -- casi
        siempre es un nombre mal escrito.

        Args:
            force: reconstruir aunque el corte ya este cargado.
            periods: cortes a procesar (p. ej. ["03_2018"]); por default,
                los que tengan trabajo pendiente.

        Returns:
            bool: True si ningun corte fallo.

        Raises:
            ValueError: si algun corte pedido no tiene descargas.
        """
        run_id = self.tracker.create_run(
            job_name="denue_transform", job_type="TRANSFORM", trigger_type="MANUAL"
        )

        # Todas las descargas exitosas, agrupadas por corte: aunque solo
        # falte un archivo de un periodo, la particion se rehace completa.
        por_corte: dict[str, list[dict]] = defaultdict(list)
        for descarga in self.tracker.get_all_successful_downloads():
            if descarga["period"]:
                por_corte[descarga["period"]].append(descarga)

        if periods:
            desconocidos = sorted(set(periods) - por_corte.keys())
            if desconocidos:
                raise ValueError(
                    f"sin descargas para el/los corte(s) {desconocidos}. "
                    f"Disponibles: {sorted(por_corte)}"
                )
            pendientes = sorted(set(periods))
            logger.info(f"Cortes pedidos explicitamente: {', '.join(pendientes)}")
        elif force:
            logger.warning("MODO FORCE: se reconstruyen todos los cortes")
            pendientes = sorted(por_corte)
        else:
            con_trabajo = {
                t["period"]
                for t in self.tracker.get_pending_process_tasks(include_failed=True)
                if t["period"]
            }
            pendientes = sorted(con_trabajo & por_corte.keys())

        if not pendientes:
            logger.info("Silver al dia, nada por transformar.")
            self.tracker.complete_run(run_id, JobStatus.SUCCESS)
            return True

        logger.info(f"{len(pendientes)} corte(s) por cargar: {', '.join(pendientes)}")
        total = 0
        fallidos: list[str] = []

        for i, period in enumerate(pendientes, start=1):
            logger.info(f"[{i}/{len(pendientes)}] corte {period}")
            try:
                total += self.ingest_period(run_id, period, por_corte[period])
            except Exception as exc:
                # Un corte que falla no detiene a los demas: su particion
                # no se engancho, asi que Silver sigue consistente.
                fallidos.append(period)
                logger.error(f"   corte {period} fallido, Silver sin cambios: {exc}")

        estado = JobStatus.SUCCESS if not fallidos else JobStatus.PARTIAL
        self.tracker.complete_run(run_id, estado)

        logger.info(
            f"Ingest terminado: {total:,} filas en {len(pendientes) - len(fallidos)} corte(s)"
        )
        if fallidos:
            logger.error(f"   cortes fallidos: {', '.join(fallidos)}")
        return not fallidos
