"""
DENUE web scraping utilities.

Scrapes the INEGI DENUE download page and synchronizes with metadata database.
"""

import re
import time
import unicodedata
from pathlib import Path

from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from src_metadata.models import TaskStatus
from src_metadata.tracker import MetadataTracker


def clean_filename(text: str) -> str:
    """
    Convert a raw string into a filesystem-safe filename.

    Example: 'Comercio al por Menor (2024).zip' -> 'comercio_al_por_menor_2024.zip'
    """
    if not text:
        return "unnamed_file"

    text = text.lower()
    text = text.replace(" ", "_").replace(".", "_")
    nfd_form = unicodedata.normalize("NFD", text)
    text_without_accents = "".join(c for c in nfd_form if unicodedata.category(c) != "Mn")
    clean_text = re.sub(r"[^a-z0-9_-]", "", text_without_accents)
    clean_text = re.sub(r"__+", "_", clean_text)
    return clean_text.strip("_")


def scrape_denue_table(
    driver,
    wait_timeout: int,
    tracker: MetadataTracker,
    run_id: str,
    output_dir: Path,
    base_url_inegi: str,
    force: bool = False,
) -> list[tuple[str, str, str]]:
    """
    Scrape INEGI DENUE table and synchronize with metadata database.

    Args:
        driver: Selenium WebDriver instance
        wait_timeout: Seconds to wait for table to load
        tracker: MetadataTracker instance
        run_id: Current job run ID
        output_dir: Base output directory for downloads
        base_url_inegi: Base URL for INEGI website
        force: If True, re-download ALL files ignoring existing state

    Returns:
        List of (period, activity, url) tuples for files that need downloading

    Logic:
        Normal mode:
            1. Scrape INEGI table for all available files
            2. Skip files already successfully downloaded (in metadata DB)
            3. Sync files found on disk but not in DB
            4. Return new files that need downloading

        Force mode:
            1. Scrape INEGI table for all available files
            2. Return ALL files for re-download (ignore metadata and disk)
    """
    new_tasks = []
    wait = WebDriverWait(driver, wait_timeout)
    project_root = Path.cwd()

    try:
        # Wait for table to load
        print("  ... Waiting for table 'tblDescargaArchivos_denue'...")
        wait.until(EC.presence_of_element_located((By.ID, "tblDescargaArchivos_denue")))
        time.sleep(3)  # Allow JS to finish rendering

        # Parse table
        soup = BeautifulSoup(driver.page_source, "html.parser")
        table = soup.find("table", id="tblDescargaArchivos_denue")

        if not table or not table.find("tbody"):
            print("❌ Error: Table body not found in HTML")
            return []

        rows = table.find("tbody").find_all("tr")
        print(f"  ... Processing {len(rows)} rows from INEGI website")

        # Get existing successful downloads from metadata (only for normal mode)
        existing_urls = set()
        if not force:
            existing_tasks = tracker.get_tasks_by_run(run_id) if run_id else []
            existing_urls = {
                task["source_url"]
                for task in existing_tasks
                if task["status"] == TaskStatus.SUCCESS.value
            }

        counts = {"skipped": 0, "synced": 0, "new": 0, "forced": 0}

        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 3:
                continue

            # Extract metadata
            activity_raw = cols[0].get_text(strip=True)
            if not activity_raw:
                continue

            activity = clean_filename(activity_raw)
            period = cols[1].get_text(strip=True).replace("/", "_")

            # Find ZIP link
            link_tag = cols[2].find("a", href=re.compile(r"csv\.zip"))

            if link_tag:
                url = f"{base_url_inegi}{link_tag['href']}"

                # FORCE MODE: Queue everything for download
                if force:
                    new_tasks.append((period, activity, url))
                    counts["forced"] += 1
                    continue

                # NORMAL MODE: Skip/sync/download logic
                # A. Skip if already in metadata as successful
                if url in existing_urls:
                    counts["skipped"] += 1
                    continue

                # B. Sync if file exists on disk but not in metadata
                filename = f"{activity}.zip"
                dest_path = output_dir / period / filename

                if dest_path.exists():
                    # File exists on disk - create metadata record
                    print(f"📝 Syncing from disk: {filename}")

                    task_id = tracker.create_task(
                        run_id=run_id,
                        task_name=f"{activity}_{period}",
                        source_url=url,
                        target_path=str(dest_path.relative_to(project_root)),
                        period=period,
                    )

                    # Mark as successful (found on disk)
                    tracker.complete_task(
                        task_id=task_id,
                        status=TaskStatus.SUCCESS,
                        actual_size_bytes=dest_path.stat().st_size,
                        validation_passed=True,
                    )

                    counts["synced"] += 1
                    continue

                # C. New task - not in DB and not on disk
                new_tasks.append((period, activity, url))
                counts["new"] += 1

            else:
                # D. Link not found in INEGI table
                print(f"⚠️  Link not found for: {activity} ({period})")

                # Create failed task record
                task_id = tracker.create_task(
                    run_id=run_id,
                    task_name=f"{activity}_{period}",
                    source_url="N/A",
                    target_path=f"MISSING_LINK/{activity}.zip",
                    period=period,
                )

                tracker.complete_task(
                    task_id=task_id,
                    status=TaskStatus.FAILED,
                    error_message="No ZIP link found in INEGI table",
                )

        # Display summary
        print("\n📊 Scraping Summary:")

        if force:
            print(f"   🔄 Force mode - queued for re-download: {counts['forced']}")
        else:
            print(f"   ⏩ Already in metadata: {counts['skipped']}")
            print(f"   🏠 Synced from disk: {counts['synced']}")
            print(f"   🆕 New files to download: {counts['new']}")

        return new_tasks

    except Exception as e:
        print(f"❌ Fatal error in scraper: {str(e)}")
        import traceback

        traceback.print_exc()
        return []
