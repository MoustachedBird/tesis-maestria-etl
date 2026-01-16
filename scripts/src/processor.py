import os
import logging
from pathlib import Path
from multiprocessing import Pool, cpu_count
from scripts.utils.denue_helpers import process_zip_worker, get_valid_columns_list
from scripts.utils.manifest import ManifestManager

class DenueProcessor:
    def __init__(self, input_dir: Path, output_dir: Path, project_root: Path, force_mode: bool = False):
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.data_root = project_root / "data" 
        self.force_mode = force_mode
        self.num_workers = max(1, cpu_count() - 1)
        
        self.manifest_mgr = ManifestManager(self.data_root)

    def process(self):
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
        logging.info("🚀 DENUE PROCESSOR - MANIFEST DRIVEN")
        
        # 1. Fallback Scan
        self.manifest_mgr.init_raw_manifest(self.input_dir)
        
        # 2. Reconstruir inventario Processed
        self.manifest_mgr.rebuild_processed_manifest(self.output_dir)
        
        # 3. Validar Integridad
        self.manifest_mgr.validate_and_repair()
        
        # 4. Obtener Tareas
        tasks = self.manifest_mgr.get_raw_tasks(retry_errors=self.force_mode)
        
        if not tasks:
            logging.info("✨ Todo al día. Nada pendiente de procesar.")
            return

        logging.info(f"🔥 Procesando {len(tasks)} archivos...")
        
        target_columns = get_valid_columns_list()
        worker_args = []
        project_cwd = Path.cwd()
        
        for t in tasks:
            full_path = project_cwd / t["file_path"]
            if not full_path.exists():
                logging.warning(f"⚠️ Archivo perdido: {full_path}")
                continue
            worker_args.append((full_path, t["period"], target_columns, self.output_dir))

        # 5. Ejecución
        batch_results = []
        try:
            with Pool(self.num_workers) as pool:
                for i, res in enumerate(pool.imap_unordered(process_zip_worker, worker_args), 1):
                    
                    icon = "✅" if res['status'] == 'OK' else "❌"
                    print(f"[{i}/{len(tasks)}] {icon} {res['file']}")
                    
                    # --- CAMBIO IMPORTANTE ---
                    # Guardamos el mensaje SIEMPRE, no solo en error.
                    # Esto permite ver "Filled: 5 cols" en el manifiesto aunque sea OK.
                    batch_results.append({
                        "file_path": res['file'], 
                        "period": res['period'],
                        "process_status": "DONE" if res['status'] == 'OK' else "ERROR",
                        "process_msg": res['invalid_cols'] # <-- Guardamos el reporte siempre
                    })
                    
                    if len(batch_results) >= 20:
                        self.manifest_mgr.update_raw_status_batch(batch_results)
                        batch_results = []

            if batch_results:
                self.manifest_mgr.update_raw_status_batch(batch_results)

        except KeyboardInterrupt:
            logging.warning("⚠️ Interrumpido.")
            pool.terminate()
            pool.join()
            return
        
        logging.info("📋 Actualizando inventario final...")
        self.manifest_mgr.rebuild_processed_manifest(self.output_dir)
        logging.info("✅ PROCESO FINALIZADO")