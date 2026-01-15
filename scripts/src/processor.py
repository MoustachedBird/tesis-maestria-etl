import os
import logging
from pathlib import Path
from multiprocessing import Pool, cpu_count
from typing import List, Tuple

from scripts.utils.denue_helpers import process_zip_worker, get_valid_columns_list
from scripts.utils.manifest import ManifestManager

class DenueProcessor:
    def __init__(self, input_dir: Path, output_dir: Path, project_root: Path, force_mode: bool = False):
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.data_root = project_root / "data" # Raíz donde vivirán los manifiestos
        self.force_mode = force_mode
        self.num_workers = max(1, cpu_count() - 1)
        
        # Instanciar el Manager apuntando a /data
        self.manifest_mgr = ManifestManager(self.data_root)

    def process(self):
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
        logging.info("🚀 DENUE PROCESSOR - MANIFEST DRIVEN")
        
        # 1. Sincronizar Manifiesto RAW (Detectar nuevos ZIPs)
        self.manifest_mgr.init_raw_manifest(self.input_dir)
        
        # 2. Obtener tareas (NEW o ERROR si es force)
        tasks = self.manifest_mgr.get_raw_tasks(retry_errors=self.force_mode)
        
        if not tasks:
            logging.info("✨ Todo está al día según 'data/manifest_download.csv'.")
            return

        logging.info(f"🔥 Procesando {len(tasks)} archivos pendientes...")
        
        # Preparar argumentos para multiprocessing
        target_columns = get_valid_columns_list()
        worker_args = []
        for t in tasks:
            # Reconstruir ruta completa: data_root + relative_path
            full_path = self.data_root / t["file_path"]
            worker_args.append((full_path, t["period"], target_columns, self.output_dir))

        # 3. Ejecutar Workers
        batch_results = []
        try:
            with Pool(self.num_workers) as pool:
                for i, res in enumerate(pool.imap_unordered(process_zip_worker, worker_args), 1):
                    
                    # Feedback visual
                    icon = "✅" if res['status'] == 'OK' else "❌"
                    print(f"[{i}/{len(tasks)}] {icon} {res['file']}")
                    
                    # Acumular resultado para actualizar manifiesto
                    batch_results.append({
                        "file_path": res['file'], # Debe coincidir con el ID del manifiesto
                        "period": res['period'],
                        "status": "DONE" if res['status'] == 'OK' else "ERROR",
                        "error_msg": res['invalid_cols'] if res['status'] != 'OK' else ""
                    })
                    
                    # Actualizar manifiesto cada 20 archivos (Checkpointing)
                    if len(batch_results) >= 20:
                        self.manifest_mgr.update_raw_status_batch(batch_results)
                        batch_results = []

            # Guardar remanentes
            if batch_results:
                self.manifest_mgr.update_raw_status_batch(batch_results)

        except KeyboardInterrupt:
            logging.warning("⚠️ Proceso interrumpido.")
            pool.terminate()
        
        # 4. Paso Final: Reconstruir el catálogo de Parquets
        # Esto es crucial para que el siguiente paso (Harmonization) sepa qué existe.
        logging.info("📋 Actualizando inventario de archivos procesados...")
        self.manifest_mgr.rebuild_processed_manifest(self.output_dir)
        
        logging.info("✅ CICLO DE PROCESAMIENTO COMPLETADO")