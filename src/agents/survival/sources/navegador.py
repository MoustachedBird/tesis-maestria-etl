"""Leer con un navegador real lo que `requests` no puede: Google Maps, Facebook, Instagram.

Son las dos fuentes que mejor contestan "¿sigue abierto?" —Maps con su
estado explícito ("Cerrado permanentemente") y las redes con su última
publicación— y las dos se arman con JavaScript: a `paginas.leer_pagina` le
llegan vacías. Aquí se abren en Chrome headless (el de la imagen `etl`) y se
devuelve el texto que vería una persona.

Tres decisiones:

- **Sin sesión.** No se inicia sesión en ninguna parte: se lee lo que la
  página muestra a un visitante anónimo. Una página que redirige al login
  se devuelve como `ERROR`, igual que en `paginas.py`.
- **Un solo Chrome por corrida**, que se abre al primer uso y se reusa entre
  negocios. Si una lectura lo tumba se descarta y la siguiente abre otro: un
  navegador colgado no debe llevarse los negocios que faltan.
- **Las fechas relativas se anotan.** Maps y Facebook dicen "hace 2 meses",
  no una fecha, y el verificador rechaza toda fecha que no esté escrita en
  lo visto. Como esta página se acaba de renderizar, "hace 2 meses" sí se
  puede resolver contra hoy sin que el modelo deduzca nada: se anota como
  "hace 2 meses [≈ 2026-09]". En el extracto de un buscador NO se hace,
  porque ahí "hace 2 meses" es relativo al día en que el motor rastreó la
  página, que no se conoce.

Los errores se devuelven como TEXTO, por la misma razón que en `paginas.py`.
Un bloqueo anti-bot (CAPTCHA, "tráfico inusual") también es `ERROR`
(`cortesia.detectar_bloqueo`), y entre dos páginas del mismo dominio se
espera (`cortesia.Pausador`).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from src.agents.survival.sources.cortesia import Bloqueado, Pausador, detectar_bloqueo
from src.agents.survival.sources.paginas import SIN_CONTENIDO, leer_pagina
from src.shared.logging import get_stage_logger

logger = get_stage_logger(__name__, Path("./data/agente.log"))

_DOMINIOS_CON_JS: tuple[str, ...] = (
    "google.com/maps",
    "maps.google",
    "maps.app.goo.gl",
    "goo.gl/maps",
    "facebook.com",
    "fb.com",
    "fb.me",
    "instagram.com",
)
"""URLs que solo entregan contenido con JavaScript: van directo al navegador.

El resto entra primero por `requests` (más rápido, sin Chrome) y solo cae
al navegador si llega vacío (`leer_con_respaldo`).
"""

_MAPS_BUSQUEDA = "https://www.google.com/maps/search/{consulta}?hl=es"
"""Búsqueda de Google Maps por texto; con un solo resultado redirige a la ficha del lugar."""

_RELATIVA = re.compile(
    r"\bhace\s+(un|una|\d+)\s+(d[ií]as?|semanas?|mes(?:es)?|a[ñn]os?)\b", re.IGNORECASE
)
"""Una fecha relativa como las escriben Maps y Facebook en español."""

_AVISOS: tuple[str, ...] = (
    "button[aria-label='Rechazar todo']",
    "button[aria-label='Aceptar todo']",
    "div[role='dialog'] [aria-label='Cerrar']",
    "div[role='dialog'] [aria-label='Close']",
)
"""Botones que quitan lo que tapa el contenido: el consentimiento de Google y el aviso de login de Facebook."""


def necesita_navegador(url: str) -> bool:
    """Si la URL es de un sitio que solo se lee con JavaScript."""
    u = url.lower()
    dominio = urlparse(u).netloc
    return any(f in u if "/" in f else f in dominio for f in _DOMINIOS_CON_JS)


def anotar_fechas_relativas(texto: str, hoy: date) -> str:
    """Agrega el mes aproximado a cada "hace N días/semanas/meses/años".

    Args:
        texto: lo que se leyó de una página recién renderizada.
        hoy: el día de la lectura.

    Returns:
        str: el mismo texto con `[≈ AAAA-MM]` tras cada fecha relativa.
    """

    def anotar(m: re.Match) -> str:
        n = 1 if m.group(1).lower() in ("un", "una") else int(m.group(1))
        unidad = m.group(2).lower()
        if unidad.startswith("d"):
            fecha = hoy - timedelta(days=n)
        elif unidad.startswith("s"):
            fecha = hoy - timedelta(weeks=n)
        elif unidad.startswith("m"):
            meses = hoy.year * 12 + hoy.month - 1 - n
            fecha = date(meses // 12, meses % 12 + 1, 1)
        else:
            fecha = date(hoy.year - n, hoy.month, 1)
        return f"{m.group(0)} [≈ {fecha:%Y-%m}]"

    return _RELATIVA.sub(anotar, texto)


def _chrome(timeout: int, perfil: str | None = None) -> Any:
    """Un Chrome headless en español de México, sin sesión.

    No reusa `src.etl.utils.browser`: los agentes no importan del ETL
    (`tests/unit/test_layer_boundaries.py`).

    Args:
        timeout: segundos máximos para cargar una página.
        perfil: carpeta de un perfil persistente (solo cookies de
            consentimiento: nunca se inicia sesión); None = desechable.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    opciones = Options()
    if perfil:
        carpeta = Path(perfil).resolve()
        carpeta.mkdir(parents=True, exist_ok=True)
        # Un Chrome que murio sin cerrar deja el candado y el siguiente no
        # arranca con "user data directory is already in use". Solo hay un
        # Chrome por corrida, asi que el candado siempre es de uno muerto.
        for candado in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            (carpeta / candado).unlink(missing_ok=True)
        opciones.add_argument(f"--user-data-dir={carpeta}")
    opciones.add_argument("--headless=new")
    # Sin estas tres Chrome no arranca dentro de un contenedor.
    opciones.add_argument("--no-sandbox")
    opciones.add_argument("--disable-dev-shm-usage")
    opciones.add_argument("--disable-gpu")
    opciones.add_argument("--window-size=1280,2000")
    opciones.add_argument("--lang=es-MX")
    opciones.add_experimental_option(
        "prefs",
        {
            "intl.accept_languages": "es-MX,es",
            "profile.default_content_setting_values.notifications": 2,
            "profile.default_content_setting_values.geolocation": 2,
        },
    )
    driver = webdriver.Chrome(options=opciones)
    driver.set_page_load_timeout(timeout)
    return driver


class Navegador:
    """Un Chrome headless que se abre al primer uso y se reusa entre lecturas."""

    def __init__(
        self,
        timeout: int = 20,
        espera: float = 5.0,
        *,
        perfil: str | None = None,
        pausador: Pausador | None = None,
        fabrica: Callable[[int], Any] | None = None,
        hoy: Callable[[], date] = date.today,
    ):
        """
        Args:
            timeout: segundos máximos para cargar una página.
            espera: segundos máximos que se espera a que el JavaScript pinte
                el contenido después de cargar.
            perfil: carpeta del perfil persistente de Chrome; None = desechable.
            pausador: la pausa por dominio de la corrida; None = sin pausa.
            fabrica: cómo se crea el driver; se inyecta en los tests.
            hoy: el día de la lectura; se inyecta en los tests.
        """
        self.timeout = timeout
        self.espera = espera
        self._fabrica = fabrica or (lambda t: _chrome(t, perfil))
        self._pausador = pausador
        self._hoy = hoy
        self._driver: Any = None
        self.ultima_url: str | None = None
        """A dónde llegó la última lectura tras las redirecciones (Maps redirige a la ficha)."""

    def comprobar(self) -> None:
        """Arranca Chrome; lanza si no se puede (imagen sin navegador, sin memoria compartida)."""
        self._abrir()

    def cerrar(self) -> None:
        """Cierra Chrome si está abierto; no falla si ya murió."""
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:  # noqa: BLE001, S110  (ya estaba muerto: no hay nada que cerrar)
                pass
            self._driver = None

    def leer(self, url: str, max_caracteres: int = 6000, min_caracteres: int = 200) -> str:
        """El texto visible de una página tras ejecutar su JavaScript.

        Args:
            url: qué abrir.
            max_caracteres: tope del texto devuelto.
            min_caracteres: texto visible mínimo para que cuente como leída.

        Returns:
            str: el texto (con las fechas relativas anotadas), o `ERROR: ...`.
        """
        try:
            driver = self._ir(url)
            texto = self._texto_cuando_pinte(driver, min_caracteres)
            destino = driver.current_url
            titulo = (driver.title or "").strip()
        except Exception as e:  # noqa: BLE001  (un Chrome roto es un ERROR para el modelo, no una caida)
            self.cerrar()
            return f"ERROR: el navegador no pudo abrir {url} ({e.__class__.__name__})"
        self.ultima_url = destino

        motivo = detectar_bloqueo(f"{titulo} {texto}", destino)
        if motivo:
            logger.info(f"         ⛔ bloqueo en {url[:120]}: {motivo}")
            return f"ERROR: {url} respondió con un bloqueo anti-bot ({motivo}). NO la leíste."
        if "/login" in destino or "accounts.google.com" in destino:
            return f"ERROR: {url} pide iniciar sesión. NO la leíste."
        if len(texto) < min_caracteres:
            return (
                f"ERROR: {url} {SIN_CONTENIDO} ni con navegador ({len(texto)} caracteres). "
                "NO la leíste: de esta URL solo sabes lo que mostró el buscador."
            )
        logger.info(f"         🌐 leída: {len(texto):,} caracteres · {titulo[:80]!r}")
        texto = anotar_fechas_relativas(texto, self._hoy())
        cuerpo = f"TITULO: {titulo}\n{texto}" if titulo else texto
        if len(cuerpo) > max_caracteres:
            return cuerpo[:max_caracteres] + f"\n[... recortado a {max_caracteres} caracteres]"
        return cuerpo

    def buscar_en_maps(
        self, consulta: str, max_caracteres: int = 6000, min_caracteres: int = 200
    ) -> tuple[str, str]:
        """Busca en Google Maps y devuelve lo que muestra: la ficha, o la lista de resultados.

        Args:
            consulta: nombre y lugar, como se escribiría en el buscador de Maps.
            max_caracteres: tope del texto devuelto.
            min_caracteres: texto visible mínimo para que cuente como leída.

        Returns:
            tuple[str, str]: la URL de la búsqueda (la que se cita como
            evidencia) y el texto o el `ERROR`.
        """
        url = _MAPS_BUSQUEDA.format(consulta=quote(consulta.strip()))
        texto = self.leer(url, max_caracteres, min_caracteres)
        if not texto.startswith("ERROR"):
            self._esperar_ficha()
        return url, texto

    def _esperar_ficha(self) -> None:
        """Maps cambia la URL a `/maps/place/` un momento después de pintar la ficha: se espera.

        Sin esto `ultima_url` se queda en `/maps/search/` y la fuente no sabe
        que cayó en una ficha (no lee sus reseñas). Si en `espera` segundos no
        cambia, era una lista de resultados.
        """
        limite = time.monotonic() + self.espera
        while time.monotonic() < limite:
            try:
                actual = self._abrir().current_url
            except Exception:  # noqa: BLE001  (sin driver no hay que esperar)
                return
            if "/maps/place/" in actual:
                self.ultima_url = actual
                return
            time.sleep(0.5)

    def html(self, url: str, min_caracteres: int = 200) -> str:
        """El HTML de una página tras ejecutar su JavaScript: para leer resultados de un buscador.

        Raises:
            Bloqueado: si la página es un CAPTCHA o similar.
            RuntimeError: si el navegador no pudo abrirla.
        """
        try:
            driver = self._ir(url)
            texto = self._texto_cuando_pinte(driver, min_caracteres)
            destino = driver.current_url
            fuente = driver.page_source or ""
            texto = f"{driver.title or ''} {texto}"
        except Exception as e:
            self.cerrar()
            raise RuntimeError(f"el navegador no pudo abrir {url} ({e.__class__.__name__})") from e
        self.ultima_url = destino
        motivo = detectar_bloqueo(texto, destino)
        if motivo:
            raise Bloqueado(urlparse(url).netloc, motivo)
        return fuente

    def resenas_recientes(self, max_caracteres: int = 6000, min_caracteres: int = 200) -> str:
        """En la ficha de Maps abierta, las reseñas ordenadas de la más reciente a la más vieja.

        La ficha muestra las reseñas "más relevantes", que pueden tener años;
        lo que fecha la actividad es la más reciente. Se hace lo que haría una
        persona: pestaña Reseñas, Ordenar, Más recientes. Se ubican los
        botones por su etiqueta en español o inglés y no por clases CSS.

        Returns:
            str: el texto del panel con las fechas relativas anotadas, o `ERROR: ...`.
        """
        driver = self._abrir()
        logger.info("         🌐 Maps: pestaña Reseñas → Ordenar → Más recientes")
        if not _clic(driver, "button[role='tab']", ("reseñas", "opiniones", "reviews")):
            return "ERROR: la ficha de Maps no tiene pestaña de reseñas."
        time.sleep(min(self.espera, 2.0))
        if _clic(driver, "button", ("ordenar", "sort")):
            time.sleep(1.0)
            _clic(driver, "[role='menuitemradio'], [role='menuitem']", ("recientes", "newest"))
            time.sleep(min(self.espera, 2.0))
        for _ in range(2):  # cargar unas reseñas más
            try:
                driver.execute_script(
                    'const p = document.querySelector(\'div[role="main"] div[tabindex="-1"]\');'
                    "if (p) p.scrollTop = p.scrollHeight;"
                )
            except Exception:  # noqa: BLE001, S112  (sin panel desplazable: se lee lo que haya)
                break
            time.sleep(1.0)
        try:
            crudo = driver.execute_script(
                "const m = document.querySelector('div[role=\"main\"]');"
                "return m ? m.innerText : (document.body ? document.body.innerText : '');"
            )
        except Exception as e:  # noqa: BLE001  (un Chrome roto es un ERROR para el modelo)
            self.cerrar()
            return f"ERROR: el navegador falló al leer las reseñas ({e.__class__.__name__})"
        texto = re.sub(r"\s+", " ", crudo or "").strip()
        if len(texto) < min_caracteres:
            return f"ERROR: el panel de reseñas {SIN_CONTENIDO} ({len(texto)} caracteres)."
        texto = anotar_fechas_relativas(texto, self._hoy())
        if len(texto) > max_caracteres:
            return texto[:max_caracteres] + f"\n[... recortado a {max_caracteres} caracteres]"
        return texto

    # ----- internos -----

    def _ir(self, url: str) -> Any:
        """Abre `url` respetando la pausa por dominio."""
        if self._pausador is not None:
            self._pausador.esperar(url)
        logger.info(f"         🌐 Chrome abre {url[:150]}")
        driver = self._abrir()
        driver.get(url)
        return driver

    def _abrir(self) -> Any:
        if self._driver is None:
            self._driver = self._fabrica(self.timeout)
        return self._driver

    def _texto_cuando_pinte(self, driver: Any, min_caracteres: int) -> str:
        """Espera a que el texto visible deje de crecer (o se acabe la espera) y lo devuelve.

        No se espera un elemento concreto: Maps y Facebook cambian su HTML
        seguido, y un selector fijo rompería el lector en silencio. "El texto
        ya no crece" no depende del sitio.
        """
        limite = time.monotonic() + self.espera
        previo = -1
        texto = ""
        while True:
            self._quitar_avisos(driver)
            crudo = driver.execute_script("return document.body ? document.body.innerText : ''")
            texto = re.sub(r"\s+", " ", crudo or "").strip()
            if len(texto) >= min_caracteres and len(texto) == previo:
                return texto
            if time.monotonic() >= limite:
                return texto
            previo = len(texto)
            time.sleep(0.5)

    def _quitar_avisos(self, driver: Any) -> None:
        """Cierra el consentimiento de Google y el aviso de login de Facebook, si están."""
        for selector in _AVISOS:
            try:
                for boton in driver.find_elements("css selector", selector)[:1]:
                    boton.click()
            except Exception:  # noqa: BLE001, S112  (el aviso no esta o no se deja: se lee lo que haya)
                continue


def _clic(driver: Any, selector: str, etiquetas: tuple[str, ...]) -> bool:
    """Hace clic en el primer elemento de `selector` cuya etiqueta o texto contiene una de `etiquetas`."""
    try:
        elementos = driver.find_elements("css selector", selector)
    except Exception:  # noqa: BLE001  (sin elementos: no hay en que hacer clic)
        return False
    for el in elementos:
        try:
            rotulo = f"{el.get_attribute('aria-label') or ''} {el.text or ''}".lower()
            if any(e in rotulo for e in etiquetas):
                el.click()
                return True
        except Exception:  # noqa: BLE001, S112  (elemento que desaparecio o no se deja: el siguiente)
            continue
    return False


def leer_con_respaldo(
    url: str,
    navegador: Navegador,
    max_caracteres: int = 6000,
    timeout: int = 20,
    min_caracteres: int = 200,
) -> str:
    """Lee una página por la vía más barata que la entregue.

    Maps y las redes van directo al navegador. Lo demás entra por `requests`
    y solo si llega sin contenido (un sitio que se arma con JavaScript) se
    reintenta con el navegador.

    Args:
        url: qué leer.
        navegador: el Chrome de la corrida.
        max_caracteres: tope del texto devuelto.
        timeout: segundos por petición de `requests`.
        min_caracteres: texto visible mínimo para que cuente como leída.

    Returns:
        str: el texto, o una línea `ERROR: ...`.
    """
    if necesita_navegador(url):
        return navegador.leer(url, max_caracteres, min_caracteres)
    texto = leer_pagina(url, max_caracteres, timeout, min_caracteres)
    if texto.startswith("ERROR") and SIN_CONTENIDO in texto:
        return navegador.leer(url, max_caracteres, min_caracteres)
    return texto
