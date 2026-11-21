"""Buscadores leídos como página: Bing, DuckDuckGo, Google y Brave, sin API.

Sus APIs son de paga (Brave), están cerradas a clientes nuevos (Google) o ya
no existen (Bing); DuckDuckGo nunca tuvo una de resultados web. Aquí se lee la
página de resultados que vería una persona:

| Motor | Cómo | Por qué así |
|---|---|---|
| Bing | `requests` sobre `bing.com/search` | Entrega HTML sin JavaScript |
| DuckDuckGo | `requests` sobre `html.duckduckgo.com/html` | Es su versión sin JavaScript |
| Google | Chrome | Sin JavaScript devuelve otra página, y bloquea a `requests` |
| Brave | Chrome | Igual |

Los cuatro devuelven `Resultado`, la misma forma que SearXNG, así que
`recoleccion.FuenteWeb` los usa sin saber cuál es.

**Qué se parsea y qué no.** El código solo saca título, URL y el texto del
bloque del resultado; no intenta entender el resultado. Los motores cambian
su HTML seguido, así que cada uno tiene su selector y, si no encuentra nada,
un respaldo genérico (`_por_titulos`: un enlace externo con un título
adentro). Si un motor cambia y ninguno de los dos encuentra nada, la pieza
sale `sin_resultados` y `sondear` lo delata.

**Un bloqueo no es "sin resultados".** Una página de CAPTCHA lanza
`Bloqueado`, que `BuscadorVigilado` cuenta para apagar al motor si se repite.
"""

from __future__ import annotations

import base64
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, quote_plus, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from src.agents.survival.sources.busqueda import Buscador, Resultado
from src.agents.survival.sources.cortesia import (
    Bloqueado,
    Cortacircuitos,
    Pausador,
    detectar_bloqueo,
)
from src.shared.logging import get_stage_logger

logger = get_stage_logger(__name__, Path("./data/agente.log"))

UA_NAVEGADOR = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
"""User-Agent de un Chrome de escritorio, para las páginas de resultados de Bing y DuckDuckGo.

A diferencia de `busqueda._UA`, aquí sí se presenta como navegador: con un
User-Agent de bot, Bing y DuckDuckGo responden otra página o un bloqueo, y la
idea es leer lo que vería una persona.
"""

_MAX_EXTRACTO = 500
"""Caracteres del bloque de un resultado que se guardan como extracto."""

_DOMINIOS_PROPIOS = (
    "google.",
    "gstatic.com",
    "googleusercontent.com",
    "bing.com",
    "microsoft.com",
    "msn.com",
    "duckduckgo.com",
    "brave.com",
)
"""Dominios del propio motor (navegación, anuncios, caché): no son resultados."""


class Navegante(Protocol):
    """Lo que un buscador con Chrome necesita del navegador (`navegador.Navegador.html`)."""

    def html(self, url: str, min_caracteres: int = ...) -> str: ...


# ----- con requests -----


class BuscadorBing:
    """bing.com por `requests`, en español de México."""

    nombre = "bing"
    _URL = "https://www.bing.com/search"

    def __init__(self, timeout: int = 20, pausador: Pausador | None = None):
        self.timeout = timeout
        self.pausador = pausador

    def buscar(self, consulta: str, n: int = 8) -> list[Resultado]:
        html = _pedir(
            self._URL,
            self.pausador,
            self.timeout,
            params={"q": consulta, "setlang": "es", "cc": "MX", "mkt": "es-MX"},
        )
        return _o_guardar(self.nombre, html, resultados_bing(html))[:n]


class BuscadorDuckDuckGo:
    """La versión HTML (sin JavaScript) de DuckDuckGo, región México."""

    nombre = "duckduckgo"
    _URL = "https://html.duckduckgo.com/html/"

    def __init__(self, timeout: int = 20, pausador: Pausador | None = None):
        self.timeout = timeout
        self.pausador = pausador

    def buscar(self, consulta: str, n: int = 8) -> list[Resultado]:
        html = _pedir(self._URL, self.pausador, self.timeout, datos={"q": consulta, "kl": "mx-es"})
        return resultados_duckduckgo(html)[:n]


# ----- con Chrome -----


class BuscadorGoogle:
    """google.com en Chrome, en español de México y sin personalizar."""

    nombre = "google"

    def __init__(self, navegador: Navegante):
        self.navegador = navegador

    def buscar(self, consulta: str, n: int = 8) -> list[Resultado]:
        url = f"https://www.google.com/search?q={quote_plus(consulta)}&hl=es&gl=mx&num=10&pws=0"
        html = self.navegador.html(url)
        return _o_guardar(self.nombre, html, resultados_google(html))[:n]


class BuscadorBravePagina:
    """search.brave.com en Chrome (la página, no la API de `busqueda.BuscadorBrave`)."""

    nombre = "brave"

    def __init__(self, navegador: Navegante):
        self.navegador = navegador

    def buscar(self, consulta: str, n: int = 8) -> list[Resultado]:
        url = f"https://search.brave.com/search?q={quote_plus(consulta)}&source=web"
        html = self.navegador.html(url)
        return _o_guardar(self.nombre, html, resultados_brave(html))[:n]


class BuscadorVigilado:
    """Un buscador bajo el cortacircuitos: lo apaga si bloquea seguido.

    Mismo `nombre` y misma interfaz que el que envuelve, así que la fuente no
    sabe que está vigilado.
    """

    def __init__(self, buscador: Buscador, cortacircuitos: Cortacircuitos):
        self.buscador = buscador
        self.cortacircuitos = cortacircuitos
        self.nombre = buscador.nombre

    def buscar(self, consulta: str, n: int = 8) -> list[Resultado]:
        """Busca, salvo que el motor ya esté apagado; un bloqueo se cuenta y se relanza."""
        if self.cortacircuitos.apagado(self.nombre):
            raise RuntimeError(
                f"{self.nombre} está apagado en esta corrida: bloqueó "
                f"{self.cortacircuitos.max_bloqueos} veces seguidas"
            )
        try:
            resultados = self.buscador.buscar(consulta, n)
        except Bloqueado:
            self.cortacircuitos.registrar(self.nombre, bloqueado=True)
            raise
        self.cortacircuitos.registrar(self.nombre, bloqueado=False)
        return resultados


# ----- parsers -----


def resultados_bing(html: str) -> list[Resultado]:
    """Los resultados orgánicos de una página de Bing (`li.b_algo`)."""
    sopa = BeautifulSoup(html, "html.parser")
    resultados = []
    for li in sopa.select("li.b_algo"):
        enlace = li.select_one("h2 a[href]")
        if enlace is None:
            continue
        url = _url_bing(enlace["href"])
        titulo = enlace.get_text(" ", strip=True)
        parrafo = li.select_one(".b_caption p, p")
        extracto = parrafo.get_text(" ", strip=True) if parrafo else _texto_sin(li, titulo)
        resultados.append(Resultado(titulo, url, _corto(extracto)))
    return _limpios(resultados) or _por_titulos(sopa)


def resultados_duckduckgo(html: str) -> list[Resultado]:
    """Los resultados de la página HTML de DuckDuckGo, sin anuncios."""
    sopa = BeautifulSoup(html, "html.parser")
    resultados = []
    for bloque in sopa.select("div.result"):
        if "result--ad" in (bloque.get("class") or []):
            continue
        enlace = bloque.select_one("a.result__a[href]")
        if enlace is None:
            continue
        snippet = bloque.select_one(".result__snippet")
        resultados.append(
            Resultado(
                enlace.get_text(" ", strip=True),
                _url_duckduckgo(enlace["href"]),
                _corto(snippet.get_text(" ", strip=True) if snippet else ""),
            )
        )
    return _limpios(resultados) or _por_titulos(sopa)


def resultados_google(html: str) -> list[Resultado]:
    """Los resultados de una página de Google: enlaces externos con un `h3` adentro."""
    return _por_titulos(BeautifulSoup(html, "html.parser"), "h3")


def resultados_brave(html: str) -> list[Resultado]:
    """Los resultados web de una página de Brave Search."""
    sopa = BeautifulSoup(html, "html.parser")
    resultados = []
    for bloque in sopa.select('div.snippet[data-type="web"]'):
        enlace = bloque.select_one("a[href^='http']")
        if enlace is None:
            continue
        titulo_el = bloque.select_one(".title, [class*='title']")
        titulo = (titulo_el or enlace).get_text(" ", strip=True)
        descripcion = bloque.select_one(".snippet-description, [class*='description']")
        extracto = (
            descripcion.get_text(" ", strip=True) if descripcion else _texto_sin(bloque, titulo)
        )
        resultados.append(Resultado(titulo, enlace["href"], _corto(extracto)))
    return _limpios(resultados) or _por_titulos(sopa, "h3, h2, .title, [class*='title']")


# ----- internos -----

CARPETA_HTML = Path("data/agente/html")
"""Dónde se guarda una página de resultados que no dio ningún resultado, para revisarla."""

_MAX_HTML_GUARDADOS = 30
"""Tope de páginas guardadas por motor: es diagnóstico, no archivo."""


def _o_guardar(motor: str, html: str, resultados: list[Resultado]) -> list[Resultado]:
    """Devuelve `resultados`; si no hay ninguno, guarda el HTML para ver por qué.

    Una página sin resultados es o una búsqueda sin coincidencias o un motor
    que cambió su HTML (o un desafío que `detectar_bloqueo` no reconoce). Sin
    la página no se distinguen.
    """
    if resultados:
        return resultados
    try:
        CARPETA_HTML.mkdir(parents=True, exist_ok=True)
        if len(list(CARPETA_HTML.glob(f"{motor}-*.html"))) < _MAX_HTML_GUARDADOS:
            destino = CARPETA_HTML / f"{motor}-{datetime.now():%Y%m%d-%H%M%S-%f}.html"
            destino.write_text(html, encoding="utf-8")
            logger.info(f"         💾 {motor} sin resultados: página guardada en {destino}")
    except OSError:
        pass  # el diagnostico no tumba la busqueda
    return resultados


def _pedir(
    url: str,
    pausador: Pausador | None,
    timeout: int,
    *,
    params: dict | None = None,
    datos: dict | None = None,
) -> str:
    """GET (o POST con `datos`) como navegador; lanza `Bloqueado` si la respuesta es un bloqueo."""
    if pausador is not None:
        pausador.esperar(url)
    cabeceras = {"User-Agent": UA_NAVEGADOR, "Accept-Language": "es-MX,es;q=0.9"}
    if datos is not None:
        r = requests.post(url, data=datos, headers=cabeceras, timeout=timeout)
    else:
        r = requests.get(url, params=params, headers=cabeceras, timeout=timeout)
    sitio = urlparse(url).netloc
    if r.status_code in (403, 429):
        raise Bloqueado(sitio, f"HTTP {r.status_code}")
    r.raise_for_status()
    texto = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
    motivo = detectar_bloqueo(texto, r.url)
    if motivo:
        raise Bloqueado(sitio, motivo)
    return r.text


def _por_titulos(sopa: BeautifulSoup, selector: str = "h3, h2") -> list[Resultado]:
    """Respaldo genérico: cada enlace externo que contiene un título es un resultado.

    El bloque del resultado es el ancestro más grande del enlace que todavía
    contiene UN solo título: de ahí sale el extracto. No depende de clases
    CSS, que es lo que cada motor cambia.
    """
    resultados = []
    for titulo_el in sopa.select(selector):
        enlace = titulo_el.find_parent("a", href=True)
        if enlace is None:
            enlace = titulo_el.find("a", href=True)
        if enlace is None:
            continue
        url = enlace["href"]
        titulo = titulo_el.get_text(" ", strip=True)
        if not url.startswith("http") or not titulo:
            continue
        bloque: Tag = enlace
        for _ in range(8):
            padre = bloque.parent
            if not isinstance(padre, Tag) or len(padre.select(selector)) > 1:
                break
            bloque = padre
        resultados.append(Resultado(titulo, url, _corto(_texto_sin(bloque, titulo))))
    return _limpios(resultados)


def _limpios(resultados: list[Resultado]) -> list[Resultado]:
    """Sin enlaces del propio motor y sin URLs repetidas, en orden."""
    vistas: set[str] = set()
    limpios = []
    for r in resultados:
        dominio = urlparse(r.url).netloc.lower()
        if not r.url.startswith("http") or any(p in dominio for p in _DOMINIOS_PROPIOS):
            continue
        if r.url in vistas:
            continue
        vistas.add(r.url)
        limpios.append(r)
    return limpios


def _url_bing(href: str) -> str:
    """La URL real detrás de un enlace de seguimiento de Bing (`/ck/a?...&u=a1<base64>`)."""
    if "bing.com/ck/a" not in href:
        return href
    u = parse_qs(urlparse(href).query).get("u", [""])[0]
    if not u.startswith("a1"):
        return href
    codificado = u[2:]
    try:
        return base64.urlsafe_b64decode(codificado + "=" * (-len(codificado) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return href


def _url_duckduckgo(href: str) -> str:
    """La URL real detrás de un enlace de DuckDuckGo (`//duckduckgo.com/l/?uddg=...`)."""
    if "duckduckgo.com/l/" not in href:
        return href
    destino = parse_qs(urlparse(href if href.startswith("http") else "https:" + href).query)
    return destino.get("uddg", [href])[0]


def _texto_sin(bloque: Any, titulo: str) -> str:
    """El texto de un bloque sin su título."""
    texto = re.sub(r"\s+", " ", bloque.get_text(" ", strip=True))
    return texto.replace(titulo, "", 1).strip(" ·-—|")


def _corto(texto: str) -> str:
    """El extracto recortado a `_MAX_EXTRACTO`, en espacio."""
    texto = re.sub(r"\s+", " ", texto).strip()
    if len(texto) <= _MAX_EXTRACTO:
        return texto
    return texto[:_MAX_EXTRACTO].rsplit(" ", 1)[0] + " …"
