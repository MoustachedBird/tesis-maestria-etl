import time
import csv
import re
import requests
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

class InegiDownloader:
    def __init__(self, output_dir: Path, headless: bool = True):
        """
        Initializes the DENUE downloader.
        """
        self.output_dir = output_dir
        self.headless = headless
        self.base_url = "https://www.inegi.org.mx/app/descarga/"
        self.base_url_inegi = "https://www.inegi.org.mx"
        self.wait_timeout = 30
        
        # Generate report path dynamically inside the output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.report_path = self.output_dir / f"report_download_{timestamp}.csv"

    def _initialize_report(self):
        try:
            print(f"📄 Creating report at: {self.report_path}")
            with open(self.report_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                # English headers for the CSV
                writer.writerow(["filename", "destination_path", "download_url", "status", "reason"])
        except Exception as e:
            print(f"❌ Error initializing report: {e}")

    def _append_to_report(self, row_data):
        try:
            with open(self.report_path, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(row_data)
        except Exception as e:
            print(f"❌ Error writing to report: {e}")

    def _download_with_requests(self, url, destination):
        try:
            with requests.get(url, stream=True, timeout=60) as r:
                if r.status_code == 404:
                    return "nok", "404 - Not Found"
                r.raise_for_status()
                with open(destination, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
            return "ok", "Downloaded successfully"
        except Exception as e:
            # Return short error message
            return "nok", str(e)[:100]

    def _extract_links(self, driver):
        print(f"🔍 Navigating to {self.base_url}...")
        driver.get(self.base_url)
        wait = WebDriverWait(driver, self.wait_timeout)
        downloads_list = []

        try:
            print("  ... Waiting for table 'tblDescargaArchivos_denue'...")
            wait.until(EC.presence_of_element_located((By.ID, "tblDescargaArchivos_denue")))
            time.sleep(3) # Extra wait for JS stability

            soup = BeautifulSoup(driver.page_source, 'html.parser')
            table = soup.find('table', id='tblDescargaArchivos_denue')
            
            if not table or not table.find('tbody'):
                return []

            rows = table.find('tbody').find_all('tr')
            print(f"  ... Processing {len(rows)} rows found.")

            for row in rows:
                cols = row.find_all('td')
                if len(cols) < 3: continue

                activity_raw = cols[0].get_text(strip=True)
                if not activity_raw: continue
                
                # Use utility to clean filename
                activity = clean_filename(activity_raw)
                
                # Clean period format (e.g. 11/2024 -> 11_2024)
                period = cols[1].get_text(strip=True).replace('/', '_')
                
                link_tag = cols[2].find('a', href=re.compile(r'csv\.zip'))
                if link_tag:
                    url = f"{self.base_url_inegi}{link_tag['href']}"
                    downloads_list.append((period, activity, url))
                else:
                    # Log missing link
                    self._append_to_report([f"{activity}.zip", "N/A", "N/A", "nok", "No zip link found"])
            
            return downloads_list

        except TimeoutException:
            print("❌ Timeout: Table did not load.")
            return []
        except Exception as e:
            print(f"❌ Scraping error: {e}")
            return []

    def download_all(self):
        """
        Main public method to start the scraping and download process.
        Matches the call in main.py
        """
        print(f"\n🚀 STARTING DOWNLOAD AT: {self.output_dir}")
        self._initialize_report()
        
        # Initialize browser using utility
        driver = get_chrome_driver(headless=self.headless)
        
        if not driver: return

        try:
            files_to_download = self._extract_links(driver)
            if not files_to_download:
                print("⚠️ No files found to download.")
                return

            print(f"📊 Total files found: {len(files_to_download)}")
            
            for idx, (period, activity, url) in enumerate(files_to_download, 1):
                # Structure: data/raw/denue/2024_11/commerce.zip
                dest_folder = self.output_dir / period
                dest_folder.mkdir(parents=True, exist_ok=True)
                
                filename = f"{activity}.zip"
                final_path = dest_folder / filename
                
                print(f"[{idx}/{len(files_to_download)}] ⬇️ {filename}")
                
                status, reason = self._download_with_requests(url, final_path)
                
                # Append result to CSV report
                self._append_to_report([filename, str(final_path), url, status, reason])
                
                if status == "nok":
                    print(f"    ❌ {reason}")
                
                time.sleep(0.5)

        finally:
            driver.quit()
            print("\n✅ Process finished.")
            print(f"📄 Report saved at: {self.report_path}")

    def retry(self, report_csv: Path):
        """
        TODO: Implement retry logic reading from the provided CSV path.
        """
        pass