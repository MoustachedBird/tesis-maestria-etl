import os
import json
import logging
import csv
from pathlib import Path
from datetime import datetime
from multiprocessing import Pool, cpu_count, Lock
from typing import List, Tuple

# Import helper functions from utils
from scripts.utils.denue_helpers import process_zip_worker, get_valid_columns_list

# Global lock for CSV writing (Must be at module level for safe multiprocessing)
csv_lock = Lock()

class DenueProcessor:
    def __init__(self, input_dir: Path, output_dir: Path):
        self.input_dir = input_dir
        self.output_dir = output_dir
        # Use all available cores except one (keep system responsive)
        self.num_workers = max(1, cpu_count() - 1)
        self.timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # Report Paths
        self.log_file = self.output_dir / f"processing_{self.timestamp}.log"
        self.csv_processed = self.output_dir / f"processed_files_{self.timestamp}.csv"
        self.csv_skipped = self.output_dir / f"skipped_files_{self.timestamp}.csv"
        self.schema_file = self.output_dir / "_schema.json"

    def _setup_logging(self):
        """Configures the logger."""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(message)s',
            handlers=[
                logging.FileHandler(self.log_file, mode='w', encoding='utf-8'),
                logging.StreamHandler()
            ],
            force=True
        )

    def _init_reports(self):
        """Initializes the CSV reports with headers."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        with open(self.csv_processed, 'w', newline='', encoding='utf-8-sig') as f:
            csv.writer(f).writerow([
                'zip_file', 'period', 'total_rows', 'valid_rows',
                'invalid_columns', 'time_sec'
            ])
            
        with open(self.csv_skipped, 'w', newline='', encoding='utf-8-sig') as f:
            csv.writer(f).writerow(['zip_file', 'period', 'reason', 'details'])

    def _save_batch_report(self, file_path: Path, data: List[Tuple]):
        """Thread-safe batch writing to CSV."""
        with csv_lock:
            with open(file_path, 'a', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                writer.writerows(data)
                f.flush()
                os.fsync(f.fileno())

    def _get_tasks(self, target_columns: List[str]) -> List[Tuple]:
        """Scans input directory and prepares arguments for workers."""
        tasks = []
        if not self.input_dir.exists():
            return tasks
        
        # Structure: input_dir / period_folder / file.zip
        for period_dir in self.input_dir.iterdir():
            if period_dir.is_dir():
                zip_files = list(period_dir.glob("*.zip"))
                for zf in zip_files:
                    # Pass output_dir explicitly to avoid global variables
                    tasks.append((zf, period_dir.name, target_columns, self.output_dir))
        
        # Sort by period to process in chronological order
        return sorted(tasks, key=lambda x: x[1])

    def process(self):
        """Main execution method."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._setup_logging()
        self._init_reports()
        
        logging.info("=" * 60)
        logging.info(f"🚀 DENUE PROCESSOR STARTING - {self.num_workers} CORES (POLARS + MULTIPROCESSING)")
        logging.info(f"📂 Input: {self.input_dir}")
        logging.info(f"📂 Output: {self.output_dir}")
        logging.info("=" * 60)

        # 1. Get Schema and Save it
        target_columns = get_valid_columns_list()
        schema_data = {'columns': target_columns}
        
        with open(self.schema_file, 'w', encoding='utf-8') as f:
            json.dump(schema_data, f, indent=2)
        
        # 2. Prepare Tasks
        tasks = self._get_tasks(target_columns)
        
        if not tasks:
            logging.info("❌ No ZIP files found to process.")
            return

        logging.info(f"Found {len(tasks)} ZIP files. Starting parallel processing...")
        
        results_ok = []
        results_skipped = []
        
        try:
            # 3. Start Multiprocessing Pool
            with Pool(self.num_workers) as pool:
                # imap_unordered allows processing results as soon as they finish
                for i, res in enumerate(pool.imap_unordered(process_zip_worker, tasks), 1):
                    
                    # --- Console Progress ---
                    pct = (i / len(tasks)) * 100
                    msg = f"[{i}/{len(tasks)}] {pct:.1f}% - {res['file']} - {res['status']}"
                    
                    # Log to file every 10 items, print to console every item
                    if i % 10 == 0: logging.info(msg)
                    else: print(f"\r{msg}", end='', flush=True)
                    
                    # --- Result Collection ---
                    if res['status'] == 'OK':
                        results_ok.append((
                            res['file'], res['period'], res['total_rows'],
                            res['valid_rows'], res['invalid_cols'], f"{res['time']:.2f}"
                        ))
                    else:
                        results_skipped.append((
                            res['file'], res['period'], res['status'], res['invalid_cols']
                        ))
                    
                    # --- Batch Save (Every 50 files) ---
                    if len(results_ok) >= 50:
                        self._save_batch_report(self.csv_processed, results_ok)
                        results_ok = []
                    if len(results_skipped) >= 50:
                        self._save_batch_report(self.csv_skipped, results_skipped)
                        results_skipped = []

            # 4. Save Remaining Results
            if results_ok: self._save_batch_report(self.csv_processed, results_ok)
            if results_skipped: self._save_batch_report(self.csv_skipped, results_skipped)

        except KeyboardInterrupt:
            logging.warning("\n⚠️ Process interrupted by user.")
            pool.terminate()
            pool.join()
        
        logging.info("\n" + "="*60)
        logging.info("✅ PROCESSING COMPLETE")
        logging.info(f"📄 Reports and Parquet files saved in: {self.output_dir}")