import re
import time
from pathlib import Path
from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Importamos el limpiador de texto que ya tienes
from scripts.utils.text import clean_filename

def scrape_denue_table(driver, wait_timeout, report_mgr, output_dir, base_url_inegi):
    """
    Scrapea la tabla de INEGI y sincroniza el manifiesto DuckDB con el disco.
    """
    new_tasks = []
    wait = WebDriverWait(driver, wait_timeout)
    project_root = Path.cwd()
    
    try:
        # 1. Esperar a que la tabla dinámica cargue
        print("  ... Esperando tabla 'tblDescargaArchivos_denue'...")
        wait.until(EC.presence_of_element_located((By.ID, "tblDescargaArchivos_denue")))
        time.sleep(3) # Tiempo para que el JS termine de renderizar filas

        soup = BeautifulSoup(driver.page_source, 'html.parser')
        table = soup.find('table', id='tblDescargaArchivos_denue')
        
        if not table or not table.find('tbody'):
            print("❌ Error: No se encontró el cuerpo de la tabla en el HTML.")
            return []

        rows = table.find('tbody').find_all('tr')
        print(f"  ... Procesando {len(rows)} filas encontradas en el sitio.")

        # Obtener lo que la DB ya conoce como exitoso
        existing_urls = report_mgr.download_report.get_existing_downloads()
        
        counts = {"skipped": 0, "synced": 0}

        for row in rows:
            cols = row.find_all('td')
            if len(cols) < 3: continue

            # Extraer metadata de la fila
            activity_raw = cols[0].get_text(strip=True)
            if not activity_raw: continue
            
            activity = clean_filename(activity_raw)
            period = cols[1].get_text(strip=True).replace('/', '_')
            
            # Buscar el enlace al archivo ZIP
            link_tag = cols[2].find('a', href=re.compile(r'csv\.zip'))

            if link_tag:
                url = f"{base_url_inegi}{link_tag['href']}"
                
                # --- LÓGICA DE FILTRADO Y ACTUALIZACIÓN ---
                
                # A. Saltado: Si la URL ya está marcada como OK en DuckDB
                if url in existing_urls:
                    counts["skipped"] += 1
                    continue
                
                # B. Sincronización: Si no está en DB pero el archivo existe físicamente
                filename = f"{activity}.zip"
                dest_path = output_dir / period / filename
                rel_path = str(dest_path.resolve().relative_to(project_root))

                if dest_path.exists():
                    print(f"📝 Actualizando reporte: {filename} encontrado en disco.")
                    report_mgr.download_report.register_download(
                        file_path=rel_path, 
                        url=url, 
                        period=period,
                        status="OK", 
                        msg="Found on disk during scraping synchronization"
                    )
                    counts["synced"] += 1
                    continue
                
                # C. Tarea Nueva: No está en DB y no está en disco
                new_tasks.append((period, activity, url))
            
            else:
                # D. Error de Enlace: No hay link ZIP en la tabla de INEGI
                print(f"⚠️ Enlace no encontrado para: {activity} ({period})")
                dummy_path = f"MISSING_LINK/{activity}.zip"
                report_mgr.download_report.register_download(
                    file_path=dummy_path,
                    url="N/A",
                    period=period,
                    status="NOK",
                    msg="No se encontró link ZIP en la tabla del INEGI"
                )

        print(f"\n📊 Resumen de la tabla:")
        print(f"   ⏩ Ya registrados en DB: {counts['skipped']}")
        print(f"   🏠 Sincronizados desde disco: {counts['synced']}")
        print(f"   🆕 Pendientes por descargar: {len(new_tasks)}")
        
        return new_tasks

    except Exception as e:
        print(f"❌ Error fatal en el Scraper: {str(e)}")
        return []