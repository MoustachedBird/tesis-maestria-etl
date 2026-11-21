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
from datetime import date
from typing import Any, Protocol
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
    fecha: date | None = None
    """Fecha de publicación que reporta el buscador; la mayoría de los resultados no trae."""

    def como_texto(self) -> str:
        """El resultado para el modelo: título, URL, extracto y la fecha si el buscador la dio.

        Es también el texto contra el que `verificador.py` comprueba que una
        `fecha_publicacion` citada salió de algo visto, así que la fecha va
        escrita y no solo guardada.
        """
        lineas = [f"- {self.titulo}", f"  {self.url}", f"  {self.extracto}"]
        if self.fecha:
            lineas.append(f"  fecha de publicación: {self.fecha.isoformat()}")
        return "\n".join(lineas).strip()


def _fecha_iso(valor: Any) -> date | None:
    """La fecha de un resultado (`2026-11-05T00:00:00`, `2026-11-05`) o None si no parsea.

    Tolerante a propósito: el campo es opcional y cada motor lo llena a su
    manera; una fecha ilegible es un resultado sin fecha, no un error.
    """
    if not valor:
        return None
    try:
        return date.fromisoformat(str(valor)[:10])
    except ValueError:
        return None


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
        self.sin_respuesta: list[str] = []
        """Los motores internos de SearXNG que no contestaron la última consulta, con el motivo.

        Es lo único que dice si Google o DuckDuckGo están bloqueando a
        SearXNG en silencio: sin esto, "sin resultados" y "motor caído" se
        ven igual.
        """

    def buscar(self, consulta: str, n: int = 8) -> list[Resultado]:
        respuesta = requests.get(
            f"{self.url}/search",
            params={"q": consulta, "format": "json", "language": "es-MX", "safesearch": 0},
            headers={"User-Agent": _UA},
            timeout=self.timeout,
        )
        respuesta.raise_for_status()
        datos = respuesta.json()
        self.sin_respuesta = [
            " ".join(str(x) for x in motor if x) if isinstance(motor, list) else str(motor)
            for motor in datos.get("unresponsive_engines", [])
        ]
        return [
            Resultado(
                titulo=(r.get("title") or "").strip(),
                url=r.get("url", ""),
                extracto=(r.get("content") or "").strip(),
                fecha=_fecha_iso(r.get("publishedDate")),
            )
            for r in datos.get("results", [])[:n]
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
                fecha=_fecha_iso(r.get("page_age")),
            )
            for r in respuesta.json().get("web", {}).get("results", [])[:n]
            if r.get("url")
        ]


FUENTE_DIRECTORIO_DENUE = "directorio_denue"
"""Sitios que republican el propio DENUE: una ficha ahí no es evidencia independiente."""

_FUENTES_POR_DOMINIO: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Primero: algunos tambien caerian en `directorio`, y lo que importa de
    # ellos es que copian al INEGI. Que un negocio aparezca ahi dice que el
    # DENUE lo listo alguna vez, no que siga abierto: comparar el DENUE contra
    # si mismo. El decisor solo puede citarlos como senal `neutral`.
    (
        FUENTE_DIRECTORIO_DENUE,
        ("inegi.org.mx", "mexicopymes", "municipiodata", "pueblosamerica"),
    ),
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
