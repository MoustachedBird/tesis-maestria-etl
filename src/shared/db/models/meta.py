"""Modelos ORM de la capa `meta` — observabilidad y control del pipeline.

Reemplaza las 451 líneas de DDL en strings de `src/meta/schema.py`, que no
tenían ORM ni migraciones: el diagnóstico 1.3 de `arquitectura.md`. Al vivir
en el schema `meta` de la MISMA base, el historial de corridas puede hacer
JOIN con los datos que describe (`meta.process_tasks` →
`silver.establecimientos` por periodo), que es la razón de fondo para no
tener una base de metadatos aparte.

Dos cosas que cambian respecto al esquema de DuckDB, además del motor:

**Las FKs ahora existen.** El DDL anterior las documentaba como ausentes a
propósito — *"Foreign key constraints removed due to DuckDB UPDATE
limitations; referential integrity is maintained at application level"*. Al
traducir el esquema se confirmó el motivo concreto: DuckDB rechaza el DDL
con *"FOREIGN KEY constraints cannot use CASCADE, SET NULL or SET DEFAULT"*.
PostgreSQL no tiene esa limitación, así que cada tabla de tareas referencia
su `job_runs.run_id` de verdad, con `ON DELETE CASCADE`: borrar una corrida
se lleva sus tareas y su linaje, en vez de dejar huérfanos que nadie
detecta.

Consecuencia práctica mientras la suite siga sobre DuckDB: un
`Base.metadata.create_all()` sin filtrar falla al llegar aquí. Los tests del
esquema de datos usan `tables_for(DEFAULT_SCHEMA)` para crear solo `silver`.

**Los estados son enums de Python, no VARCHAR libres.** Se guardan como
texto (los valores de `JobStatus`/`TaskStatus`, idénticos a los de hoy) pero
el tipo los valida al escribir. Un `status='FINISHED'` deja de ser posible.
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.db.base import Base
from src.shared.enums import Layer

SCHEMA = Layer.META.value
"""Schema de todas las tablas de este módulo.

Explícito porque el MetaData tiene `silver` como default (ver
`base.py:DEFAULT_SCHEMA`); sin esto las tablas de control caerían entre las
de datos.
"""

_RUN_FK = f"{SCHEMA}.job_runs.run_id"
"""Target calificado de la FK hacia `job_runs`.

Las FKs sin calificar se resuelven en el schema por defecto del MetaData
(`silver`), así que las de este módulo tienen que llevar el schema a mano.
"""


class JobRun(Base):
    """Una corrida completa del pipeline: la unidad que el usuario dispara.

    `make pipeline`, un tick del cron semanal o un `POST /ops/runs` crean
    exactamente una fila aquí, y todas las tareas que generen cuelgan de
    ella. Es la tabla que alimenta el dashboard de "ETL runs" en Grafana.
    """

    __tablename__ = "job_runs"
    __table_args__ = (
        # El acceso real es "las últimas N corridas"; un btree se escanea
        # en reverso, así que no hace falta declararlo DESC.
        Index("ix_job_runs_run_date", "run_date"),
        Index("ix_job_runs_status", "status"),
        Index("ix_job_runs_job_name", "job_name"),
        Index("ix_job_runs_job_type", "job_type"),
        {"schema": SCHEMA},
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True, comment="UUID de la corrida")
    job_name: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="Nombre del job (p. ej. denue_downloader)"
    )
    job_type: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="DOWNLOAD, PROCESS, CLEE_RESOLUTION, LOAD"
    )
    run_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="Momento en que se programó"
    )
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float | None] = mapped_column(Double)
    status: Mapped[str] = mapped_column(String(20), nullable=False, comment="Valor de JobStatus")
    trigger_type: Mapped[str | None] = mapped_column(
        String(20), comment="Valor de TriggerType: MANUAL, SCHEDULED, RETRY"
    )
    triggered_by: Mapped[str | None] = mapped_column(String(50))

    # Métricas agregadas de las tareas hijas
    total_tasks: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tasks_succeeded: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tasks_failed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tasks_skipped: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    total_bytes_downloaded: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    avg_download_speed_mbps: Mapped[float | None] = mapped_column(Double)

    config: Mapped[str | None] = mapped_column(
        Text, comment="Snapshot de la configuración de la corrida (JSON)"
    )
    error_summary: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    download_tasks: Mapped[list["DownloadTask"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    process_tasks: Mapped[list["ProcessTask"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    normalize_tasks: Mapped[list["NormalizeTask"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    clee_resolution_tasks: Mapped[list["CleeResolutionTask"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<JobRun({self.job_name}, {self.status}, run_id={self.run_id})>"


class DownloadTask(Base):
    """La descarga de un archivo: un ZIP de DENUE o el CSV de SESNSP.

    Granularidad de archivo y no de corte porque el modo incremental decide
    archivo por archivo qué saltar: un `SUCCESS` aquí es lo que hace que el
    scraping no vuelva a bajar ese ZIP.
    """

    __tablename__ = "download_tasks"
    __table_args__ = (
        Index("ix_download_tasks_run_id", "run_id"),
        Index("ix_download_tasks_status", "status"),
        Index("ix_download_tasks_task_type", "task_type"),
        Index("ix_download_tasks_period", "period"),
        Index("ix_download_tasks_error_type", "error_type"),
        # El incremental consulta por URL para saber si ya se descargó.
        Index("ix_download_tasks_source_url", "source_url"),
        {"schema": SCHEMA},
    )

    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey(_RUN_FK, ondelete="CASCADE"), nullable=False
    )

    task_name: Mapped[str] = mapped_column(String(200), nullable=False)
    task_type: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="DOWNLOAD, VALIDATE, CLEANUP"
    )
    task_order: Mapped[int | None] = mapped_column(Integer)

    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    target_path: Mapped[str] = mapped_column(Text, nullable=False)
    period: Mapped[str | None] = mapped_column(String(20))

    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float | None] = mapped_column(Double)
    status: Mapped[str] = mapped_column(String(20), nullable=False, comment="Valor de TaskStatus")

    attempt_number: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    max_retries: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    retry_strategy: Mapped[str | None] = mapped_column(
        String(50), comment="EXPONENTIAL, FIXED, NONE"
    )
    next_retry_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    expected_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256_hash: Mapped[str | None] = mapped_column(String(64))
    expected_sha256: Mapped[str | None] = mapped_column(String(64))
    validation_status: Mapped[str | None] = mapped_column(String(20))
    validation_errors: Mapped[str | None] = mapped_column(Text)

    download_speed_mbps: Mapped[float | None] = mapped_column(Double)
    network_latency_ms: Mapped[int | None] = mapped_column(Integer)

    error_type: Mapped[str | None] = mapped_column(String(50), comment="Valor de ErrorType")
    error_message: Mapped[str | None] = mapped_column(Text)
    error_traceback: Mapped[str | None] = mapped_column(Text)
    is_retryable: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    run: Mapped["JobRun"] = relationship(back_populates="download_tasks")

    def __repr__(self) -> str:
        return f"<DownloadTask({self.task_name}, {self.status})>"


class ProcessTask(Base):
    """El ingest de un archivo: ZIP -> warehouse.

    Una fila por archivo procesado, que es también la unidad del
    incremental: con el particionado por periodo (§2.4), un fallo aquí se
    deshace tirando la partición de staging sin tocar lo ya cargado.
    """

    __tablename__ = "process_tasks"
    __table_args__ = (
        Index("ix_process_tasks_run_id", "run_id"),
        Index("ix_process_tasks_status", "status"),
        Index("ix_process_tasks_period", "period"),
        Index("ix_process_tasks_source_path", "source_path"),
        {"schema": SCHEMA},
    )

    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey(_RUN_FK, ondelete="CASCADE"), nullable=False
    )

    task_name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    target_table: Mapped[str | None] = mapped_column(
        String(100), comment="Tabla destino calificada, p. ej. silver.establecimientos"
    )
    period: Mapped[str | None] = mapped_column(String(20))

    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float | None] = mapped_column(Double)
    status: Mapped[str] = mapped_column(String(20), nullable=False)

    rows_extracted: Mapped[int | None] = mapped_column(BigInteger)
    rows_loaded: Mapped[int | None] = mapped_column(BigInteger)
    rows_rejected: Mapped[int | None] = mapped_column(BigInteger)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)

    error_type: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    run: Mapped["JobRun"] = relationship(back_populates="process_tasks")

    def __repr__(self) -> str:
        return f"<ProcessTask({self.task_name}, {self.status}, rows={self.rows_loaded})>"


class NormalizeTask(Base):
    """La normalización de fechas de un periodo.

    Tracking por periodo y no por archivo porque el UPDATE operaba sobre
    todo el corte a la vez.

    Nota: este stage **desaparece** al migrar a PostgreSQL — el `COPY`
    normaliza la fecha antes de insertar (§4.2). La tabla se conserva para
    el historial de las corridas que sí lo ejecutaron.
    """

    __tablename__ = "normalize_tasks"
    __table_args__ = (
        Index("ix_normalize_tasks_run_id", "run_id"),
        Index("ix_normalize_tasks_status", "status"),
        Index("ix_normalize_tasks_period", "period"),
        {"schema": SCHEMA},
    )

    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey(_RUN_FK, ondelete="CASCADE"), nullable=False
    )

    task_name: Mapped[str] = mapped_column(String(200), nullable=False)
    period: Mapped[date] = mapped_column(
        Date, nullable=False, comment="Periodo normalizado (DATE, no string)"
    )

    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float | None] = mapped_column(Double)
    status: Mapped[str] = mapped_column(String(20), nullable=False)

    rows_total: Mapped[int | None] = mapped_column(BigInteger)
    rows_normalized: Mapped[int | None] = mapped_column(BigInteger)
    rows_unrecognized: Mapped[int | None] = mapped_column(BigInteger)

    error_type: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    run: Mapped["JobRun"] = relationship(back_populates="normalize_tasks")

    def __repr__(self) -> str:
        return f"<NormalizeTask({self.period}, {self.status})>"


class CleeResolutionTask(Base):
    """Una pasada de resolución de identidad CLEE sobre un grupo de estados.

    Es la tarea más cara del pipeline y la que más métricas de calidad
    reporta: `entities_matched` frente a `entities_new` es lo que permite
    detectar una regresión del matching sin correr el eval completo.
    """

    __tablename__ = "clee_resolution_tasks"
    __table_args__ = (
        Index("ix_clee_resolution_tasks_run_id", "run_id"),
        Index("ix_clee_resolution_tasks_status", "status"),
        Index("ix_clee_resolution_tasks_region_group", "region_group"),
        {"schema": SCHEMA},
    )

    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey(_RUN_FK, ondelete="CASCADE"), nullable=False
    )

    task_name: Mapped[str] = mapped_column(String(200), nullable=False)
    region_group: Mapped[str | None] = mapped_column(
        String(100), comment="Grupo de regiones o estado único"
    )
    state_list: Mapped[str | None] = mapped_column(Text, comment="Array JSON de estados")

    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float | None] = mapped_column(Double)
    status: Mapped[str] = mapped_column(String(20), nullable=False)

    entities_input: Mapped[int | None] = mapped_column(BigInteger)
    entities_matched: Mapped[int | None] = mapped_column(BigInteger)
    entities_new: Mapped[int | None] = mapped_column(BigInteger)
    master_clees_created: Mapped[int | None] = mapped_column(BigInteger)
    periods_processed: Mapped[int | None] = mapped_column(Integer)
    match_confidence_avg: Mapped[float | None] = mapped_column(Double)

    spatial_matches: Mapped[int | None] = mapped_column(Integer)
    identity_matches: Mapped[int | None] = mapped_column(Integer)
    activity_matches: Mapped[int | None] = mapped_column(Integer)

    output_path: Mapped[str | None] = mapped_column(Text)

    error_type: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    run: Mapped["JobRun"] = relationship(back_populates="clee_resolution_tasks")

    def __repr__(self) -> str:
        return f"<CleeResolutionTask({self.region_group}, {self.status})>"


class DataLineage(Base):
    """De dónde salió cada dato y en qué se convirtió.

    Responde "esta fila de `silver.establecimientos`, ¿de qué ZIP vino y en
    qué corrida?" sin tener que reconstruirlo de los logs. `run_id` y
    `task_id` son opcionales porque hay linaje que no nace de una tarea
    concreta (p. ej. la siembra de un catálogo).
    """

    __tablename__ = "data_lineage"
    __table_args__ = (
        Index("ix_data_lineage_run_id", "run_id"),
        Index("ix_data_lineage_task_id", "task_id"),
        Index("ix_data_lineage_source_type", "source_type"),
        {"schema": SCHEMA},
    )

    lineage_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(36), ForeignKey(_RUN_FK, ondelete="CASCADE"))
    task_id: Mapped[str | None] = mapped_column(
        String(36), comment="Tarea de origen; sin FK porque puede ser de cualquier tipo"
    )

    source_type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="WEB, API, FILE, DATABASE"
    )
    source_location: Mapped[str] = mapped_column(Text, nullable=False)
    source_checksum: Mapped[str | None] = mapped_column(String(64))

    target_type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="FILE, TABLE, OBJECT_STORE"
    )
    target_location: Mapped[str] = mapped_column(Text, nullable=False)
    target_checksum: Mapped[str | None] = mapped_column(String(64))

    transformation: Mapped[str | None] = mapped_column(
        String(100), comment="NONE, DOWNLOAD, EXTRACT, TRANSFORM"
    )

    rows_read: Mapped[int | None] = mapped_column(BigInteger)
    rows_written: Mapped[int | None] = mapped_column(BigInteger)
    bytes_processed: Mapped[int | None] = mapped_column(BigInteger)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<DataLineage({self.source_location} -> {self.target_location})>"
