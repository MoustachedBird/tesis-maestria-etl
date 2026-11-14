"""Búsqueda web: SearXNG por default, Brave si hay llave.

Dos backends con la misma interfaz y el agente no sabe cuál usa. SearXNG es
el servicio de compose: gratis, sin llave, sin cuota, y agrega varios motores
(`crear_agentes.md` §11.3 descarta raspar DuckDuckGo a mano; SearXNG es la
forma mantenida de hacer eso mismo). Brave tiene índice propio y no cae en
la zona gris de raspar un motor ajeno: si se consigue llave, es la primera
opción del plan y basta ponerla en `.env`.

**Las URLs que devuelve un buscador son las únicas que el agente puede citar
como evidencia** (`verificador.py`): un modelo que inventa una URL es el
modo de fallo más barato de atrapar, y se atrapa aquí.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

import requests

_UA = "Mozilla/5.0 (X11; Linux x86_64) tesis-etl-agente/1.0"
"""User-Agent de las peticiones: identifica al agente sin fingir un navegador."""


@dataclass(frozen=True)
class Resultado:
    """Un resultado de búsqueda, lo mínimo que el modelo necesita para decidir si lo lee."""

    titulo: str
    url: str
    extracto: str

    def como_texto(self) -> str:
        """Una línea legible para el modelo."""
        return f"- {self.titulo}\n  {self.url}\n  {self.extracto}".strip()


class Buscador(Protocol):
    """Lo que el agente pide a un buscador: una consulta, una lista de resultados."""

    nombre: str

    def buscar(self, consulta: str, n: int = 8) -> list[Resultado]: ...


class BuscadorSearxng:
    """SearXNG por su API JSON (`search.formats` debe incluir `json` en `settings.yml`)."""

    nombre = "searxng"

    def __init__(self, url: str, timeout: int = 20):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def buscar(self, consulta: str, n: int = 8) -> list[Resultado]:
        respuesta = requests.get(
            f"{self.url}/search",
            params={"q": consulta, "format": "json", "language": "es-MX", "safesearch": 0},
            headers={"User-Agent": _UA},
            timeout=self.timeout,
        )
        respuesta.raise_for_status()
        return [
            Resultado(
                titulo=(r.get("title") or "").strip(),
                url=r.get("url", ""),
                extracto=(r.get("content") or "").strip(),
            )
            for r in respuesta.json().get("results", [])[:n]
            if r.get("url")
        ]


class BuscadorBrave:
    """Brave Search API (web). Índice propio; `country=MX`, resultados en español."""

    nombre = "brave"
    _ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str, timeout: int = 20):
        self.api_key = api_key
        self.timeout = timeout

    def buscar(self, consulta: str, n: int = 8) -> list[Resultado]:
        respuesta = requests.get(
            self._ENDPOINT,
            params={"q": consulta, "country": "MX", "search_lang": "es", "count": n},
            headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"},
            timeout=self.timeout,
        )
        respuesta.raise_for_status()
        return [
            Resultado(
                titulo=(r.get("title") or "").strip(),
                url=r.get("url", ""),
                extracto=(r.get("description") or "").strip(),
            )
            for r in respuesta.json().get("web", {}).get("results", [])[:n]
            if r.get("url")
        ]


_FUENTES_POR_DOMINIO: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("google_maps", ("google.com/maps", "maps.google", "goo.gl/maps", "maps.app.goo.gl")),
    ("facebook", ("facebook.com", "fb.com", "fb.me")),
    ("instagram", ("instagram.com",)),
    (
        "directorio",
        (
            "seccionamarilla",
            "cylex",
            "paginasamarillas",
            "infoisinfo",
            "yelp.",
            "foursquare",
            "tripadvisor",
            "nicelocal",
            "mexicored",
            "empresite",
        ),
    ),
    (
        "marketplace",
        ("mercadolibre", "ubereats", "rappi.", "didi-food", "didiglobal", "amazon.com.mx"),
    ),
    (
        "noticia",
        (
            "informador.mx",
            "milenio.com",
            "eluniversal",
            "excelsior",
            "jornada",
            "reforma.com",
            "proceso.com",
            "elfinanciero",
            "expansion.mx",
            "sdpnoticias",
            "ntrguadalajara",
            "mural.com",
        ),
    ),
    ("wayback", ("web.archive.org",)),
)
"""Cómo se clasifica una URL en `evidencias.fuente`, por fragmentos del dominio.

Se decide aquí y no lo decide el modelo: el valor es el eje de los `GROUP BY`
del análisis ("¿cuántos cierres dependen solo de Google Maps?"), y un
clasificador determinista es lo que lo vuelve comparable entre corridas y
entre modelos. Lo que no cae en ninguna es `sitio_web`: la página del propio
negocio o de un tercero cualquiera.
"""


def clasificar_fuente(url: str) -> str:
    """Tipo de fuente de una URL, para `evidencias.fuente`.

    Args:
        url: la dirección citada.

    Returns:
        str: una de las claves de `_FUENTES_POR_DOMINIO`, o `sitio_web`.
    """
    u = url.lower()
    dominio = urlparse(u).netloc
    for fuente, fragmentos in _FUENTES_POR_DOMINIO:
        if any(f in u if "/" in f else f in dominio for f in fragmentos):
            return fuente
    return "sitio_web"
