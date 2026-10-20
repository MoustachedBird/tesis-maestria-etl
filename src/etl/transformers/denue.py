"""
Unified DENUE Data Transformer

Orchestrates the complete ETL transformation pipeline:
1. TRANSFORM Stage: ZIP -> Silver DuckDB (Bronze -> Silver)
2. CLEE RESOLUTION Stage: Entity Resolution (Silver DuckDB in-place)
3. LOAD Stage: Silver DuckDB -> Gold DuckDB (Silver -> Gold)

Uses MetadataTracker for comprehensive tracking and observability.
"""

import logging
from pathlib import Path

from src.etl.loaders.loader import SilverToGoldLoader
from src.etl.transformers.clee_resolver import CLEEResolver
from src.etl.transformers.processor import BronzeToSilverProcessor
from src.etl.utils.backup import backup_warehouse
from src.meta.tracker import MetadataTracker
from src.shared.config import settings

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
        silver_db_path: Path | None = None,
        gold_db_path: Path | None = None,
        tracker: MetadataTracker | None = None,
    ):
        """
        Initialize unified transformer.

        Args:
            data_root: Root data directory
            silver_db_path: Path to Silver DuckDB (default: from settings)
            gold_db_path: Path to Gold DuckDB (default: from settings)
            tracker: MetadataTracker; por default, uno nuevo. Igual que en
                el resto de componentes, se inyecta en los tests.
        """
        self.data_root = Path(data_root)

        self.tracker = tracker or MetadataTracker()

        # Resolve paths (validators guarantee non-None, assert for type checker)
        resolved_silver = Path(silver_db_path) if silver_db_path else settings.SILVER_DUCKDB_PATH
        assert resolved_silver is not None, "SILVER_DUCKDB_PATH must be configured"
        self.silver_db_path: Path = resolved_silver

        resolved_gold = Path(gold_db_path) if gold_db_path else settings.GOLD_DUCKDB_PATH
        assert resolved_gold is not None, "GOLD_DUCKDB_PATH must be configured"
        self.gold_db_path: Path = resolved_gold

        # Initialize stages
        self.transformer = BronzeToSilverProcessor(
            data_root=self.data_root,
            tracker=self.tracker,
        )

        # Un resolver por metodo (misma maquinaria, distinta columna destino).
        self.clee_resolver = CLEEResolver(tracker=self.tracker, method="radio")
        self.clee_resolver_h3 = CLEEResolver(tracker=self.tracker, method="h3")

        self.loader = SilverToGoldLoader(
            tracker=self.tracker,
            silver_path=self.silver_db_path,
            gold_path=self.gold_db_path,
        )

        logger.info("DenuePipelineOrchestrator initialized")
        logger.info(f"   Silver DB: {self.silver_db_path}")
        logger.info(f"   Gold DB: {self.gold_db_path}")

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
        written back to the Silver DuckDB `clee_propagado_radio` column.

        Args:
            force: If True, reset all clee_propagado_radio values and re-run from scratch.

        Returns:
            True if successful, False otherwise
        """
        logger.info("Starting CLEE RESOLUTION stage (RADIO / BallTree -> clee_propagado_radio)")
        logger.info(f"   Mode: {'FORCE (reset and re-resolve all)' if force else 'INCREMENTAL'}")
        backup_warehouse(self.silver_db_path, stage="clee-radio")

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
        backup_warehouse(self.silver_db_path, stage="clee-h3")

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

    def run_load_stage(self, force: bool = False) -> bool:
        """
        Run load stage: Silver DuckDB -> Gold DuckDB.

        Promotes the Silver warehouse to Gold via file copy.

        Args:
            force: If True, copy even if Gold is up-to-date

        Returns:
            True if successful, False otherwise
        """
        logger.info("Starting LOAD stage (Silver -> Gold)")
        backup_warehouse(self.gold_db_path, stage="load")

        try:
            success = self.loader.run(force=force)

            if success:
                logger.info("LOAD stage completed successfully")
            else:
                logger.warning("LOAD stage failed")

            return success

        except Exception as e:
            logger.error(f"LOAD stage failed: {e}")
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
        logger.info("ETAPA 1/2: Transform")
        transform_success = self.run_transform_stage(force=force)

        if not transform_success:
            logger.error("Pipeline detenido: la etapa Transform fallo")
            return False

        logger.info("")
        logger.info("=" * 60)

        # Etapa 2: Load (Silver -> Gold)
        logger.info("ETAPA 2/2: Load")
        load_success = self.run_load_stage(force=force)

        logger.info("")
        logger.info("=" * 60)

        if transform_success and load_success:
            logger.info("FULL PIPELINE completed successfully!")
            logger.info(f"   Silver: {self.silver_db_path}")
            logger.info(f"   Gold: {self.gold_db_path}")
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
            "latest_load_run": _run_dict(self.tracker.get_latest_run_by_type("LOAD")),
            "latest_clee_resolution_run": _run_dict(
                self.tracker.get_latest_run_by_type("CLEE_RESOLUTION")
            ),
            "process_metrics": self.tracker.get_process_metrics(),
            "normalize_metrics": self.tracker.get_normalize_metrics(),
            "clee_resolution_metrics": self.tracker.get_clee_resolution_metrics(),
            "silver_db": str(self.silver_db_path),
            "gold_db": str(self.gold_db_path),
        }

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"DenuePipelineOrchestrator(data_root={self.data_root}, "
            f"silver={self.silver_db_path}, gold={self.gold_db_path})"
        )
