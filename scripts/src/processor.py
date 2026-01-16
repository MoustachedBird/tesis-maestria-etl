import os
import logging
from pathlib import Path
from multiprocessing import Pool, cpu_count
from typing import List, Tuple

# Importamos la lógica de extracción y configuración de columnas
from scripts.utils.denue_helpers import process_zip_worker, get_valid_columns_list
# Importamos el gestor de estado (Base de datos de archivos)
from scripts.utils.manifest import ManifestManager

class DenueProcessor:
    def __init__(self, input_dir: Path, output_dir: Path, project_root: Path, force_mode: bool = False):
        """
        Inicializa el procesador DENUE controlado por Manifiesto.
        
        Args:
            input_dir: Donde están los ZIPs (data/raw/denue)
            output_dir: Donde irán los Parquets (data/processed/denue)
            project_root: La raíz del proyecto (donde está el Makefile)
            force_mode: Si es True, reintenta archivos con estatus ERROR.
        """
        self.input_dir = input_dir
        self.output_dir = output_dir
        # La carpeta 'data' es donde se guardan los CSVs de control (manifest_download.csv, etc.)
        self.data_root = project_root / "data" 
        self.force_mode = force_mode
        self.num_workers = max(1, cpu_count() - 1)
        
        # Instanciamos el manager apuntando a data/
        self.manifest_mgr = ManifestManager(self.data_root)

    def process(self):
        # Configuración básica de logs
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
        logging.info("🚀 DENUE PROCESSOR - MANIFEST DRIVEN")
        
        # ---------------------------------------------------------
        # PASO 1: Sincronización (Detectar lo que hay en disco)
        # ---------------------------------------------------------
        # Esto escanea data/raw y agrega cualquier archivo nuevo al CSV con estatus "NEW"
        self.manifest_mgr.init_raw_manifest(self.input_dir)
        
        # ---------------------------------------------------------
        # PASO 2: Obtener Tareas Pendientes
        # ---------------------------------------------------------
        # Pide al manifiesto la lista de archivos que faltan por procesar (NEW o ERROR)
        tasks = self.manifest_mgr.get_raw_tasks(retry_errors=self.force_mode)
        
        # Si no hay tareas, intentamos actualizar el inventario final y salimos
        if not tasks:
            logging.info("✨ Todo al día. No hay archivos pendientes en 'manifest_download.csv'.")
            logging.info("📋 Verificando inventario final...")
            self.manifest_mgr.rebuild_processed_manifest(self.output_dir)
            return

        logging.info(f"🔥 Procesando {len(tasks)} archivos pendientes...")
        
        target_columns = get_valid_columns_list()
        worker_args = []
        
        # Ruta base del proyecto para reconstruir rutas absolutas
        project_cwd = Path.cwd()
        
        for t in tasks:
            # t["file_path"] viene del CSV como ruta relativa: 'data/raw/denue/2020/archivo.zip'
            # Para que Python pueda abrir el archivo, necesitamos la ruta Absoluta.
            full_path = project_cwd / t["file_path"]
            
            # Validación de seguridad
            if not full_path.exists():
                logging.warning(f"⚠️ Archivo listado en manifiesto pero no encontrado en disco: {full_path}")
                # Podrías marcarlo como ERROR aquí si quisieras, por ahora lo saltamos
                continue
                
            # Agregamos a la cola de trabajo
            worker_args.append((full_path, t["period"], target_columns, self.output_dir))

        # ---------------------------------------------------------
        # PASO 3: Ejecución Paralela (Multiprocessing)
        # ---------------------------------------------------------
        batch_results = []
        
        try:
            with Pool(self.num_workers) as pool:
                # imap_unordered es más rápido porque no espera orden secuencial
                for i, res in enumerate(pool.imap_unordered(process_zip_worker, worker_args), 1):
                    
                    # Feedback visual en consola
                    icon = "✅" if res['status'] == 'OK' else "❌"
                    # res['file'] trae la ruta relativa que devolvió el worker (ID del manifiesto)
                    print(f"[{i}/{len(tasks)}] {icon} {res['file']}")
                    
                    # Preparamos el resultado para actualizar la DB
                    batch_results.append({
                        "file_path": res['file'], 
                        "period": res['period'],
                        "status": "DONE" if res['status'] == 'OK' else "ERROR",
                        "error_msg": res['invalid_cols'] if res['status'] != 'OK' else ""
                    })
                    
                    # Actualización por lotes cada 20 archivos (para no abrir/cerrar el CSV a cada rato)
                    if len(batch_results) >= 20:
                        self.manifest_mgr.update_raw_status_batch(batch_results)
                        batch_results = []

            # Guardar cualquier remanente que quedó en el buffer
            if batch_results:
                self.manifest_mgr.update_raw_status_batch(batch_results)

        except KeyboardInterrupt:
            logging.warning("⚠️ Proceso interrumpido por el usuario.")
            pool.terminate()
            pool.join()
            return
        
        # ---------------------------------------------------------
        # PASO 4: Reconstruir Inventario de Salida (Processed)
        # ---------------------------------------------------------
        # Esto genera 'manifest_processed.csv' escaneando los Parquets generados
        logging.info("📋 Actualizando inventario de archivos Parquet (Silver Layer)...")
        self.manifest_mgr.rebuild_processed_manifest(self.output_dir)
        
        logging.info("✅ CICLO DE PROCESAMIENTO COMPLETADO")