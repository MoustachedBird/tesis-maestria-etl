"""
Registro de corridas, tareas y linaje del pipeline.

Escribe en el schema `meta` de la misma base que los datos, a traves de los
modelos ORM de `shared/db/models/meta.py`. Que viva en la misma base no es un
detalle de despliegue: permite que el historial haga JOIN con lo que
describe (`meta.process_tasks` -> `silver.establecimientos` por periodo), que
es la razon de no tener una base de metadatos aparte.

Las cuatro familias de tarea --descarga, proceso, resolucion CLEE y
normalizacion-- comparten el mismo ciclo: se crean PENDING, pasan a RUNNING
con su `start_time` y terminan con un estado final, una duracion y sus
metricas propias. Ese ciclo se implementa una vez en `_create_task`,
`_start_task` y `_complete_task`; los metodos publicos solo dicen de que
tabla se trata y que metricas lleva.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, sessionmaker

from src.shared.db.models.meta import (
    CleeResolutionTask,
    DataLineage,
    DownloadTask,
    JobRun,
    NormalizeTask,
    ProcessTask,
)
from src.shared.enums import ErrorType, JobStatus, TaskStatus

#: Tipo de tarea que se escribe en `download_tasks.task_type`. Unico valor
#: en uso: la tabla nacio preparada para VALIDATE y CLEANUP que nunca se
#: implementaron.
DOWNLOAD_TASK_TYPE = "DOWNLOAD"


def _as_dict(obj) -> dict[str, Any]:
    """Fila ORM como diccionario plano.

    El contrato publico del tracker son diccionarios, no entidades ORM: los
    stages lo consumen asi y el API los serializa directo.

    Args:
        obj: instancia de un modelo declarativo.

    Returns:
        dict: columnas y valores de la fila.
    """
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


class MetadataTracker:
    """
    Ciclo de vida de corridas y tareas del pipeline.

    Cada metodo abre y cierra su propia transaccion, de modo que lo que se
    registra sobrevive aunque la etapa que lo invoco falle despues. Es
    deliberado: el historial de una corrida fallida es justamente lo que se
    quiere conservar.
    """

    def __init__(self, session_factory: sessionmaker[Session] | None = None):
        """
        Args:
            session_factory: fabrica de sesiones; por default la del
                proyecto. Se inyecta en los tests para apuntar a la base
                de pruebas.
        """
        if session_factory is None:
            from src.shared.db.session import get_session_factory

            session_factory = get_session_factory()
        self._session_factory = session_factory

    @contextmanager
    def _session(self) -> Iterator[Session]:
        """Sesion con commit al salir y rollback ante excepcion."""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ========================================================================
    # CORRIDAS
    # ========================================================================

    def create_run(
        self,
        job_name: str,
        job_type: str = "DOWNLOAD",
        trigger_type: str = "MANUAL",
        triggered_by: str = "system",
        config: dict[str, Any] | None = None,
    ) -> str:
        """
        Abre una corrida y devuelve su identificador.

        Args:
            job_name: nombre del job (p. ej. "denue_downloader").
            job_type: DOWNLOAD, PROCESS, CLEE_RESOLUTION, LOAD.
            trigger_type: MANUAL, SCHEDULED, RETRY.
            triggered_by: quien o que la disparo.
            config: configuracion de la corrida, se guarda como JSON.

        Returns:
            str: run_id (UUID).
        """
        run_id = str(uuid.uuid4())
        now = datetime.now()
        with self._session() as session:
            session.add(
                JobRun(
                    run_id=run_id,
                    job_name=job_name,
                    job_type=job_type,
                    run_date=now,
                    start_time=now,
                    status=JobStatus.RUNNING.value,
                    trigger_type=trigger_type,
                    triggered_by=triggered_by,
                    config=json.dumps(config) if config else None,
                )
            )
        return run_id

    def complete_run(
        self,
        run_id: str,
        status: JobStatus | None = None,
        error_summary: str | None = None,
    ) -> None:
        """
        Cierra una corrida y consolida las metricas de sus descargas.

        Si no se pasa `status`, se deduce del resultado de las tareas: sin
        tareas o sin fallos es SUCCESS, sin exitos es FAILED, y una mezcla
        es PARTIAL.

        Args:
            run_id: corrida a cerrar.
            status: estado final explicito.
            error_summary: resumen del error, si fallo.
        """
        with self._session() as session:
            run = session.get(JobRun, run_id)
            if run is None:
                return

            stats = session.execute(
                select(
                    func.count(DownloadTask.task_id),
                    func.coalesce(
                        func.sum(case((DownloadTask.status == TaskStatus.SUCCESS.value, 1), else_=0)),
                        0,
                    ),
                    func.coalesce(
                        func.sum(case((DownloadTask.status == TaskStatus.FAILED.value, 1), else_=0)),
                        0,
                    ),
                    func.coalesce(
                        func.sum(case((DownloadTask.status == TaskStatus.SKIPPED.value, 1), else_=0)),
                        0,
                    ),
                    func.coalesce(func.sum(DownloadTask.file_size_bytes), 0),
                    func.avg(DownloadTask.download_speed_mbps),
                ).where(DownloadTask.run_id == run_id)
            ).one()
            total, succeeded, failed, skipped, total_bytes, avg_speed = stats

            if status is None:
                if total == 0 or failed == 0:
                    status = JobStatus.SUCCESS
                elif succeeded == 0:
                    status = JobStatus.FAILED
                else:
                    status = JobStatus.PARTIAL

            end_time = datetime.now()
            run.end_time = end_time
            run.duration_seconds = (
                (end_time - run.start_time).total_seconds() if run.start_time else 0.0
            )
            run.status = status.value
            run.total_tasks = total
            run.tasks_succeeded = succeeded
            run.tasks_failed = failed
            run.tasks_skipped = skipped
            run.total_bytes_downloaded = total_bytes
            run.avg_download_speed_mbps = avg_speed
            run.error_summary = error_summary

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """
        Una corrida por su identificador.

        Args:
            run_id: identificador de la corrida.

        Returns:
            dict | None: la fila, o None si no existe.
        """
        with self._session() as session:
            run = session.get(JobRun, run_id)
            return _as_dict(run) if run else None

    def get_recent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        """
        Las corridas mas recientes, de la mas nueva a la mas vieja.

        Args:
            limit: cuantas devolver.

        Returns:
            list[dict]: corridas.
        """
        with self._session() as session:
            runs = session.scalars(
                select(JobRun).order_by(JobRun.run_date.desc()).limit(limit)
            ).all()
            return [_as_dict(r) for r in runs]

    def get_run_summary(self, run_id: str) -> dict[str, Any]:
        """
        Una corrida con el conteo real de sus descargas.

        Los campos `actual_*` se recalculan desde `download_tasks` en vez de
        leer los agregados de `job_runs`: permiten detectar una corrida cuyo
        `complete_run` no llego a ejecutarse.

        Args:
            run_id: identificador de la corrida.

        Returns:
            dict: la corrida mas `actual_total_tasks`, `actual_succeeded` y
            `actual_failed`; vacio si la corrida no existe.
        """
        with self._session() as session:
            run = session.get(JobRun, run_id)
            if run is None:
                return {}

            total, succeeded, failed = session.execute(
                select(
                    func.count(DownloadTask.task_id),
                    func.coalesce(
                        func.sum(case((DownloadTask.status == TaskStatus.SUCCESS.value, 1), else_=0)),
                        0,
                    ),
                    func.coalesce(
                        func.sum(case((DownloadTask.status == TaskStatus.FAILED.value, 1), else_=0)),
                        0,
                    ),
                ).where(DownloadTask.run_id == run_id)
            ).one()

            return {
                **_as_dict(run),
                "actual_total_tasks": total,
                "actual_succeeded": succeeded,
                "actual_failed": failed,
            }

    # ========================================================================
    # CICLO DE VIDA COMUN A TODA TAREA
    # ========================================================================

    def _create_task(self, model, **fields) -> str:
        """Inserta una tarea en PENDING y devuelve su id.

        Args:
            model: clase ORM de la tabla de tareas.
            **fields: columnas propias de esa familia de tarea.

        Returns:
            str: task_id (UUID).
        """
        task_id = str(uuid.uuid4())
        with self._session() as session:
            session.add(model(task_id=task_id, status=TaskStatus.PENDING.value, **fields))
        return task_id

    def _start_task(self, model, task_id: str, **fields) -> None:
        """Pasa una tarea a RUNNING y le pone `start_time`.

        Args:
            model: clase ORM de la tabla de tareas.
            task_id: tarea a marcar.
            **fields: columnas adicionales (p. ej. `attempt_number`).
        """
        with self._session() as session:
            task = session.get(model, task_id)
            if task is None:
                return
            task.status = TaskStatus.RUNNING.value
            task.start_time = datetime.now()
            for key, value in fields.items():
                setattr(task, key, value)

    def _complete_task(self, model, task_id: str, status: TaskStatus, **fields) -> None:
        """Cierra una tarea calculando su duracion desde `start_time`.

        Los campos en `None` no se escriben: permite completar una tarea
        informando solo las metricas que aplican, sin borrar las que ya
        tenia.

        Args:
            model: clase ORM de la tabla de tareas.
            task_id: tarea a cerrar.
            status: estado final.
            **fields: metricas propias de esa familia de tarea.
        """
        with self._session() as session:
            task = session.get(model, task_id)
            if task is None:
                return
            end_time = datetime.now()
            task.status = status.value
            task.end_time = end_time
            if task.start_time:
                task.duration_seconds = (end_time - task.start_time).total_seconds()
            for key, value in fields.items():
                if value is not None:
                    setattr(task, key, value)

    # ========================================================================
    # TAREAS DE DESCARGA
    # ========================================================================

    def create_task(
        self,
        run_id: str,
        task_name: str,
        source_url: str,
        target_path: str,
        period: str | None = None,
        max_retries: int = 5,
    ) -> str:
        """
        Registra la descarga pendiente de un archivo.

        Args:
            run_id: corrida padre.
            task_name: nombre legible.
            source_url: URL de origen.
            target_path: ruta local de destino.
            period: corte al que pertenece (p. ej. "2024_11").
            max_retries: tope de reintentos.

        Returns:
            str: task_id (UUID).
        """
        return self._create_task(
            DownloadTask,
            run_id=run_id,
            task_name=task_name,
            task_type=DOWNLOAD_TASK_TYPE,
            source_url=source_url,
            target_path=target_path,
            period=period,
            max_retries=max_retries,
        )

    def start_task(self, task_id: str, attempt_number: int = 1) -> None:
        """
        Marca una descarga como en curso.

        Args:
            task_id: tarea a marcar.
            attempt_number: numero de intento (para reintentos).
        """
        self._start_task(DownloadTask, task_id, attempt_number=attempt_number)

    def complete_task(
        self,
        task_id: str,
        status: TaskStatus,
        result: Any | None = None,
        actual_size_bytes: int | None = None,
        actual_hash: str | None = None,
        download_speed_mbps: float | None = None,
        validation_passed: bool | None = None,
        error_type: ErrorType | None = None,
        error_message: str | None = None,
        duration_seconds: float | None = None,
        attempt_number: int | None = None,
    ) -> None:
        """
        Cierra una descarga con sus metricas.

        Acepta un `DownloadResult` o los campos sueltos; el objeto gana si
        se pasan ambos.

        Args:
            task_id: tarea a cerrar.
            status: estado final.
            result: `DownloadResult` con las metricas.
            actual_size_bytes: tamano del archivo.
            actual_hash: SHA256 calculado.
            download_speed_mbps: velocidad media.
            validation_passed: si la validacion paso.
            error_type: clasificacion del error.
            error_message: detalle del error.
            duration_seconds: duracion medida por el llamador (se ignora:
                la duracion se calcula desde `start_time`).
            attempt_number: numero de intento.
        """
        if result is not None:
            actual_size_bytes = result.file_size_bytes
            actual_hash = result.sha256_hash
            download_speed_mbps = result.download_speed_mbps
            validation_passed = result.validation_passed
            error_type = result.error_type
            error_message = result.error_message
            attempt_number = result.attempt_number

        validation_status = None
        if validation_passed is not None:
            validation_status = "PASSED" if validation_passed else "FAILED"

        self._complete_task(
            DownloadTask,
            task_id,
            status,
            file_size_bytes=actual_size_bytes,
            sha256_hash=actual_hash,
            download_speed_mbps=download_speed_mbps,
            validation_status=validation_status,
            error_type=error_type.value if error_type else None,
            error_message=error_message,
            attempt_number=attempt_number,
        )

    def get_tasks_by_run(self, run_id: str) -> list[dict[str, Any]]:
        """
        Todas las descargas de una corrida, en orden de creacion.

        Args:
            run_id: corrida.

        Returns:
            list[dict]: tareas.
        """
        with self._session() as session:
            tasks = session.scalars(
                select(DownloadTask)
                .where(DownloadTask.run_id == run_id)
                .order_by(DownloadTask.created_at)
            ).all()
            return [_as_dict(t) for t in tasks]

    def get_successful_download_urls(self) -> set[str]:
        """
        URLs que alguna vez se descargaron con exito, de cualquier corrida.

        Cruza TODAS las corridas a proposito: el scraper la usa para saltarse
        lo ya descargado, y filtrar por la corrida en curso --que al momento
        del scrape esta vacia-- hacia que cada corrida re-registrara desde
        disco los cientos de archivos ya presentes.

        Returns:
            set[str]: URLs con al menos una descarga SUCCESS.
        """
        with self._session() as session:
            urls = session.scalars(
                select(DownloadTask.source_url)
                .where(
                    DownloadTask.status == TaskStatus.SUCCESS.value,
                    DownloadTask.source_url.is_not(None),
                )
                .distinct()
            ).all()
            return set(urls)

    # ========================================================================
    # TAREAS DE PROCESO (ZIP -> Silver)
    # ========================================================================

    def create_process_task(
        self,
        run_id: str,
        task_name: str,
        source_path: str,
        period: str | None = None,
        target_table: str | None = None,
    ) -> str:
        """
        Registra el procesamiento pendiente de un archivo.

        Args:
            run_id: corrida padre.
            task_name: nombre legible.
            source_path: ruta del ZIP de origen.
            period: corte al que pertenece.
            target_table: tabla destino.

        Returns:
            str: task_id (UUID).
        """
        return self._create_task(
            ProcessTask,
            run_id=run_id,
            task_name=task_name,
            source_path=source_path,
            target_table=target_table,
            period=period,
        )

    def start_process_task(self, task_id: str) -> None:
        """
        Marca un procesamiento como en curso.

        Args:
            task_id: tarea a marcar.
        """
        self._start_task(ProcessTask, task_id)

    def complete_process_task(
        self,
        task_id: str,
        status: TaskStatus,
        rows_extracted: int | None = None,
        rows_loaded: int | None = None,
        rows_rejected: int | None = None,
        file_size_bytes: int | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """
        Cierra un procesamiento con sus metricas de filas.

        Args:
            task_id: tarea a cerrar.
            status: estado final.
            rows_extracted: filas leidas del ZIP.
            rows_loaded: filas escritas en Silver.
            rows_rejected: filas rechazadas por validacion.
            file_size_bytes: tamano del archivo de origen.
            error_type: clasificacion del error.
            error_message: detalle del error.
        """
        self._complete_task(
            ProcessTask,
            task_id,
            status,
            rows_extracted=rows_extracted,
            rows_loaded=rows_loaded,
            rows_rejected=rows_rejected,
            file_size_bytes=file_size_bytes,
            error_type=error_type,
            error_message=error_message,
        )

    def get_pending_process_tasks(self, include_failed: bool = False) -> list[dict[str, Any]]:
        """
        Archivos descargados con exito que falta procesar.

        Deduplica por ruta en los dos lados. Un mismo ZIP puede tener varias
        descargas SUCCESS --cada `extract` que lo re-sincroniza desde disco
        crea una-- y sin deduplicar, el ingest lo leeria una vez por
        registro.

        Args:
            include_failed: incluir tambien los que fallaron al procesarse.

        Returns:
            list[dict]: con `source_path`, `period` y `download_task_id`.
        """
        with self._session() as session:
            # Ultimo intento de proceso por ruta.
            ultimo_proceso = (
                select(
                    ProcessTask.source_path,
                    ProcessTask.status,
                    func.row_number()
                    .over(
                        partition_by=ProcessTask.source_path,
                        order_by=ProcessTask.created_at.desc(),
                    )
                    .label("rn"),
                )
                .subquery()
            )
            proceso = select(ultimo_proceso).where(ultimo_proceso.c.rn == 1).subquery()

            # Ultima descarga exitosa por ruta.
            ultima_descarga = (
                select(
                    DownloadTask.target_path,
                    DownloadTask.period,
                    DownloadTask.task_id,
                    DownloadTask.created_at,
                    func.row_number()
                    .over(
                        partition_by=DownloadTask.target_path,
                        order_by=DownloadTask.created_at.desc(),
                    )
                    .label("rn"),
                )
                .where(DownloadTask.status == TaskStatus.SUCCESS.value)
                .subquery()
            )

            sin_procesar = proceso.c.source_path.is_(None)
            condicion = (
                sin_procesar | (proceso.c.status == TaskStatus.FAILED.value)
                if include_failed
                else sin_procesar
            )

            filas = session.execute(
                select(
                    ultima_descarga.c.target_path,
                    ultima_descarga.c.period,
                    ultima_descarga.c.task_id,
                )
                .select_from(
                    ultima_descarga.outerjoin(
                        proceso, ultima_descarga.c.target_path == proceso.c.source_path
                    )
                )
                .where(ultima_descarga.c.rn == 1, condicion)
                .order_by(
                    ultima_descarga.c.period.desc(), ultima_descarga.c.created_at.desc()
                )
            ).all()

            return [
                {"source_path": path, "period": period, "download_task_id": task_id}
                for path, period, task_id in filas
            ]

    def get_all_successful_downloads(self) -> list[dict[str, Any]]:
        """
        Toda descarga exitosa, una por ruta de destino.

        El camino de `--force`, que reprocesa todo en vez de solo lo
        pendiente. Deduplica por ruta por la misma razon que
        `get_pending_process_tasks`: un ZIP acumula una descarga SUCCESS por
        cada `extract` que lo re-sincroniza desde disco, y sin deduplicar se
        reprocesaria una vez por registro --horas de ingest para el mismo
        resultado.

        Returns:
            list[dict]: con `source_path`, `period` y `download_task_id`.
        """
        with self._session() as session:
            ultima = (
                select(
                    DownloadTask.target_path,
                    DownloadTask.period,
                    DownloadTask.task_id,
                    func.row_number()
                    .over(
                        partition_by=DownloadTask.target_path,
                        order_by=DownloadTask.created_at.desc(),
                    )
                    .label("rn"),
                )
                .where(DownloadTask.status == TaskStatus.SUCCESS.value)
                .subquery()
            )
            filas = session.execute(
                select(ultima.c.target_path, ultima.c.period, ultima.c.task_id)
                .where(ultima.c.rn == 1)
                .order_by(ultima.c.period.desc())
            ).all()
            return [
                {"source_path": path, "period": period, "download_task_id": task_id}
                for path, period, task_id in filas
            ]

    def has_successful_process_task(self, task_name: str, target_table: str) -> bool:
        """
        Si un archivo ya se proceso con exito hacia una tabla.

        Args:
            task_name: nombre de la tarea (el del archivo de origen).
            target_table: tabla destino.

        Returns:
            bool: True si existe al menos un SUCCESS.
        """
        with self._session() as session:
            return (
                session.scalar(
                    select(ProcessTask.task_id)
                    .where(
                        ProcessTask.task_name == task_name,
                        ProcessTask.target_table == target_table,
                        ProcessTask.status == TaskStatus.SUCCESS.value,
                    )
                    .limit(1)
                )
                is not None
            )

    def get_normalized_periods(self) -> set[str]:
        """
        Cortes cuya normalizacion de fechas ya termino bien.

        Returns:
            set[str]: periodos con al menos una tarea SUCCESS.
        """
        with self._session() as session:
            return set(
                session.scalars(
                    select(NormalizeTask.period)
                    .where(NormalizeTask.status == TaskStatus.SUCCESS.value)
                    .distinct()
                ).all()
            )

    def get_latest_run_by_type(self, *job_types: str) -> dict[str, Any] | None:
        """
        La corrida mas reciente de alguno de los tipos dados.

        Args:
            *job_types: tipos aceptados (p. ej. "TRANSFORM", "PROCESS").

        Returns:
            dict | None: la corrida, o None si no hay ninguna.
        """
        with self._session() as session:
            run = session.scalars(
                select(JobRun)
                .where(JobRun.job_type.in_(job_types))
                .order_by(JobRun.run_date.desc())
                .limit(1)
            ).first()
            return _as_dict(run) if run else None

    # ========================================================================
    # TAREAS DE RESOLUCION CLEE
    # ========================================================================

    def create_clee_resolution_task(
        self,
        run_id: str,
        task_name: str,
        region_group: str,
        state_list: list[str],
    ) -> str:
        """
        Registra la resolucion pendiente de un grupo de estados.

        Args:
            run_id: corrida padre.
            task_name: nombre legible.
            region_group: identificador del grupo.
            state_list: claves de entidad del grupo.

        Returns:
            str: task_id (UUID).
        """
        return self._create_task(
            CleeResolutionTask,
            run_id=run_id,
            task_name=task_name,
            region_group=region_group,
            state_list=json.dumps(state_list),
        )

    def start_clee_resolution_task(self, task_id: str) -> None:
        """
        Marca una resolucion como en curso.

        Args:
            task_id: tarea a marcar.
        """
        self._start_task(CleeResolutionTask, task_id)

    def complete_clee_resolution_task(
        self,
        task_id: str,
        status: TaskStatus,
        entities_input: int | None = None,
        entities_matched: int | None = None,
        entities_new: int | None = None,
        master_clees_created: int | None = None,
        periods_processed: int | None = None,
        match_confidence_avg: float | None = None,
        spatial_matches: int | None = None,
        identity_matches: int | None = None,
        activity_matches: int | None = None,
        output_path: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """
        Cierra una resolucion con sus metricas de matching.

        Args:
            task_id: tarea a cerrar.
            status: estado final.
            entities_input: identidades de entrada.
            entities_matched: identidades emparejadas.
            entities_new: identidades nuevas.
            master_clees_created: CLEEs maestros generados.
            periods_processed: periodos recorridos.
            match_confidence_avg: confianza media del match.
            spatial_matches: emparejamientos por cercania.
            identity_matches: emparejamientos por identidad.
            activity_matches: emparejamientos por actividad.
            output_path: ruta de salida.
            error_type: clasificacion del error.
            error_message: detalle del error.
        """
        self._complete_task(
            CleeResolutionTask,
            task_id,
            status,
            entities_input=entities_input,
            entities_matched=entities_matched,
            entities_new=entities_new,
            master_clees_created=master_clees_created,
            periods_processed=periods_processed,
            match_confidence_avg=match_confidence_avg,
            spatial_matches=spatial_matches,
            identity_matches=identity_matches,
            activity_matches=activity_matches,
            output_path=output_path,
            error_type=error_type,
            error_message=error_message,
        )

    # ========================================================================
    # TAREAS DE NORMALIZACION
    # ========================================================================

    def create_normalize_task(self, run_id: str, task_name: str, period: str) -> str:
        """
        Registra la normalizacion pendiente de un corte.

        Args:
            run_id: corrida padre.
            task_name: nombre legible.
            period: corte a normalizar.

        Returns:
            str: task_id (UUID).
        """
        return self._create_task(
            NormalizeTask, run_id=run_id, task_name=task_name, period=period
        )

    def start_normalize_task(self, task_id: str) -> None:
        """
        Marca una normalizacion como en curso.

        Args:
            task_id: tarea a marcar.
        """
        self._start_task(NormalizeTask, task_id)

    def complete_normalize_task(
        self,
        task_id: str,
        status: TaskStatus,
        rows_total: int | None = None,
        rows_normalized: int | None = None,
        rows_unrecognized: int | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """
        Cierra una normalizacion con sus metricas de filas.

        Args:
            task_id: tarea a cerrar.
            status: estado final.
            rows_total: filas del corte.
            rows_normalized: filas con fecha reconocida.
            rows_unrecognized: filas con formato no reconocido.
            error_type: clasificacion del error.
            error_message: detalle del error.
        """
        self._complete_task(
            NormalizeTask,
            task_id,
            status,
            rows_total=rows_total,
            rows_normalized=rows_normalized,
            rows_unrecognized=rows_unrecognized,
            error_type=error_type,
            error_message=error_message,
        )

    # ========================================================================
    # LINAJE
    # ========================================================================

    def record_lineage(
        self,
        run_id: str,
        task_id: str,
        source_location: str,
        target_location: str,
        source_type: str = "WEB",
        target_type: str = "FILE",
        bytes_processed: int | None = None,
    ) -> None:
        """
        Deja constancia de que un origen produjo un destino.

        Args:
            run_id: corrida.
            task_id: tarea que lo produjo.
            source_location: URL o ruta de origen.
            target_location: ruta de destino.
            source_type: WEB, API, FILE, DATABASE.
            target_type: FILE, TABLE, OBJECT_STORE.
            bytes_processed: bytes movidos.
        """
        with self._session() as session:
            session.add(
                DataLineage(
                    lineage_id=str(uuid.uuid4()),
                    run_id=run_id,
                    task_id=task_id,
                    source_type=source_type,
                    source_location=source_location,
                    target_type=target_type,
                    target_location=target_location,
                    transformation=DOWNLOAD_TASK_TYPE,
                    bytes_processed=bytes_processed,
                )
            )

    # ========================================================================
    # METRICAS AGREGADAS
    # ========================================================================

    def get_process_metrics(self) -> list[dict[str, Any]]:
        """
        Rendimiento del procesamiento, por corte.

        Returns:
            list[dict]: `period`, `total_processed`, `total_rows`,
            `avg_duration_sec`, `avg_rows_loaded`.
        """
        with self._session() as session:
            filas = session.execute(
                select(
                    ProcessTask.period,
                    func.count(ProcessTask.task_id),
                    func.coalesce(func.sum(ProcessTask.rows_loaded), 0),
                    func.avg(ProcessTask.duration_seconds),
                    func.avg(ProcessTask.rows_loaded),
                )
                .where(ProcessTask.status == TaskStatus.SUCCESS.value)
                .group_by(ProcessTask.period)
                .order_by(ProcessTask.period.desc())
            ).all()
            columnas = (
                "period",
                "total_processed",
                "total_rows",
                "avg_duration_sec",
                "avg_rows_loaded",
            )
            return [dict(zip(columnas, fila)) for fila in filas]

    def get_clee_resolution_metrics(self) -> list[dict[str, Any]]:
        """
        Rendimiento de la resolucion CLEE, por grupo de region.

        Returns:
            list[dict]: `region_group`, `total_resolutions`,
            `total_entities`, `total_matched`, `total_new`, `avg_confidence`.
        """
        with self._session() as session:
            filas = session.execute(
                select(
                    CleeResolutionTask.region_group,
                    func.count(CleeResolutionTask.task_id),
                    func.coalesce(func.sum(CleeResolutionTask.entities_input), 0),
                    func.coalesce(func.sum(CleeResolutionTask.entities_matched), 0),
                    func.coalesce(func.sum(CleeResolutionTask.entities_new), 0),
                    func.avg(CleeResolutionTask.match_confidence_avg),
                )
                .where(CleeResolutionTask.status == TaskStatus.SUCCESS.value)
                .group_by(CleeResolutionTask.region_group)
                .order_by(CleeResolutionTask.region_group)
            ).all()
            columnas = (
                "region_group",
                "total_resolutions",
                "total_entities",
                "total_matched",
                "total_new",
                "avg_confidence",
            )
            return [dict(zip(columnas, fila)) for fila in filas]

    def get_normalize_metrics(self) -> list[dict[str, Any]]:
        """
        Rendimiento de la normalizacion de fechas, por corte.

        Returns:
            list[dict]: `period`, `total_normalizations`, `total_rows`,
            `total_normalized`, `total_unrecognized`, `avg_duration_sec`.
        """
        with self._session() as session:
            filas = session.execute(
                select(
                    NormalizeTask.period,
                    func.count(NormalizeTask.task_id),
                    func.coalesce(func.sum(NormalizeTask.rows_total), 0),
                    func.coalesce(func.sum(NormalizeTask.rows_normalized), 0),
                    func.coalesce(func.sum(NormalizeTask.rows_unrecognized), 0),
                    func.avg(NormalizeTask.duration_seconds),
                )
                .where(NormalizeTask.status == TaskStatus.SUCCESS.value)
                .group_by(NormalizeTask.period)
                .order_by(NormalizeTask.period.desc())
            ).all()
            columnas = (
                "period",
                "total_normalizations",
                "total_rows",
                "total_normalized",
                "total_unrecognized",
                "avg_duration_sec",
            )
            return [dict(zip(columnas, fila)) for fila in filas]
