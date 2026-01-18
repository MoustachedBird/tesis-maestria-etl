import time
import requests
import re
from pathlib import Path
from datetime import datetime
from bs4 import BeautifulSoup

# Selenium Imports
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

# Custom Utilities
from scripts.utils.browser import get_chrome_driver
from scripts.utils.text import clean_filename
# Importamos el Manager Unificado
from scripts.utils.manifest import ReportManager

class InegiDownloader:
    def __init__(self, output_dir: Path, headless: bool = True):
        """
        Descargador DENUE integrado con el Manifiesto Unificado.
        """
        self.output_dir = output_dir
        self.headless = headless
        self.base_url = "https://www.inegi.org.mx/app/descarga/"
        self.base_url_inegi = "https://www.inegi.org.mx"
        self.wait_timeout = 30
        
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Calcular raíz del proyecto (data/raw/denue -> root)
        # Asumimos que output_dir es .../backend-core/data/raw/denue
        self.data_root = self.output_dir.parent.parent 
        
        # Instanciar el Gestor de Manifiesto
        self.report_mgr = ReportManager(self.data_root)

    def _download_with_requests(self, url, destination):
        """
        Descarga eficiente por streaming.
        """
        try:
            with requests.get(url, stream=True, timeout=60) as r:
                if r.status_code == 404:
                    return "NOK", "404 - Not Found"
                r.raise_for_status()
                with open(destination, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
            return "OK", "Downloaded successfully"
        except Exception as e:
            return "NOK", str(e)[:100]

    def _extract_and_filter_links(self, driver):
        """
        Scrapea la tabla de INEGI y filtra lo que ya tenemos en el Manifiesto.
        """
        print(f"🔍 Navegando a {self.base_url}...")
        driver.get(self.base_url)
        wait = WebDriverWait(driver, self.wait_timeout)
        new_downloads_list = []

        try:
            print("  ... Esperando tabla 'tblDescargaArchivos_denue'...")
            wait.until(EC.presence_of_element_located((By.ID, "tblDescargaArchivos_denue")))
            time.sleep(3)

            soup = BeautifulSoup(driver.page_source, 'html.parser')
            table = soup.find('table', id='tblDescargaArchivos_denue')
            
            if not table or not table.find('tbody'):
                return []

            rows = table.find('tbody').find_all('tr')
            print(f"  ... Procesando {len(rows)} filas encontradas.")

            # Obtener SET de URLs que ya existen y están OK según el Manifiesto
            existing_urls = self.report_mgr.download_report.get_existing_downloads()
            skipped_count = 0
            
            for row in rows:
                cols = row.find_all('td')
                if len(cols) < 3: continue

                activity_raw = cols[0].get_text(strip=True)
                if not activity_raw: continue
                
                activity = clean_filename(activity_raw)
                period = cols[1].get_text(strip=True).replace('/', '_')
                
                link_tag = cols[2].find('a', href=re.compile(r'csv\.zip'))
                if link_tag:
                    url = f"{self.base_url_inegi}{link_tag['href']}"
                    
                    # === FILTRO INTELIGENTE ===
                    # Si la URL ya está en el manifiesto como OK, la saltamos.
                    if url in existing_urls:
                        skipped_count += 1
                        continue
                        
                    new_downloads_list.append((period, activity, url))
                else:
                    # Registramos error de enlace perdido en el manifiesto
                    # Usamos un path dummy para el registro
                    dummy_path = f"MISSING_LINK/{activity}.zip"
                    self.report_mgr.download_report.register_download(
                        file_path=dummy_path,
                        url="N/A",
                        period=period,
                        status="NOK",
                        msg="No se encontró link ZIP en la tabla"
                    )
            
            if skipped_count > 0:
                print(f"⏩ Saltados {skipped_count} archivos (Ya existen en Manifiesto).")
            
            return new_downloads_list

        except TimeoutException:
            print("❌ Timeout: La tabla no cargó.")
            return []
        except Exception as e:
            print(f"❌ Error de Scraping: {e}")
            return []

    def download_all(self):
        """
        Orquesta el proceso de descarga unificado.
        """
        print(f"\n🚀 INICIANDO DESCARGA INTELIGENTE EN: {self.output_dir}")
        
        # 1. Auditoría Inicial (Sincronizar Disco -> Manifiesto)
        # Esto reemplaza al viejo _load_and_audit_report
        # Detecta si alguien borró archivos manualmente o si hay archivos nuevos 'extra'
        print("🔍 Auditando archivos físicos vs Manifiesto...")
        self.report_mgr.download_report.init_raw_manifest(self.output_dir)
        
        driver = get_chrome_driver(headless=self.headless)
        if not driver: return

        try:
            # 2. Obtener SOLO archivos nuevos (filtrando vs Manifiesto)
            files_to_download = self._extract_and_filter_links(driver)
            
            if not files_to_download:
                print("✨ Sistema al día. No hay descargas nuevas.")
            else:
                print(f"📊 Archivos nuevos a descargar: {len(files_to_download)}")
                
                for idx, (period, activity, url) in enumerate(files_to_download, 1):
                    dest_folder = self.output_dir / period
                    dest_folder.mkdir(parents=True, exist_ok=True)
                    
                    filename = f"{activity}.zip"
                    final_path = dest_folder / filename
                    
                    # Ruta relativa para el ID del Manifiesto (ej: data/raw/denue/2024/file.zip)
                    rel_path = str(final_path.resolve().relative_to(Path.cwd()))
                    
                    print(f"[{idx}/{len(files_to_download)}] ⬇️ {filename}")
                    
                    status, msg = self._download_with_requests(url, final_path)
                    
                    if status == "NOK":
                        print(f"    ❌ {msg}")
                    
                    # 3. REGISTRAR EN SINGLE SOURCE OF TRUTH
                    self.report_mgr.download_report.register_download(
                        file_path=rel_path,
                        url=url,
                        period=period,
                        status=status,
                        msg=msg
                    )
                    
                    time.sleep(0.5)

        finally:
            if driver:
                driver.quit()
            
            print("\n✅ Fase inicial terminada.")

        # 4. Fase de Reintentos (Usando el Manifiesto)
        print(f"\n🔍 Verificando descargas fallidas (NOK)...")
        self.retry()

    def retry(self):
        """
        Reintenta descargas marcadas como 'NOK' en el manifiesto.
        """
        # Obtenemos la lista de fallos desde el Manifiesto
        failed_tasks = self.report_mgr.download_report.get_failed_downloads()
        
        if not failed_tasks:
            print("✨ No hay reparaciones pendientes.")
            return

        print(f"🔄 Reintentando {len(failed_tasks)} archivos...")
        
        for task in failed_tasks:
            url = task['download_url']
            rel_path = task['file_path']
            period = task['period']
            
            if not url or url == "N/A": continue

            # Reconstruir ruta absoluta
            final_path = Path.cwd() / rel_path
            final_path.parent.mkdir(parents=True, exist_ok=True)
            
            print(f"--- Reintentando: {final_path.name} ---")
            
            success = False
            last_msg = ""
            
            for attempt in range(1, 4):
                print(f"    👉 Intento {attempt}/3...")
                status, msg = self._download_with_requests(url, final_path)
                
                if status == "OK":
                    success = True
                    print(f"    ✅ ÉXITO!")
                    # Actualizar Manifiesto inmediatamente
                    self.report_mgr.download_report.register_download(
                        file_path=rel_path,
                        url=url,
                        period=period,
                        status="OK",
                        msg="Retry successful"
                    )
                    break
                else:
                    last_msg = msg
                    time.sleep(2)
            
            if not success:
                # Actualizar error en Manifiesto
                self.report_mgr.download_report.register_download(
                    file_path=rel_path,
                    url=url,
                    period=period,
                    status="NOK",
                    msg=f"Falló tras 3 intentos: {last_msg}"
                )