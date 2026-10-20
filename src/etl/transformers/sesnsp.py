"""
Transform SESNSP: CSV consolidado (Bronze) -> Silver DuckDB.

Toma el CSV de incidencia delictiva municipal del corte mas reciente en
data/bronze/sesnsp/ (formato ancho: 12 columnas de mes) y lo carga en
Silver en formato largo:

- `sesnsp_delitos`: catalogo de la jerarquia del delito. NO se toca aqui:
  lo siembra `make db-migrate` desde data/seeds/seed_sesnsp_delitos.parquet.
  Este transform solo VALIDA que los delitos del corte existan en el
  catalogo y los enlaza por FK.
- `sesnsp_incidencia_municipal`: una fila por municipio x delito x mes,
  `fecha` DATE YYYY-MM-01 y claves geograficas zero-padded compatibles con
  DENUE (cve_ent '09', cve_mun '015').

Cada corte mensual del SESNSP REVISA cifras historicas (las carpetas de un
mes pasado pueden corregirse), por lo que la tabla de hechos se
reconstruye completa en cada ingesta en vez de hacerse upsert incremental.
Todo el unpivot corre dentro de DuckDB (out-of-core); Polars solo se usa
para las combinaciones distintas que se validan contra los catalogos.

Tracking: job `sesnsp_transform`, una process_task por archivo de corte.
Incremental: si el mismo archivo ya se proceso con SUCCESS se omite
(los cortes nuevos traen nombre de archivo nuevo); --force reprocesa.
"""

import re
from datetime import date
from pathlib import Path

import polars as pl
from sqlalchemy import Engine, delete, select

from src.etl.loaders.sesnsp import (
    DELITO_KEY_COLUMNS,
    DELITOS_TABLE,
    validate_delitos_del_csv,
    validate_municipios_del_csv,
)
from src.meta.tracker import MetadataTracker
from src.shared.db.base import get_table
from src.shared.db.copy import copy_dataframe
from src.shared.enums import JobStatus, TaskStatus
from src.shared.logging import get_stage_logger

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
        engine: Engine | None = None,
    ):
        """
        Args:
            data_root: Raiz de datos (contiene bronze/ y seeds/).
            tracker: MetadataTracker; por default, uno nuevo.
            engine: conexion a la base; por default la del proyecto.
        """
        self.data_root = Path(data_root)
        self.bronze_dir = self.data_root / "bronze" / "sesnsp"
        self.seeds_dir = self.data_root / "seeds"
        self.tracker = tracker or MetadataTracker()
        self._engine = engine

    @property
    def engine(self) -> Engine:
        """Engine de la base, resuelto la primera vez que se usa."""
        if self._engine is None:
            from src.shared.db.session import get_engine

            self._engine = get_engine()
        return self._engine

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
        return self.tracker.has_successful_process_task(csv_path.name, self.TARGET_TABLE)

    def _leer_csv(self, csv_path: Path) -> pl.DataFrame:
        """Lee el CSV ancho del corte y deriva sus claves.

        El SESNSP publica un archivo ancho: una fila por municipio y delito,
        con doce columnas de mes. Aqui solo se leen y se normalizan las
        claves; el paso a formato largo va aparte.

        `cve_mun` sale modulo 1000 porque el SESNSP concatena entidad y
        municipio en un solo numero.

        Args:
            csv_path: archivo del corte.

        Returns:
            pl.DataFrame: ancho, con cve_ent, cve_mun, anio y los delitos.
        """
        encoding = detect_csv_encoding(csv_path)
        logger.info(f"   Encoding detectado: {encoding}")

        crudo = pl.read_csv(
            csv_path,
            encoding=encoding,
            infer_schema_length=20_000,
            null_values=["", "NA", "ND"],
        )

        meses = [nombre for nombre, _ in MONTH_COLUMNS]
        return crudo.select(
            pl.col("Año").cast(pl.Int32, strict=False).alias("anio"),
            pl.col("Clave_Ent")
            .cast(pl.Int32, strict=False)
            .cast(pl.Utf8)
            .str.pad_start(2, "0")
            .alias("cve_ent"),
            (pl.col("Cve. Municipio").cast(pl.Int32, strict=False) % 1000)
            .cast(pl.Utf8)
            .str.pad_start(3, "0")
            .alias("cve_mun"),
            *[
                pl.col(origen).str.strip_chars().alias(destino)
                for origen, destino in CSV_DELITO_COLUMNS.items()
            ],
            # `strict=False` y no un cast duro: si un corte futuro pone un
            # token no numerico en un mes, la celda queda NULL y se filtra
            # despues, en vez de tumbar el corte entero.
            *[pl.col(m).cast(pl.Int32, strict=False).alias(m) for m in meses],
        )

    def _a_formato_largo(self, ancho: pl.DataFrame, delitos: pl.DataFrame) -> pl.DataFrame:
        """Pasa de doce columnas de mes a una fila por mes.

        Args:
            ancho: salida de `_leer_csv`.
            delitos: catalogo, con su `id` y la clave natural.

        Returns:
            pl.DataFrame: listo para el COPY, con delito_id y fecha.
        """
        meses = {nombre: numero for nombre, numero in MONTH_COLUMNS}

        largo = ancho.unpivot(
            on=list(meses),
            index=["anio", "cve_ent", "cve_mun", *CSV_DELITO_COLUMNS.values()],
            variable_name="mes_nombre",
            value_name="carpetas",
        ).drop_nulls("carpetas")

        return (
            largo.with_columns(
                mes=pl.col("mes_nombre").replace_strict(meses, return_dtype=pl.Int32)
            )
            .join(delitos, on=DELITO_KEY_COLUMNS, how="inner")
            .select(
                "cve_ent",
                "cve_mun",
                pl.col("id").alias("delito_id"),
                pl.date("anio", "mes", 1).alias("fecha"),
                "carpetas",
            )
        )

    def _load_csv(self, conn, csv_path: Path) -> tuple[int, int]:
        """
        Valida los linkeos del corte y reconstruye la tabla de hechos.

        Se reconstruye completa en cada corte porque el SESNSP revisa cifras
        historicas: el corte de junio corrige meses de hace anos. Un
        incremental dejaria las correcciones fuera.

        Args:
            conn: conexion con transaccion abierta por el llamador.
            csv_path: archivo del corte.

        Returns:
            tuple: (0 -- el catalogo no se modifica aqui, filas cargadas).
        """
        ancho = self._leer_csv(csv_path)

        # Linkeo contra los catalogos, que la migracion sembro desde los
        # parquet: un delito o municipio nuevo se reporta para darlo de
        # alta, no se auto-inventa.
        validate_delitos_del_csv(conn, ancho.select(DELITO_KEY_COLUMNS).unique())
        validate_municipios_del_csv(conn, ancho.select(["cve_ent", "cve_mun"]).unique())

        catalogo = get_table(DELITOS_TABLE)
        delitos = pl.DataFrame(
            conn.execute(
                select(catalogo.c["id"], *[catalogo.c[c] for c in DELITO_KEY_COLUMNS])
            ).all(),
            schema=["id", *DELITO_KEY_COLUMNS],
            orient="row",
        )

        largo = self._a_formato_largo(ancho, delitos)

        hechos = get_table(self.TARGET_TABLE)
        conn.execute(delete(hechos))
        copy_dataframe(conn, largo, self.TARGET_TABLE)
        return 0, largo.height

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
                'Corre primero: make extract (o make extract ARGS="--only sesnsp")'
            )
            return False

        if not force and self._already_processed(csv_path):
            logger.info(f"✨ Corte ya procesado: {csv_path.name}. Nada por transformar.")
            return True

        # No hace falta respaldar nada antes de escribir: el DELETE y el
        # COPY van en una transaccion, asi que un fallo deja la tabla como
        # estaba.

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

        try:
            # El esquema y los catalogos (delitos, geografia) los crea y
            # siembra la migracion desde los seeds parquet; aqui solo se
            # cargan hechos y se validan sus linkeos. Todo en una
            # transaccion: el DELETE y el COPY se ven juntos o no se ven.
            with self.engine.begin() as conn:
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
                target_location=f"silver.{self.TARGET_TABLE}",
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
