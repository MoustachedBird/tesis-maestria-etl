"""
Unified DENUE Data Transformer

Orchestrates the complete ETL transformation pipeline:
1. TRANSFORM: los ZIP de Bronze se vuelven particiones de Silver
2. CLEE RESOLUTION: resolucion de identidad, en sitio sobre Silver


Uses MetadataTracker for comprehensive tracking and observability.
"""

import logging
from pathlib import Path

from src.etl.transformers.clee_resolver import CLEEResolver
from src.etl.transformers.processor import BronzeToSilverProcessor
from src.meta.tracker import MetadataTracker

logger = logging.getLogger(__name__)


class DenuePipelineOrchestrator:
    """
    Unified transformer for DENUE data pipeline.

    Coordina el ingest y las dos resoluciones de identidad, con su registro
    de metadatos. Gold no existe todavia: la Fase 5 define que significa
    derivarlo desde Silver.

    Usage:
        transformer = DenuePipelineOrchestrator(data_root=Path("data"))

        # Run individual stages
        transformer.run_transform_stage(force=False)
        transformer.run_clee_resolution_stage("radio", force=False)

        # Or run complete pipeline
        transformer.run_full_pipeline(force=False)
    """

    def __init__(
        self,
        data_root: Path,
        tracker: MetadataTracker | None = None,
    ):
        """
        Initialize unified transformer.

        Args:
            data_root: Root data directory
            tracker: MetadataTracker; por default, uno nuevo. Igual que en
                el resto de componentes, se inyecta en los tests.
        """
        self.data_root = Path(data_root)

        self.tracker = tracker or MetadataTracker()

        # Initialize stages
        self.transformer = BronzeToSilverProcessor(
            data_root=self.data_root,
            tracker=self.tracker,
        )

        # Un resolver por metodo, construido bajo demanda: cada uno abre su
        # archivo de log al nacer, y una corrida de `--only clee-radio` no
        # tiene por que crear los logs de los otros metodos.
        self._resolvers: dict[str, CLEEResolver] = {}

        logger.info("DenuePipelineOrchestrator initialized")

    def run_transform_stage(self, force: bool = False, periods: list[str] | None = None) -> bool:
        """
        Run transform stage: ZIP -> Silver.

        La unidad es el corte: cada periodo se carga como una particion
        nueva, con la fecha y las celdas H3 ya derivadas.

        Args:
            force: reconstruir aunque el corte ya este cargado
            periods: cortes a procesar; por default, los pendientes

        Returns:
            True if successful, False otherwise
        """
        logger.info("Starting TRANSFORM stage (Bronze -> Silver)")
        logger.info(
            f"   Mode: {'FORCE (reconstruye todos los cortes)' if force else 'INCREMENTAL'}"
        )

        try:
            success = self.transformer.run(force=force, periods=periods)

            if success:
                logger.info("TRANSFORM stage completed successfully")
            else:
                logger.warning("TRANSFORM stage completed with errors")

            return success

        except Exception as e:
            logger.error(f"TRANSFORM stage failed: {e}")
            raise

    def _resolver(self, metodo: str) -> CLEEResolver:
        """El resolver de un metodo, creado la primera vez que se pide.

        Args:
            metodo: clave de `CLEEResolver.METHODS`.

        Returns:
            CLEEResolver: el del metodo, reusado en llamadas siguientes.
        """
        if metodo not in self._resolvers:
            self._resolvers[metodo] = CLEEResolver(tracker=self.tracker, method=metodo)
        return self._resolvers[metodo]

    def run_clee_resolution_stage(self, metodo: str, force: bool = False) -> bool:
        """
        Corre la resolucion de identidad de UN metodo.

        Los metodos comparten el motor de ventana deslizante y la cascada;
        lo que cambia es como se generan los candidatos y, en los que la
        declaren, la regla de decision. Cada uno escribe su propia tabla
        (`silver.clee_<metodo>`), asi que una falla en uno no daña a los
        otros ni a la tabla de hechos.

        Es generica a proposito: agregar un metodo es una entrada en
        `CLEEResolver.METHODS` mas su seccion en el CLI, sin tocar esto.

        Args:
            metodo: clave de `CLEEResolver.METHODS` (radio, h3, celda_h3_7...).
            force: si True, reconstruye la tabla del metodo desde cero.

        Returns:
            True si termino sin errores.
        """
        resolver = self._resolver(metodo)
        logger.info(f"Starting CLEE RESOLUTION stage ({metodo} -> silver.{resolver.clee_table})")
        logger.info(f"   Mode: {'FORCE (reset and re-resolve all)' if force else 'INCREMENTAL'}")

        try:
            success = resolver.run(force=force)
            if success:
                logger.info(f"CLEE RESOLUTION ({metodo}) stage completed successfully")
            else:
                logger.warning(f"CLEE RESOLUTION ({metodo}) stage completed with errors")
            return success
        except Exception as e:
            logger.error(f"CLEE RESOLUTION ({metodo}) stage failed: {e}")
            raise

    def run_full_pipeline(self, force: bool = False) -> bool:
        """
        Run complete pipeline: Transform -> Normalize-Dates -> Load.

        Executes transform, date normalization, and load stages
        sequentially with full tracking. CLEE resolution is not included
        by default (run separately).

        Args:
            force: If True, force reprocess in all stages

        Returns:
            True if all stages successful, False otherwise
        """
        logger.info("Starting FULL PIPELINE (Transform -> Normalize-Dates -> H3-Cells -> Load)")
        logger.info("=" * 60)

        # Etapa 1: Transform (Bronze -> Silver). El ingest deriva la fecha
        # y las celdas H3 en vuelo, asi que no hay etapas sueltas para eso.
        logger.info("ETAPA 1/1: Transform")
        transform_success = self.run_transform_stage(force=force)

        if not transform_success:
            logger.error("Pipeline detenido: la etapa Transform fallo")
            return False

        logger.info("")
        logger.info("=" * 60)

        if transform_success:
            logger.info("FULL PIPELINE completed successfully!")
            return True
        else:
            logger.warning("FULL PIPELINE completed with errors")
            return False

    def get_pipeline_status(self) -> dict:
        """
        Get current pipeline status and metrics.

        Returns:
            Dict with pipeline statistics
        """

        def _run_dict(run: dict | None) -> dict:
            """Resumen compacto de una corrida, o huecos si no hubo ninguna."""
            if run is None:
                return {"run_id": None, "status": None, "date": None, "succeeded": 0, "failed": 0}
            return {
                "run_id": run["run_id"],
                "status": run["status"],
                "date": run["run_date"],
                "succeeded": run["tasks_succeeded"],
                "failed": run["tasks_failed"],
            }

        return {
            "latest_transform_run": _run_dict(self.tracker.get_latest_run_by_type("TRANSFORM")),
            "latest_clee_resolution_run": _run_dict(
                self.tracker.get_latest_run_by_type("CLEE_RESOLUTION")
            ),
            "process_metrics": self.tracker.get_process_metrics(),
            "clee_resolution_metrics": self.tracker.get_clee_resolution_metrics(),
        }

    def __repr__(self) -> str:
        """String representation."""
        return f"DenuePipelineOrchestrator(data_root={self.data_root})"
