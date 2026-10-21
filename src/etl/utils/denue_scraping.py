"""
DENUE web scraping utilities.

Scrapes the INEGI DENUE download page and synchronizes with metadata database.
"""

import re
import unicodedata
from pathlib import Path

from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from src.meta.tracker import MetadataTracker
from src.shared.config import settings
from src.shared.enums import TaskStatus

TABLE_ID = "tblDescargaArchivos_denue"
"""ID de la tabla de descargas en la pagina de INEGI."""

ZIP_LINK_RE = re.compile(r"csv\.zip")
"""Patron de los enlaces de descarga; filas sin esto no son descargables."""


class ScrapeIncompleteError(RuntimeError):
    """
    La tabla de INEGI no entrego el catalogo completo.

    Se levanta cuando la tabla no alcanza `DENUE_SCRAPE_MIN_LINKS` enlaces
    .zip dentro del timeout. Tiene que ser un error y no una lista vacia: sin
    enlaces no hay nada que descargar, pero tampoco se puede concluir que no
    haya archivos nuevos, y el run debe quedar FAILED en lugar de limpio.
    """


def _count_zip_links(driver) -> int:
    """
    Cuenta los enlaces .zip presentes en la tabla en este instante.

    Se usa como condicion de espera: mide poblacion real de la tabla, no la
    existencia del elemento, que aparece vacio mucho antes que el AJAX.
    """
    return len(driver.find_elements(By.CSS_SELECTOR, f"#{TABLE_ID} tbody a[href*='csv.zip']"))


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
    project_root = Path.cwd()
    min_links = settings.DENUE_SCRAPE_MIN_LINKS

    # Espera por CONTENIDO, no por presencia del elemento: la tabla existe
    # casi de inmediato con filas placeholder y el catalogo entra despues
    # por AJAX. El timeout aqui es el tope total de esa espera.
    print(f"  ... Waiting for table '{TABLE_ID}' to reach {min_links}+ .zip links...")
    try:
        WebDriverWait(driver, wait_timeout).until(lambda d: _count_zip_links(d) >= min_links)
    except Exception as e:
        found = _count_zip_links(driver)
        raise ScrapeIncompleteError(
            f"La tabla de INEGI quedo con {found} enlaces .zip (minimo esperado: "
            f"{min_links}) tras {wait_timeout}s. La pagina no termino de cargar "
            f"o cambio de markup; no se descargo nada. Ajusta "
            f"DENUE_SCRAPE_TIMEOUT_SECONDS / DENUE_SCRAPE_MIN_LINKS si el "
            f"catalogo de INEGI cambio de tamano."
        ) from e

    # Parse table
    soup = BeautifulSoup(driver.page_source, "html.parser")
    table = soup.find("table", id=TABLE_ID)

    if not table or not table.find("tbody"):
        raise ScrapeIncompleteError(
            f"La tabla '{TABLE_ID}' no tiene <tbody> en el HTML renderizado; "
            "INEGI cambio el markup de la pagina de descargas."
        )

    rows = table.find("tbody").find_all("tr")
    print(f"  ... Processing {len(rows)} rows from INEGI website")

    try:
        # Get existing successful downloads from metadata (only for normal mode).
        # Cruza TODOS los runs: el run en curso todavia no tiene tareas
        # cuando se raspa la tabla, asi que filtrar por el (como se hacia
        # antes) dejaba el set vacio y mandaba cada archivo ya descargado a
        # la rama de "sync desde disco", que crea un registro SUCCESS nuevo
        # por corrida -- 943 registros para 484 archivos reales.
        existing_urls = set()
        if not force:
            existing_urls = tracker.get_successful_download_urls()

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
            link_tag = cols[2].find("a", href=ZIP_LINK_RE)

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
                    print(f"📝 Syncing from disk: {period}/{filename}")

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
        # Propaga: un scrape roto no es "0 archivos nuevos". El extractor
        # cierra el run como FAILED con este mensaje.
        print(f"❌ Fatal error in scraper: {str(e)}")
        import traceback

        traceback.print_exc()
        raise
