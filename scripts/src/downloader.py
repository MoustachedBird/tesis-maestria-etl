import time
from pathlib import Path
from scripts.utils.browser import get_chrome_driver
from scripts.src.reporter import ReportManager
from scripts.utils.denue_network import download_file_streaming
from scripts.utils.denue_scraping import scrape_denue_table

class InegiDownloader:
    def __init__(self, output_dir: Path, headless: bool = True):
        self.output_dir = output_dir
        self.headless = headless
        self.base_url = "https://www.inegi.org.mx/app/descarga/"
        self.base_url_inegi = "https://www.inegi.org.mx"
        
        self.report_mgr = ReportManager(self.output_dir.parent.parent)

    def download_all(self):
        print(f"\n🚀 DESCARGA INTELIGENTE: {self.output_dir}")
        self.report_mgr.download_report.init_raw_manifest(self.output_dir)
        
        driver = get_chrome_driver(headless=self.headless)
        if not driver: return

        try:
            driver.get(self.base_url)
            tasks = scrape_denue_table(driver, 30, self.report_mgr, self.output_dir, self.base_url_inegi)
            
            if not tasks:
                print("✨ Nada nuevo que descargar.")
                return

            print(f"📊 Archivos a procesar: {len(tasks)}")
            for idx, (period, activity, url) in enumerate(tasks, 1):
                dest_path = self.output_dir / period / f"{activity}.zip"
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                
                print(f"[{idx}/{len(tasks)}] ⬇️ {activity}.zip")
                status, msg = download_file_streaming(url, dest_path)
                
                rel_path = str(dest_path.resolve().relative_to(Path.cwd()))
                self.report_mgr.download_report.register_download(
                    file_path=rel_path, url=url, period=period, status=status, msg=msg
                )
                time.sleep(0.5)

        finally:
            if driver: driver.quit()
        
        self.retry()

    def retry(self):
        """Usa el reporte de DuckDB para reintentar fallos."""
        failed = self.report_mgr.download_report.get_failed_downloads()
        if not failed: return

        print(f"🔄 Reintentando {len(failed)} fallos...")
        for task in failed:
            if not task['download_url'] or task['download_url'] == "N/A": continue
            
            dest = Path.cwd() / task['file_path']
            dest.parent.mkdir(parents=True, exist_ok=True)
            
            status, msg = download_file_streaming(task['download_url'], dest)
            self.report_mgr.download_report.register_download(
                file_path=task['file_path'], url=task['download_url'], 
                period=task['period'], status=status, msg=msg
            )