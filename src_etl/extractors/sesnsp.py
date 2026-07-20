"""
SESNSP Extractor - Incidencia delictiva municipal desde datos.gob.mx.

Descarga el CSV consolidado de incidencia delictiva del fuero comun a
nivel municipal (carpetas de investigacion por municipio x delito x mes,
2015+) publicado por el SESNSP bajo licencia CC-BY-4.0.

Por que datos.gob.mx y no el SharePoint del SESNSP: el enlace de OneDrive
(sspcgob-my.sharepoint.com) exige inicio de sesion Microsoft (verificado
2026-07-19: redirige a login.microsoftonline.com incluso en navegador
limpio), y gob.mx protege sus paginas con un challenge anti-bot. La API
CKAN de datos.gob.mx es estable, sin autenticacion, y siempre apunta al
corte vigente aunque el nombre del archivo cambie (IDM_NM_dic25.csv ->
IDM_NM_jun26.csv...).

El descubrimiento es en dos pasos:
1. GET SESNSP_CKAN_PACKAGE_URL -> JSON del paquete "Incidencia delictiva".
2. Se toma el recurso cuyo nombre es SESNSP_RESOURCE_NAME y se descarga su
   URL con el Downloader generico (reintentos + validacion + metadata).

SESNSP_DOWNLOAD_URL (env) permite saltarse el paso 1 con una URL directa.
"""

import ssl
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
import truststore

from src_etl.core.config import settings
from src_etl.core.download_config import DownloadConfig
from src_etl.core.logging import PipelineLogger
from src_etl.utils.downloader import Downloader
from src_metadata.models import TaskStatus
from src_metadata.tracker import MetadataTracker

# datos.gob.mx sirve una cadena TLS incompleta (falta el intermedio): la
# verificacion con el bundle certifi de requests falla, pero el trust store
# del sistema operativo si la resuelve (igual que curl/navegadores). Se usa
# un SSLContext de truststore solo para la llamada CKAN; la descarga del
# CSV (repodatos.atdt.gob.mx) tiene cadena completa y no lo necesita.
_SYSTEM_SSL_CONTEXT = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
"""Contexto TLS respaldado por los certificados del sistema operativo."""


class _SystemTrustAdapter(requests.adapters.HTTPAdapter):
    """Adapter de requests que valida TLS contra el trust store del SO."""

    def init_poolmanager(self, *args, **kwargs):
        """Inyecta el SSLContext del sistema en el pool de conexiones."""
        kwargs["ssl_context"] = _SYSTEM_SSL_CONTEXT
        return super().init_poolmanager(*args, **kwargs)


class SesnspExtractor:
    """
    Descarga el CSV municipal del SESNSP hacia data/bronze/sesnsp/.

    Mismo contrato de observabilidad que DenueExtractor: un job_run
    `sesnsp_downloader`, una download_task por archivo y lineage WEB->FILE.

    Example:
        >>> extractor = SesnspExtractor(output_dir=Path("data/bronze/sesnsp"))
        >>> extractor.download_all()
    """

    JOB_NAME = "sesnsp_downloader"
    """Nombre del job en metadata (job_runs.job_name)."""

    def __init__(
        self,
        output_dir: Path,
        config: DownloadConfig | None = None,
        force: bool = False,
    ):
        """
        Args:
            output_dir: Directorio Bronze destino (data/bronze/sesnsp).
            config: Configuracion de descargas (defaults DOWNLOAD_* del env).
            force: Re-descargar aunque el archivo del corte ya exista.
        """
        self.output_dir = Path(output_dir)
        self.force = force
        self.config = config or DownloadConfig(download_directory=self.output_dir)
        self.tracker = MetadataTracker(db_path=settings.DATA_DIR / "metadata.duckdb")
        self.run_id: str | None = None

    def _discover_download_url(self, log) -> str:
        """
        Resuelve la URL del CSV del corte vigente.

        Usa SESNSP_DOWNLOAD_URL si esta definida; si no, consulta la API
        CKAN y busca el recurso SESNSP_RESOURCE_NAME.

        Raises:
            RuntimeError: si el paquete CKAN no contiene el recurso esperado.
        """
        if settings.SESNSP_DOWNLOAD_URL:
            log.info("sesnsp_url_override", url=settings.SESNSP_DOWNLOAD_URL)
            return settings.SESNSP_DOWNLOAD_URL

        session = requests.Session()
        session.mount("https://", _SystemTrustAdapter())
        session.headers["User-Agent"] = self.config.user_agent

        log.info("sesnsp_ckan_discovery", url=settings.SESNSP_CKAN_PACKAGE_URL)
        resp = session.get(settings.SESNSP_CKAN_PACKAGE_URL, timeout=self.config.timeout_seconds)
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("success"):
            raise RuntimeError(f"CKAN package_show fallo: {payload}")

        resources = payload["result"].get("resources", [])
        for resource in resources:
            if resource.get("name", "").strip().lower() == settings.SESNSP_RESOURCE_NAME.lower():
                url = resource["url"]
                log.info("sesnsp_resource_found", name=resource["name"], url=url)
                return url

        available = [r.get("name") for r in resources]
        raise RuntimeError(
            f"Recurso '{settings.SESNSP_RESOURCE_NAME}' no encontrado en el paquete CKAN. "
            f"Disponibles: {available}. Ajusta SESNSP_RESOURCE_NAME o define "
            "SESNSP_DOWNLOAD_URL en .env."
        )

    def download_all(self) -> dict:
        """
        Descarga el CSV del corte vigente (si no esta ya en Bronze).

        El nombre del archivo remoto identifica el corte: si ya existe en
        Bronze se omite la descarga (salvo force=True), de modo que correr
        el extract repetidamente solo baja datos cuando SESNSP publica un
        corte nuevo.

        Returns:
            Resumen del run (dict de MetadataTracker.get_run_summary).
        """
        self.run_id = self.tracker.create_run(
            job_name=self.JOB_NAME,
            job_type="DOWNLOAD",
            trigger_type="MANUAL",
            triggered_by="user",
            config=self.config.to_dict(),
        )
        pipeline_logger = PipelineLogger.create_for_run(
            run_id=self.run_id,
            job_name=self.JOB_NAME,
            log_dir=self.config.log_directory,
            log_level=self.config.log_level,
        )
        log = pipeline_logger.get_logger()

        try:
            url = self._discover_download_url(log)
            filename = Path(unquote(urlparse(url).path)).name
            dest_path = self.output_dir / filename
            self.output_dir.mkdir(parents=True, exist_ok=True)

            if dest_path.exists() and not self.force:
                log.info("sesnsp_already_downloaded", file=filename, skipped=True)
                self.tracker.complete_run(self.run_id)
                return self.tracker.get_run_summary(self.run_id)

            task_id = self.tracker.create_task(
                run_id=self.run_id,
                task_name=f"download_{filename}",
                source_url=url,
                target_path=str(dest_path),
            )
            self.tracker.start_task(task_id)
            log.info("sesnsp_download_started", url=url, target=str(dest_path))

            downloader = Downloader(self.config)
            result = downloader.download(url=url, target_path=dest_path)
            downloader.close()

            if result.success:
                self.tracker.complete_task(
                    task_id=task_id,
                    status=TaskStatus.SUCCESS,
                    actual_size_bytes=result.size_bytes,
                    actual_hash=(
                        result.validation_result.actual_hash if result.validation_result else None
                    ),
                    download_speed_mbps=result.download_speed_mbps,
                    validation_passed=(
                        result.validation_result.is_valid if result.validation_result else None
                    ),
                    duration_seconds=result.duration_seconds,
                )
                self.tracker.record_lineage(
                    run_id=self.run_id,
                    task_id=task_id,
                    source_location=url,
                    target_location=str(dest_path),
                    source_type="WEB",
                    target_type="FILE",
                    bytes_processed=result.size_bytes,
                )
                log.info("sesnsp_download_completed", size_bytes=result.size_bytes)
            else:
                self.tracker.complete_task(
                    task_id=task_id,
                    status=TaskStatus.FAILED,
                    error_type=result.error_type,
                    error_message=result.error_message,
                    duration_seconds=result.duration_seconds,
                )
                log.error(
                    "sesnsp_download_failed",
                    error_type=result.error_type.value if result.error_type else None,
                    error_message=result.error_message,
                )

            self.tracker.complete_run(self.run_id)
            return self.tracker.get_run_summary(self.run_id)

        except Exception as e:
            log.error("sesnsp_extract_failed", error=str(e))
            from src_metadata.models import JobStatus

            self.tracker.complete_run(self.run_id, JobStatus.FAILED, error_summary=str(e))
            raise
