"""Registrar en metadata los ZIP que ya estan en Bronze.

El ingest no mira el disco: lee de `meta.download_tasks` que archivos hay y
a que corte pertenecen. Eso es deliberado --asi una corrida sabe exactamente
que proceso y puede reportarlo-- pero deja un hueco cuando Bronze llega por
otra via que la descarga: una copia desde otra maquina, un respaldo
restaurado, un ZIP puesto a mano.

Sin esto, la unica forma de registrar 484 archivos que ya existen seria
volver a descargarlos de INEGI con Selenium. Esta operacion cierra ese hueco
sin inventar nada: recorre Bronze, deduce el corte del nombre del
directorio, y deja una descarga SUCCESS por archivo.

Es idempotente: un archivo ya registrado se omite, asi que correrlo dos
veces no duplica el historial.
"""

from __future__ import annotations

from pathlib import Path

from src.meta.tracker import MetadataTracker
from src.shared.enums import TaskStatus
from src.shared.logging import get_logger

logger = get_logger(__name__)

#: Esquema de URL que marca un archivo que llego por copia y no por descarga.
#: Que no sea una URL de INEGI es informacion: dice que su procedencia no se
#: puede reconstruir desde el historial.
LOCAL_SCHEME = "file://"


def sync_bronze(
    bronze_dir: Path,
    tracker: MetadataTracker,
    project_root: Path | None = None,
) -> dict[str, int]:
    """Registra los ZIP presentes en Bronze como descargas exitosas.

    Los cortes se deducen de la estructura de directorios que usa el
    extractor: `<bronze>/<corte>/<archivo>.zip`, con el corte en formato
    `MM_AAAA`.

    Las rutas se guardan RELATIVAS a la raiz del proyecto, que es como las
    espera el ingest: resuelve `Path.cwd() / source_path`. Guardarlas
    absolutas haria el historial inservible al cambiar de maquina o de
    contenedor.

    Args:
        bronze_dir: directorio de Bronze (p. ej. `data/bronze/denue`).
        tracker: registro donde dejar las descargas.
        project_root: raiz contra la que relativizar; por default, el
            directorio de trabajo.

    Returns:
        dict: corte -> archivos registrados en esta llamada.

    Raises:
        FileNotFoundError: si el directorio de Bronze no existe.
    """
    bronze_dir = Path(bronze_dir)
    if not bronze_dir.is_dir():
        raise FileNotFoundError(
            f"No existe {bronze_dir}. Bronze se descarga con `extract` o se copia "
            "de otra maquina antes de sincronizar."
        )

    raiz = Path(project_root or Path.cwd())
    ya_registrados = {
        tarea["source_path"] for tarea in tracker.get_all_successful_downloads()
    }

    # Un corte por subdirectorio, en orden para que el log sea legible.
    cortes = sorted(d for d in bronze_dir.iterdir() if d.is_dir())
    if not cortes:
        logger.warning("Bronze sin subdirectorios de corte", bronze=str(bronze_dir))
        return {}

    run_id = tracker.create_run(
        job_name="bronze_sync",
        job_type="DOWNLOAD",
        trigger_type="MANUAL",
        config={"bronze_dir": str(bronze_dir)},
    )

    registrados: dict[str, int] = {}
    omitidos = 0

    for corte in cortes:
        nuevos = 0
        for zip_path in sorted(corte.glob("*.zip")):
            relativa = str(zip_path.resolve().relative_to(raiz.resolve()))
            if relativa in ya_registrados:
                omitidos += 1
                continue

            task_id = tracker.create_task(
                run_id=run_id,
                task_name=zip_path.name,
                source_url=f"{LOCAL_SCHEME}{zip_path.resolve()}",
                target_path=relativa,
                period=corte.name,
            )
            tracker.start_task(task_id)
            tracker.complete_task(
                task_id,
                TaskStatus.SUCCESS,
                actual_size_bytes=zip_path.stat().st_size,
                validation_passed=True,
            )
            nuevos += 1

        if nuevos:
            registrados[corte.name] = nuevos
            logger.info("corte sincronizado", period=corte.name, archivos=nuevos)

    tracker.complete_run(run_id)
    total = sum(registrados.values())
    logger.info(
        "Bronze sincronizado",
        archivos_nuevos=total,
        cortes=len(registrados),
        ya_estaban=omitidos,
    )
    return registrados
