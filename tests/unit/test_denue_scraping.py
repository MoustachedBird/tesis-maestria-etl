"""Tests del scraping de la tabla de descargas de DENUE.

Cubren el contrato de reintentos entre corridas: lo que quedo SUCCESS se
salta, y lo que quedo FAILED (o nunca se intento) se vuelve a encolar. Usan
dobles del driver de Selenium y del tracker, asi que no necesitan navegador
ni Postgres.
"""

from pathlib import Path

import pytest

from src.etl.utils.denue_scraping import (
    ScrapeIncompleteError,
    clean_filename,
    scrape_denue_table,
)
from src.shared.enums import TaskStatus

BASE_URL = "https://www.inegi.org.mx"

# Tres archivos del catalogo: uno ya descargado con exito, uno que fallo en
# una corrida previa y uno que nunca se intento.
OK_HREF = "/contenidos/masiva/denue/denue_00_23_csv.zip"
FAILED_HREF = "/contenidos/masiva/denue/denue_00_43_csv.zip"
NEW_HREF = "/contenidos/masiva/denue/denue_00_51_csv.zip"


def _row(activity: str, period: str, href: str | None) -> str:
    """Una fila de la tabla de INEGI; `href=None` imita una fila sin enlace."""
    link = f'<a href="{href}">Descargar</a>' if href else "Sin archivo"
    return f"<tr><td>{activity}</td><td>{period}</td><td>{link}</td></tr>"


def _page(rows: list[str]) -> str:
    """HTML minimo con la tabla que busca el scraper."""
    return (
        "<html><body><table id='tblDescargaArchivos_denue'><tbody>"
        + "".join(rows)
        + "</tbody></table></body></html>"
    )


class FakeDriver:
    """
    Driver de Selenium de mentiras.

    `zip_links` es lo que reporta `find_elements` (la condicion de espera) y
    `page_source` lo que parsea BeautifulSoup. Se pueden desacoplar para
    simular una tabla a medio renderizar.
    """

    def __init__(self, html: str, zip_links: int | None = None):
        self.page_source = html
        self._zip_links = html.count("csv.zip") if zip_links is None else zip_links

    def find_elements(self, *args):
        """Acepta la firma (by, selector) de Selenium; el conteo la ignora."""
        return [object()] * self._zip_links


class FakeTracker:
    """Tracker en memoria: solo lo que toca el scraper."""

    def __init__(self, successful_urls: set[str] | None = None):
        self.successful_urls = successful_urls or set()
        self.created: list[dict] = []
        self.completed: list[dict] = []

    def get_successful_download_urls(self) -> set[str]:
        return set(self.successful_urls)

    def create_task(self, **kwargs) -> str:
        task_id = f"task-{len(self.created)}"
        self.created.append({"task_id": task_id, **kwargs})
        return task_id

    def complete_task(self, **kwargs) -> None:
        self.completed.append(kwargs)


@pytest.fixture
def catalog_html() -> str:
    """Tabla con los tres casos mas una fila sin enlace de descarga."""
    return _page(
        [
            _row("Construccion", "05/2026", OK_HREF),
            _row("Comercio al por mayor", "05/2026", FAILED_HREF),
            _row("Informacion en medios masivos", "05/2026", NEW_HREF),
            _row("Diccionario de datos", "2016", None),
        ]
    )


def _scrape(driver, tracker, tmp_path: Path, force: bool = False):
    return scrape_denue_table(
        driver=driver,
        wait_timeout=1,
        tracker=tracker,
        run_id="run-1",
        output_dir=tmp_path,
        base_url_inegi=BASE_URL,
        force=force,
    )


def test_reintenta_los_fallidos_y_baja_los_nuevos(catalog_html, tmp_path):
    """El contrato entre corridas: SUCCESS se salta, FAILED se reintenta.

    `get_successful_download_urls` solo devuelve SUCCESS, asi que un archivo
    fallido no esta en ese set y vuelve a la cola junto con los nuevos. Es lo
    que permite relanzar el ETL sin flags para completar una corrida
    interrumpida.
    """
    # Solo el primero quedo SUCCESS; el segundo fallo y el tercero es nuevo.
    tracker = FakeTracker(successful_urls={f"{BASE_URL}{OK_HREF}"})

    tasks = _scrape(FakeDriver(catalog_html, zip_links=200), tracker, tmp_path)

    urls = {url for _, _, url in tasks}
    assert f"{BASE_URL}{OK_HREF}" not in urls, "lo ya descargado no se reencola"
    assert f"{BASE_URL}{FAILED_HREF}" in urls, "el fallido se reintenta"
    assert f"{BASE_URL}{NEW_HREF}" in urls, "el nuevo se encola"


def test_archivo_en_disco_se_adopta_sin_redescargar(catalog_html, tmp_path, monkeypatch):
    """Un ZIP presente en Bronze sin registro se sincroniza, no se baja otra vez."""
    # El scraper guarda `target_path` relativo al cwd, asi que Bronze tiene
    # que quedar debajo de el (en produccion: cwd=/app, Bronze=/app/data/...).
    monkeypatch.chdir(tmp_path)

    dest = tmp_path / "05_2026" / "comercio_al_por_mayor.zip"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"zip")

    tracker = FakeTracker(successful_urls={f"{BASE_URL}{OK_HREF}"})
    tasks = _scrape(FakeDriver(catalog_html, zip_links=200), tracker, tmp_path)

    urls = {url for _, _, url in tasks}
    assert f"{BASE_URL}{FAILED_HREF}" not in urls, "el que esta en disco no se reencola"
    assert any(c["status"] == TaskStatus.SUCCESS for c in tracker.completed)


def test_parcial_en_tmp_no_cuenta_como_descargado(catalog_html, tmp_path):
    """Un `.tmp` a medias no se confunde con el ZIP final.

    El downloader baja a `.tmp/{nombre}.zip.tmp` y solo renombra al destino
    al completar, asi que una descarga interrumpida deja el parcial fuera de
    la ruta que mira el scraper y el archivo se reintenta.
    """
    partial = tmp_path / ".tmp" / "comercio_al_por_mayor.zip.tmp"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"incompleto")

    tracker = FakeTracker(successful_urls={f"{BASE_URL}{OK_HREF}"})
    tasks = _scrape(FakeDriver(catalog_html, zip_links=200), tracker, tmp_path)

    assert f"{BASE_URL}{FAILED_HREF}" in {url for _, _, url in tasks}


def test_fila_sin_enlace_queda_registrada_como_fallida(catalog_html, tmp_path):
    """Una fila sin .zip se anota FAILED en metadata, no se ignora en silencio."""
    tracker = FakeTracker()
    _scrape(FakeDriver(catalog_html, zip_links=200), tracker, tmp_path)

    failed = [c for c in tracker.completed if c["status"] == TaskStatus.FAILED]
    assert len(failed) == 1
    assert "No ZIP link found" in failed[0]["error_message"]


def test_tabla_a_medio_renderizar_levanta_error(tmp_path):
    """Una tabla con solo filas placeholder y cero enlaces es un error.

    Sin enlaces no se puede concluir que no haya archivos nuevos, asi que
    revienta para que el extractor marque el run FAILED en lugar de cerrarlo
    limpio con 0 tareas.
    """
    html = _page(
        [
            _row("Diccionario de datos", "2016", None),
            _row("Mdm complemento", "2016", None),
        ]
    )

    with pytest.raises(ScrapeIncompleteError, match="enlaces .zip"):
        _scrape(FakeDriver(html, zip_links=0), FakeTracker(), tmp_path)


def test_force_reencola_todo_incluido_lo_exitoso(catalog_html, tmp_path):
    """Con force se ignora el estado previo: baja el catalogo completo."""
    tracker = FakeTracker(successful_urls={f"{BASE_URL}{OK_HREF}"})
    tasks = _scrape(FakeDriver(catalog_html, zip_links=200), tracker, tmp_path, force=True)

    assert f"{BASE_URL}{OK_HREF}" in {url for _, _, url in tasks}
    assert len(tasks) == 3


def test_clean_filename_normaliza_acentos_y_espacios():
    """El nombre del archivo sale del texto de la tabla, que trae acentos."""
    assert clean_filename("Información en Medios Masivos") == "informacion_en_medios_masivos"
    assert clean_filename("") == "unnamed_file"
