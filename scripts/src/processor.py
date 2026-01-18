import os
import logging
from pathlib import Path
from multiprocessing import Pool, cpu_count
from scripts.utils.denue_helpers import process_zip_worker, get_valid_columns_list
from scripts.src.reporter import ReportManager

class DenueProcessor:
    def __init__(self, input_dir: Path, output_dir: Path, project_root: Path, force_mode: bool = False):
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.data_root = project_root / "data" 
        self.force_mode = force_mode
        self.num_workers = max(1, cpu_count() - 1)
        
        # Orquestador Maestro (DuckDB)
        self.report_mgr = ReportManager(self.data_root)

    def process(self):
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
        logging.info("🚀 DENUE PROCESSOR - DUCKDB MANIFEST DRIVEN")
        
        # 1. Registro inicial / Sincronización de ZIPs (Capa Raw data)
        # Equivalente a init_raw_manifest: usamos register_download o escaneo manual
        # Para este processor, el equivalente directo es init_raw_manifest que definimos en ReportRawFiles
        self.report_mgr.download_report.init_raw_manifest(self.input_dir)
        
        # 2. Reconstruir inventario Silver (Capa Silver)
        self.report_mgr.process_report.rebuild_processed_manifest(self.output_dir)
        
        # 3. Validar Integridad (Orquestador Maestro)
        self.report_mgr.validate_integrity()
        
        # 4. Obtener Tareas (Capa Raw data)
        tasks = self.report_mgr.download_report.get_raw_tasks(retry_errors=self.force_mode)
        
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
                logging.warning(f"⚠️ Archivo perdido en disco: {full_path}")
                continue
            worker_args.append((full_path, t["period"], target_columns, self.output_dir))

        # 5. Ejecución en paralelo
        batch_results = []
        try:
            with Pool(self.num_workers) as pool:
                for i, res in enumerate(pool.imap_unordered(process_zip_worker, worker_args), 1):
                    
                    icon = "✅" if res['status'] == 'OK' else "❌"
                    print(f"[{i}/{len(tasks)}] {icon} {res['file']}")
                    
                    # Preparamos el batch para update_raw_status_batch
                    batch_results.append({
                        "file_path": res['file'], 
                        "process_status": "DONE" if res['status'] == 'OK' else "ERROR",
                        "process_msg": str(res['invalid_cols'])
                    })
                    
                    # Guardado parcial cada 20 archivos en DuckDB
                    if len(batch_results) >= 20:
                        self.report_mgr.download_report.update_raw_status_batch(batch_results)
                        batch_results = []

            # Guardar resultados finales del lote
            if batch_results:
                self.report_mgr.download_report.update_raw_status_batch(batch_results)

        except KeyboardInterrupt:
            logging.warning("⚠️ Proceso interrumpido por el usuario.")
            return
        
        # 6. Sincronización final de la capa Silver
        logging.info("📋 Sincronizando inventario Silver final...")
        self.report_mgr.process_report.rebuild_processed_manifest(self.output_dir)
        logging.info("✅ PROCESO FINALIZADO")