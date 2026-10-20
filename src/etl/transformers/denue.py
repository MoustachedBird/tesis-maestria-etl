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

    Coordinates transform, CLEE resolution, and load stages with full
    metadata tracking and observability.

    Usage:
        transformer = DenuePipelineOrchestrator(data_root=Path("data"))

        # Run individual stages
        transformer.run_transform_stage(force=False)
        transformer.run_load_stage()

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

        # Un resolver por metodo (misma maquinaria, distinta columna destino).
        self.clee_resolver = CLEEResolver(tracker=self.tracker, method="radio")
        self.clee_resolver_h3 = CLEEResolver(tracker=self.tracker, method="h3")

        logger.info("DenuePipelineOrchestrator initialized")

    def run_transform_stage(self, force: bool = False) -> bool:
        """
        Run transform stage: ZIP -> Silver.

        La unidad es el corte: cada periodo se carga como una particion
        nueva, con la fecha y las celdas H3 ya derivadas.

        Args:
            force: reconstruir todos los cortes, no solo los pendientes

        Returns:
            True if successful, False otherwise
        """
        logger.info("Starting TRANSFORM stage (Bronze -> Silver)")
        logger.info(f"   Mode: {'FORCE (reconstruye todos los cortes)' if force else 'INCREMENTAL'}")

        try:
            success = self.transformer.run(force=force)

            if success:
                logger.info("TRANSFORM stage completed successfully")
            else:
                logger.warning("TRANSFORM stage completed with errors")

            return success

        except Exception as e:
            logger.error(f"TRANSFORM stage failed: {e}")
            raise

    def run_clee_resolution_stage(self, force: bool = False) -> bool:
        """
        Run CLEE resolution stage: Entity Resolution.

        Matches establishments across time periods and generates
        master identifiers (CLEEs) for entity tracking. Results are
        written back to the `clee_propagado_radio` column.

        Args:
            force: If True, reset all clee_propagado_radio values and re-run from scratch.

        Returns:
            True if successful, False otherwise
        """
        logger.info("Starting CLEE RESOLUTION stage (RADIO / BallTree -> clee_propagado_radio)")
        logger.info(f"   Mode: {'FORCE (reset and re-resolve all)' if force else 'INCREMENTAL'}")

        try:
            success = self.clee_resolver.run(force=force)

            if success:
                logger.info("CLEE RESOLUTION (radio) stage completed successfully")
            else:
                logger.warning("CLEE RESOLUTION (radio) stage completed with errors")

            return success

        except Exception as e:
            logger.error(f"CLEE RESOLUTION (radio) stage failed: {e}")
            raise

    def run_clee_resolution_h3_stage(self, force: bool = False) -> bool:
        """
        Run CLEE resolution stage with H3 cell blocking.

        Same sliding-window entity resolution as the radio method, but
        candidates come from H3 cell blocking (grid_disk + exact haversine
        post-filter). Results are written to the `clee_propagado_h3` column,
        independent from `clee_propagado_radio` (side-by-side comparison).

        Args:
            force: If True, reset all clee_propagado_h3 values and re-run.

        Returns:
            True if successful, False otherwise
        """
        logger.info("Starting CLEE RESOLUTION stage (H3 blocking -> clee_propagado_h3)")
        logger.info(f"   Mode: {'FORCE (reset and re-resolve all)' if force else 'INCREMENTAL'}")

        try:
            success = self.clee_resolver_h3.run(force=force)

            if success:
                logger.info("CLEE RESOLUTION (h3) stage completed successfully")
            else:
                logger.warning("CLEE RESOLUTION (h3) stage completed with errors")

            return success

        except Exception as e:
            logger.error(f"CLEE RESOLUTION (h3) stage failed: {e}")
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
            "latest_transform_run": _run_dict(
                self.tracker.get_latest_run_by_type("TRANSFORM", "PROCESS")
            ),
            "latest_normalize_run": _run_dict(self.tracker.get_latest_run_by_type("NORMALIZE")),
            "latest_clee_resolution_run": _run_dict(
                self.tracker.get_latest_run_by_type("CLEE_RESOLUTION")
            ),
            "process_metrics": self.tracker.get_process_metrics(),
            "normalize_metrics": self.tracker.get_normalize_metrics(),
            "clee_resolution_metrics": self.tracker.get_clee_resolution_metrics(),
        }

    def __repr__(self) -> str:
        """String representation."""
        return f"DenuePipelineOrchestrator(data_root={self.data_root})"
