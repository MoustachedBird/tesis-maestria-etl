from enum import StrEnum


class JobStatus(StrEnum):
    """Estado de una corrida completa (`meta.job_runs`).

    Compartido entre el ETL (que lo escribe al abrir y cerrar cada corrida),
    el scheduler (que marca `SKIPPED` cuando el advisory lock está tomado) y
    el API (que lo serializa en `/ops/runs`). Vive en `shared` porque los
    tres lo necesitan sin dependerse entre sí.

    `PARTIAL` existe porque una corrida con varias tareas puede terminar con
    unas exitosas y otras fallidas: es un resultado real del pipeline, no un
    estado intermedio.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


class TaskStatus(StrEnum):
    """Estado de una tarea individual dentro de una corrida.

    Se distingue de `JobStatus` en dos puntos: una tarea puede quedar
    `SKIPPED` (el modo incremental la salta porque ya tiene un `SUCCESS`
    previo) y nunca es `PARTIAL`, porque es la unidad atómica de trabajo.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class TriggerType(StrEnum):
    """Qué originó una corrida (`meta.job_runs.trigger_type`).

    Hoy es texto libre en el tracker; se vuelve enum porque el scheduler
    necesita distinguir sus propias corridas de las manuales para reportar
    el cron semanal, y el API las etiqueta al encolar en `meta.run_requests`.

    `MANUAL` cubre tanto `make pipeline` como `POST /ops/runs`: en ambos casos
    la decisión fue de una persona. `RETRY` queda para un reintento
    automático de tareas fallidas de una corrida previa.
    """

    MANUAL = "MANUAL"
    SCHEDULED = "SCHEDULED"
    RETRY = "RETRY"


class ErrorType(StrEnum):
    """Clasificación de errores de descarga y procesamiento.

    Permite agrupar fallos en los dashboards sin parsear mensajes de error,
    y decidir si una tarea es reintentable: los de red y timeout lo son, los
    de validación y `DISK_FULL` no.

    Los valores no coinciden con los nombres en varios casos (`NETWORK_ERROR`
    → `"NETWORK"`) porque son los que ya están escritos en la metadata
    existente; el nombre es el legible en código y el valor el persistido.
    """

    NETWORK_ERROR = "NETWORK"
    HTTP_404 = "HTTP_404"
    HTTP_500 = "HTTP_500"
    HTTP_TIMEOUT = "TIMEOUT"
    VALIDATION_ERROR = "VALIDATION"
    DISK_FULL = "DISK_FULL"
    ZIP_CORRUPT = "ZIP_CORRUPT"
    HASH_MISMATCH = "HASH_MISMATCH"
    SIZE_MISMATCH = "SIZE_MISMATCH"
    UNKNOWN = "UNKNOWN"
