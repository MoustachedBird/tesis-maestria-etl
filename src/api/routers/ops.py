"""Endpoints de operacion: que corrio, cuando y como termino.

Leen el schema `meta` a traves de `MetadataTracker`, el mismo que escribe el
ETL. Que la lectura y la escritura compartan una sola definicion de las
consultas es lo que evita que el dashboard muestre una cosa y el pipeline
registre otra.

Todo aqui es de solo lectura. El API nunca dispara una corrida: el disparo
manual se hara encolando en `meta.run_requests` para que lo tome el
scheduler (Fase 7), nunca hablandole a Docker desde un proceso web.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from src.meta.tracker import MetadataTracker
from src.shared.enums import JobStatus

router = APIRouter(prefix="/ops", tags=["ops"])

#: Corridas que devuelve `GET /ops/runs` sin paginar.
DEFAULT_RUN_LIMIT = 20

#: Tope duro de la pagina, para que un `?limit=100000` no barra la tabla.
MAX_RUN_LIMIT = 200


def get_tracker() -> MetadataTracker:
    """Tracker para la peticion en curso.

    Construirlo es barato --solo guarda la fabrica de sesiones-- asi que no
    hace falta compartir una instancia entre peticiones.

    Returns:
        MetadataTracker: lector del schema `meta`.
    """
    return MetadataTracker()


Tracker = Annotated[MetadataTracker, Depends(get_tracker)]


@router.get("/runs")
def list_runs(
    tracker: Tracker,
    limit: int = Query(DEFAULT_RUN_LIMIT, ge=1, le=MAX_RUN_LIMIT),
) -> list[dict[str, Any]]:
    """
    Las corridas mas recientes, de la mas nueva a la mas vieja.

    Args:
        tracker: lector del schema `meta`.
        limit: cuantas devolver.

    Returns:
        list[dict]: corridas con sus metricas agregadas.
    """
    return tracker.get_recent_runs(limit=limit)


@router.get("/runs/{run_id}")
def get_run(run_id: str, tracker: Tracker) -> dict[str, Any]:
    """
    Una corrida, con el conteo real de sus tareas.

    Los campos `actual_*` se recalculan desde las tareas en vez de leer los
    agregados de la corrida: si difieren, la corrida murio antes de
    consolidar sus metricas.

    Args:
        run_id: identificador de la corrida.
        tracker: lector del schema `meta`.

    Returns:
        dict: la corrida.

    Raises:
        HTTPException: 404 si no existe.
    """
    run = tracker.get_run_summary(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"No existe la corrida {run_id}")
    return run


@router.get("/runs/{run_id}/tasks")
def get_run_tasks(run_id: str, tracker: Tracker) -> list[dict[str, Any]]:
    """
    Las descargas de una corrida, en orden de creacion.

    Args:
        run_id: identificador de la corrida.
        tracker: lector del schema `meta`.

    Returns:
        list[dict]: tareas de descarga.

    Raises:
        HTTPException: 404 si la corrida no existe.
    """
    if tracker.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail=f"No existe la corrida {run_id}")
    return tracker.get_tasks_by_run(run_id)


@router.get("/health/pipeline")
def pipeline_health(tracker: Tracker) -> dict[str, Any]:
    """
    Si la ultima corrida de cada etapa termino bien.

    Es la sonda que mira un monitor externo: responde 200 siempre --el
    endpoint esta sano aunque el pipeline no-- y el veredicto va en el
    cuerpo. Mezclar las dos cosas haria que un fallo del ETL se viera igual
    que un API caido.

    Args:
        tracker: lector del schema `meta`.

    Returns:
        dict: `healthy` global y el detalle por etapa.
    """
    # Solo las etapas que algun job escribe de verdad. Una etapa que no
    # puede existir en `meta.job_runs` reportaria null para siempre y haria
    # ruido en la sonda.
    etapas = {
        "extract": ("DOWNLOAD",),
        "transform": ("TRANSFORM",),
        "clee_resolution": ("CLEE_RESOLUTION",),
    }

    detalle: dict[str, Any] = {}
    for etapa, tipos in etapas.items():
        run = tracker.get_latest_run_by_type(*tipos)
        if run is None:
            detalle[etapa] = {"status": None, "last_run": None, "run_id": None}
            continue
        detalle[etapa] = {
            "status": run["status"],
            "last_run": run["run_date"],
            "run_id": run["run_id"],
            "duration_seconds": run["duration_seconds"],
            "tasks_succeeded": run["tasks_succeeded"],
            "tasks_failed": run["tasks_failed"],
        }

    # Una etapa que nunca corrio no es un fallo: es un pipeline a medio
    # estrenar. Solo cuenta como no sana la que corrio y no termino bien.
    fallidas = [
        etapa
        for etapa, info in detalle.items()
        if info["status"] is not None and info["status"] != JobStatus.SUCCESS.value
    ]

    return {"healthy": not fallidas, "failing_stages": fallidas, "stages": detalle}
