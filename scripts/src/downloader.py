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
# NEW: Manifest Manager for Architecture Compatibility
from scripts.utils.manifest import ManifestManager

class InegiDownloader:
    def __init__(self, output_dir: Path, headless: bool = True):
        """
        Initializes the DENUE downloader with Manifest support.
        """
        self.output_dir = output_dir
        self.headless = headless
        self.base_url = "https://www.inegi.org.mx/app/descarga/"
        self.base_url_inegi = "https://www.inegi.org.mx"
        self.wait_timeout = 30
        
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.report_path = self.output_dir / "report_download.csv"
        
        # In-memory registry of existing successful downloads
        self.existing_downloads = {}

    def _load_and_audit_report(self):
        """
        Reads existing report. 
        Verifies if files marked as 'ok' actually exist on disk.
        If missing, marks them as 'nok' to re-download.
        Returns a list of all rows (including updates).
        """
        if not self.report_path.exists():
            print(f"📄 No existing report found. Starting fresh.")
            # Create header with 'date' column
            with open(self.report_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(["date", "filename", "destination_path", "download_url", "status", "reason"])
            return []

        print(f"🔍 Auditing existing report: {self.report_path.name}")
        rows = []
        modified = False

        try:
            with open(self.report_path, 'r', newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames
                
                # Check if 'date' column exists (migration support)
                if 'date' not in fieldnames:
                    print("⚠️ Old report format detected. Recreating headers...")
                    fieldnames = ["date", "filename", "destination_path", "download_url", "status", "reason"]
                    modified = True

                for row in reader:
                    # Logic: If status is OK, verify file existence
                    if row.get('status') in ['ok', 'retry-ok']:
                        dest_path = Path(row['destination_path'])
                        if not dest_path.exists():
                            print(f"    ❌ MISSING FILE: {row['filename']} (Marked OK but not found)")
                            row['status'] = 'nok'
                            row['reason'] = 'Audit: File missing on disk'
                            modified = True
                        else:
                            # Register as valid download to skip later
                            self.existing_downloads[row['download_url']] = str(dest_path)
                    
                    if 'date' not in row:
                        row['date'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    
                    rows.append(row)
        
        except Exception as e:
            print(f"❌ Error reading report: {e}. Starting fresh.")
            return []

        if modified:
            print("💾 Saving audited report changes...")
            self._write_rows(rows, fieldnames)
            
        return rows

    def _write_rows(self, rows, fieldnames=None):
        if not fieldnames:
             fieldnames = ["date", "filename", "destination_path", "download_url", "status", "reason"]
             
        with open(self.report_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def _append_to_report(self, row_dict):
        fieldnames = ["date", "filename", "destination_path", "download_url", "status", "reason"]
        if 'date' not in row_dict:
            row_dict['date'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        try:
            with open(self.report_path, 'a', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writerow(row_dict)
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
            return "nok", str(e)[:100]

    def _extract_and_filter_links(self, driver):
        print(f"🔍 Navigating to {self.base_url}...")
        driver.get(self.base_url)
        wait = WebDriverWait(driver, self.wait_timeout)
        new_downloads_list = []

        try:
            print("  ... Waiting for table 'tblDescargaArchivos_denue'...")
            wait.until(EC.presence_of_element_located((By.ID, "tblDescargaArchivos_denue")))
            time.sleep(3)

            soup = BeautifulSoup(driver.page_source, 'html.parser')
            table = soup.find('table', id='tblDescargaArchivos_denue')
            
            if not table or not table.find('tbody'):
                return []

            rows = table.find('tbody').find_all('tr')
            print(f"  ... Processing {len(rows)} rows found.")

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
                    
                    if url in self.existing_downloads:
                        skipped_count += 1
                        continue
                        
                    new_downloads_list.append((period, activity, url))
                else:
                    err_row = {
                        "filename": f"{activity}.zip", 
                        "destination_path": "N/A", 
                        "download_url": "N/A", 
                        "status": "nok", 
                        "reason": "No zip link found"
                    }
                    self._append_to_report(err_row)
            
            if skipped_count > 0:
                print(f"⏩ Skipped {skipped_count} files (Already exist locally).")
            
            return new_downloads_list

        except TimeoutException:
            print("❌ Timeout: Table did not load.")
            return []
        except Exception as e:
            print(f"❌ Scraping error: {e}")
            return []

    def download_all(self):
        print(f"\n🚀 STARTING SMART DOWNLOAD AT: {self.output_dir}")
        self._load_and_audit_report()
        driver = get_chrome_driver(headless=self.headless)

        if not driver: return

        try:
            files_to_download = self._extract_and_filter_links(driver)
            
            if not files_to_download:
                print("✨ System up to date. No new files to download.")
            else:
                print(f"📊 New files to download: {len(files_to_download)}")
                for idx, (period, activity, url) in enumerate(files_to_download, 1):
                    dest_folder = self.output_dir / period
                    dest_folder.mkdir(parents=True, exist_ok=True)
                    filename = f"{activity}.zip"
                    final_path = dest_folder / filename
                    
                    print(f"[{idx}/{len(files_to_download)}] ⬇️ {filename}")
                    status, reason = self._download_with_requests(url, final_path)
                    
                    if status == "nok":
                        print(f"    ❌ {reason}")
                    
                    row_data = {
                        "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        "filename": filename,
                        "destination_path": str(final_path),
                        "download_url": url,
                        "status": status,
                        "reason": reason
                    }
                    self._append_to_report(row_data)
                    time.sleep(0.5)
        finally:
            if driver: driver.quit()
            print("\n✅ Initial phase finished.")

        print(f"\n🔍 Checking for any pending fixes (Audit + New Failures)...")
        self.retry()
        
        # --- NEW: UPDATE GLOBAL MANIFEST ---
        self._sync_manifest()

    def retry(self):
        if not self.report_path.exists(): return
        print(f"\n🔄 STARTING RETRY CHECK ON: {self.report_path.name}")
        
        rows = []
        try:
            with open(self.report_path, 'r', newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames
                rows = list(reader)
        except Exception: return

        updated_count = 0
        for row in rows:
            if row['status'] == 'nok':
                filename = row['filename']
                url = row['download_url']
                dest_path_str = row['destination_path']
                
                if not url or url == "N/A" or not dest_path_str: continue

                dest_path = Path(dest_path_str)
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                
                print(f"--- Retrying: {filename} ---")
                success = False
                last_reason = ""
                
                for attempt in range(1, 4):
                    print(f"    👉 Attempt {attempt}/3...")
                    status, reason = self._download_with_requests(url, dest_path)
                    if status == "ok":
                        success = True
                        print(f"    ✅ SUCCESS!")
                        break
                    else:
                        last_reason = reason
                        time.sleep(2)
                
                row['date'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                if success:
                    row['status'] = 'retry-ok'
                    row['reason'] = 'Retry downloaded successfully'
                    updated_count += 1
                else:
                    row['reason'] = f"Failed after 3 attempts. Last error: {last_reason}"

        if updated_count > 0:
            self._write_rows(rows, fieldnames)
            print(f"✅ Fixed {updated_count} files.")
        else:
            print("✨ No files needed fixing.")

    def _sync_manifest(self):
        """
        Updates the global manifest_download.csv so the Processor knows about new files immediately.
        """
        try:
            print(f"\n📦 Syncing with Global Manifest (manifest_download.csv)...")
            # Calculate Data Root: ../data/raw/denue -> ../data
            data_root = self.output_dir.parent.parent
            
            manifest_mgr = ManifestManager(data_root)
            manifest_mgr.init_raw_manifest(self.output_dir)
            print("✅ Global Manifest synced successfully.")
        except Exception as e:
            print(f"⚠️ Warning: Could not sync global manifest: {e}")
            print("   (The processor will auto-detect files next time anyway)")