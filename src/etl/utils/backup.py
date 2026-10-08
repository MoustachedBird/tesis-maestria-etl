"""
Respaldo automatico del warehouse antes de cada comando que lo modifica.

Motivacion (incidente 2026-07-24, ver docs/findings.md): un OOM a media
carga dejo filas parciales y corrompio el indice ART; el reintento
incremental remato la base y la unica salida fue reconstruir 8 horas desde
Bronze. Con un respaldo previo, cualquier corrida que salga mal se revierte
copiando un archivo en vez de rehacer el pipeline.

Diseno:
  - UN respaldo rotativo por base (`<nombre>.backup.duckdb`): cada comando
    sobreescribe el anterior. Es el estado "justo antes de esta corrida".
  - Se hace UNA sola vez por proceso: `make transform` corre varias etapas
    (ingest, normalize-dates, ...) y el respaldo util es el previo al
    comando completo, no el de la ultima etapa.
  - En APFS (macOS) la copia es un clon copy-on-write: 52GB en ~3ms y 0
    bytes extra hasta que los archivos divergen. Fuera de APFS cae a una
    copia normal, que si cuesta tiempo y espacio.
  - NUNCA se sobreescribe el respaldo con una base ilegible: si la fuente
    no abre, se conserva el respaldo anterior (que es justo el que salva).
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import duckdb

from src.shared.config import settings
from src.shared.logging import get_stage_logger

logger = get_stage_logger(__name__, Path("./data/etl_backup.log"))

_ya_respaldadas: set[Path] = set()
"""Bases ya respaldadas en ESTE proceso (evita repetir por cada etapa)."""


def backup_path_for(db_path: Path | str) -> Path:
    """Ruta del respaldo rotativo de una base: `<nombre>.backup.duckdb`."""
    db_path = Path(db_path)
    return db_path.with_suffix(".backup.duckdb")


def _checkpoint(db_path: Path) -> bool:
    """Vuelca el WAL pendiente al archivo principal.

    Sin esto el clon podria quedar sin los ultimos commits (viven en el
    `.wal`, que es un archivo aparte). Devuelve False si la base no se
    puede abrir para escritura -- senal de que no conviene respaldarla.
    """
    try:
        conn = duckdb.connect(str(db_path))
        try:
            conn.execute("CHECKPOINT")
        finally:
            conn.close()
        return True
    except duckdb.Error as e:
        logger.warning(f"⚠️ No se pudo hacer CHECKPOINT de {db_path.name}: {e}")
        return False


def _es_legible(db_path: Path) -> bool:
    """Verifica que la base abra y responda una consulta trivial.

    Guarda contra el peor escenario del respaldo rotativo: pisar una copia
    sana con una base ya corrupta. No detecta corrupcion de indices ART
    (eso solo aparece al escribir), pero si detecta el archivo invalidado
    o truncado, que es el caso en el que el respaldo importa.
    """
    try:
        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        return True
    except duckdb.Error as e:
        logger.error(f"❌ {db_path.name} no es legible ({e}); se conserva el respaldo anterior")
        return False


def _copiar(origen: Path, destino: Path) -> str:
    """Copia origen->destino, con clon APFS si el sistema lo soporta.

    Devuelve el metodo usado ("clon APFS" | "copia"). `cp -c` pide clon
    copy-on-write: instantaneo y sin consumir espacio hasta que los
    archivos divergen. Si el volumen no es APFS, cp -c falla y se cae a
    una copia byte a byte.
    """
    try:
        subprocess.run(
            ["cp", "-c", str(origen), str(destino)],
            check=True,
            capture_output=True,
        )
        return "clon APFS"
    except (subprocess.CalledProcessError, FileNotFoundError):
        shutil.copyfile(origen, destino)
        return "copia"


def backup_warehouse(db_path: Path | str, *, stage: str = "") -> Path | None:
    """
    Respalda la base ANTES de modificarla; sobreescribe el respaldo previo.

    Idempotente por proceso: la primera etapa que escribe dispara el
    respaldo y las siguientes lo reutilizan, de modo que el archivo
    guardado es el estado previo al COMANDO completo.

    Args:
        db_path: Base a respaldar (Silver o Gold).
        stage: Nombre de la etapa que lo solicita (solo para el log).

    Returns:
        Ruta del respaldo, o None si se omitio (deshabilitado, base
        inexistente o ilegible).
    """
    db_path = Path(db_path)

    if not settings.WAREHOUSE_BACKUP_ENABLED:
        return None
    if db_path in _ya_respaldadas:
        return backup_path_for(db_path)
    if not db_path.exists() or db_path.stat().st_size == 0:
        # Primera corrida: no hay nada que perder todavia.
        _ya_respaldadas.add(db_path)
        return None

    destino = backup_path_for(db_path)
    # Temporal en el MISMO directorio: el reemplazo final es atomico solo
    # dentro del mismo sistema de archivos, y asi un fallo a media copia
    # nunca deja el respaldo bueno a medias.
    temporal = destino.with_suffix(".tmp")

    _checkpoint(db_path)
    if not _es_legible(db_path):
        return None

    gb = db_path.stat().st_size / 1024**3
    inicio = time.perf_counter()
    try:
        temporal.unlink(missing_ok=True)
        metodo = _copiar(db_path, temporal)
        os.replace(temporal, destino)
    except OSError as e:
        temporal.unlink(missing_ok=True)
        logger.warning(f"⚠️ No se pudo respaldar {db_path.name}: {e} (se continua sin respaldo)")
        return None

    _ya_respaldadas.add(db_path)
    etapa = f" [{stage}]" if stage else ""
    logger.info(
        f"🛟 Respaldo{etapa}: {db_path.name} -> {destino.name} "
        f"({gb:.1f} GB, {metodo}, {time.perf_counter() - inicio:.2f}s)"
    )
    return destino
