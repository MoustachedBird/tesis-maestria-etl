"""El historial de una URL en los archivos de la web: Internet Archive y Common Crawl.

Contesta "¿cuándo fue la última vez que esta página existió?", con una fecha
que pone el archivo y no el negocio: la captura más reciente del sitio
propio o de la página de Facebook es el último rastro fechado de que
estaban en línea. Es gratis y sin llave.

- **Internet Archive (Wayback Machine)**: el índice CDX dice cuándo se
  capturó una URL y con qué código HTTP; la captura más reciente se puede
  leer (`id_` = el HTML tal como se archivó, sin la barra del archivo).
- **Common Crawl**: un rastreador abierto que publica un índice por crawl
  (cada uno o dos meses). Se consultan los más recientes.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date

import requests

from src.agents.survival.sources.cortesia import Pausador

_UA = "tesis-etl-agente/1.0 (verificacion academica de negocios)"
"""User-Agent para los archivos, que piden identificarse; sin datos personales."""

_CDX_WAYBACK = "https://web.archive.org/cdx/search/cdx"
"""El índice de capturas del Internet Archive."""

_COLECCIONES_CC = "https://index.commoncrawl.org/collinfo.json"
"""La lista de crawls de Common Crawl, del más reciente al más viejo."""


@dataclass(frozen=True)
class Captura:
    """Una captura archivada de una URL."""

    fecha: date
    estado_http: str
    marca: str
    """El timestamp del archivo (AAAAMMDDhhmmss): la llave para pedir la captura."""
    original: str


def capturas_wayback(
    url: str, *, timeout: int = 30, pausador: Pausador | None = None, ultimas: int = 5
) -> tuple[Captura | None, list[Captura]]:
    """La primera captura y las `ultimas` más recientes (una por mes) de `url`.

    Raises:
        requests.RequestException: si el índice no contesta.
    """
    base = {"url": url, "output": "json", "fl": "timestamp,statuscode,original"}
    primera = _cdx(_CDX_WAYBACK, {**base, "limit": "1"}, timeout, pausador)
    recientes = _cdx(
        _CDX_WAYBACK, {**base, "limit": f"-{ultimas}", "collapse": "timestamp:6"}, timeout, pausador
    )
    return (primera[0] if primera else None), recientes


def url_de_captura(c: Captura, cruda: bool = True) -> str:
    """La URL de una captura en el Wayback; `cruda` = el HTML archivado sin la barra del archivo."""
    return f"https://web.archive.org/web/{c.marca}{'id_' if cruda else ''}/{c.original}"


def crawls_recientes(n: int, *, timeout: int = 30) -> list[dict]:
    """Los `n` crawls más recientes de Common Crawl (`id`, `name`, `cdx-api`)."""
    r = requests.get(_COLECCIONES_CC, headers={"User-Agent": _UA}, timeout=timeout)
    r.raise_for_status()
    return r.json()[:n]


def capturas_common_crawl(
    url: str, crawl: dict, *, timeout: int = 60, pausador: Pausador | None = None
) -> list[Captura]:
    """Las capturas de `url` (o de páginas bajo ella) en un crawl de Common Crawl.

    El índice responde 404 cuando no hay capturas: eso es una lista vacía,
    no un error.
    """
    return _cdx(
        crawl["cdx-api"],
        {"url": url, "matchType": "prefix", "output": "json", "limit": "3"},
        timeout,
        pausador,
        intentos=2,
    )


# ----- internos -----


def _cdx(
    endpoint: str,
    params: dict,
    timeout: int,
    pausador: Pausador | None,
    intentos: int = 2,
) -> list[Captura]:
    """Consulta un índice CDX (Wayback o Common Crawl) y devuelve sus capturas."""
    for intento in range(1, intentos + 1):
        if pausador is not None:
            pausador.esperar(endpoint)
        r = requests.get(endpoint, params=params, headers={"User-Agent": _UA}, timeout=timeout)
        if r.status_code == 404:
            return []
        if r.status_code in (429, 502, 503, 504) and intento < intentos:
            time.sleep(10 * intento)
            continue
        r.raise_for_status()
        return _parsear_cdx(r.text)
    return []


def _parsear_cdx(texto: str) -> list[Captura]:
    """Las dos formas de respuesta: Wayback (una lista JSON con cabecera) y Common Crawl (JSON por línea)."""
    texto = texto.strip()
    if not texto:
        return []
    if texto.startswith("["):
        filas = json.loads(texto)
        if not filas:
            return []
        cabecera, datos = filas[0], filas[1:]
        registros = [dict(zip(cabecera, f, strict=False)) for f in datos]
    else:
        registros = [json.loads(linea) for linea in texto.splitlines() if linea.strip()]
    capturas = []
    for r in registros:
        marca = str(r.get("timestamp", ""))
        if len(marca) < 8 or not marca[:8].isdigit():
            continue
        capturas.append(
            Captura(
                fecha=date(int(marca[:4]), int(marca[4:6]), int(marca[6:8])),
                estado_http=str(r.get("statuscode") or r.get("status") or "?"),
                marca=marca,
                original=str(r.get("original") or r.get("url") or ""),
            )
        )
    return capturas
