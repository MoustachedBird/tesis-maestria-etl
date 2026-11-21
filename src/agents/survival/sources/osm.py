"""OpenStreetMap por Overpass: qué hay mapeado alrededor del punto del DENUE.

Es la única fuente gratuita que busca por POSICIÓN y no por nombre, así que
separa homónimos sola: una taquería con el mismo nombre en otro municipio no
aparece. Y tiene un vocabulario explícito de cierre: un local que un mapeador
vio desocupado se etiqueta `disused:shop=...` o `was:amenity=...`.

El código no decide si un objeto es el negocio: calcula su distancia al punto
del DENUE y qué tanto se parece su nombre, y lo escribe en la pieza para que
el especialista decida con eso a la vista.
"""

from __future__ import annotations

import math
import time
import unicodedata
from dataclasses import dataclass

import requests
from rapidfuzz import fuzz

from src.agents.survival.sources.cortesia import Pausador

_UA = "tesis-etl-agente/1.0 (verificacion academica de negocios)"
"""User-Agent para Overpass, que pide identificarse; sin datos personales."""

_CICLO_DE_VIDA = ("disused", "was", "abandoned", "demolished", "razed", "removed", "vacant")
"""Prefijos de OSM para algo que existió y ya no opera en ese lugar."""

_CLAVES_DE_NEGOCIO = ("amenity", "shop", "craft", "office", "tourism", "leisure", "healthcare")
"""Claves que hacen de un objeto un negocio o servicio (y no una calle o un río)."""

_ETIQUETAS_UTILES = (
    "opening_hours",
    "check_date",
    "survey:date",
    "start_date",
    "end_date",
    "phone",
    "contact:phone",
    "website",
    "contact:website",
    "contact:facebook",
    "addr:street",
    "addr:housenumber",
)
"""Etiquetas que se le muestran al modelo además de nombre y tipo.

`check_date` y `survey:date` son la fecha en que un mapeador comprobó el
objeto en campo: de lo poco en OSM que fecha que el negocio existía.
"""

_RADIO_TIERRA_M = 6_371_000
"""Radio medio de la Tierra para la distancia haversine (constante física)."""


@dataclass(frozen=True)
class Objeto:
    """Un objeto de OSM cerca del punto, con lo que hace falta para identificarlo."""

    tipo: str
    id: int
    nombre: str
    clase: str
    distancia_m: float
    similitud: int
    editado: str | None
    ciclo_de_vida: tuple[str, ...]
    etiquetas: tuple[tuple[str, str], ...]

    @property
    def url(self) -> str:
        """La página del objeto en openstreetmap.org."""
        return f"https://www.openstreetmap.org/{self.tipo}/{self.id}"

    def como_texto(self) -> str:
        """Una línea para el modelo."""
        partes = [
            f"«{self.nombre or '(sin nombre)'}»",
            self.clase or "sin tipo",
            f"a {self.distancia_m:.0f} m",
            f"nombre {self.similitud}/100",
        ]
        if self.ciclo_de_vida:
            partes.append("CICLO DE VIDA: " + ", ".join(self.ciclo_de_vida))
        partes += [f"{k}={v}" for k, v in self.etiquetas]
        if self.editado:
            partes.append(f"última edición en OSM: {self.editado}")
        partes.append(self.url)
        return "- " + " · ".join(partes)


def consulta_overpass(lat: float, lon: float, radio_m: int) -> str:
    """La consulta Overpass QL: todo lo que tiene nombre o etiqueta de ciclo de vida en el radio."""
    ciclo = "|".join(_CICLO_DE_VIDA)
    return (
        f"[out:json][timeout:25];("
        f'nwr(around:{radio_m},{lat},{lon})["name"];'
        f'nwr(around:{radio_m},{lat},{lon})[~"^({ciclo}):"~"."];'
        f");out tags center meta;"
    )


def buscar_cerca(
    lat: float,
    lon: float,
    nombres: list[str],
    *,
    radio_m: int,
    url: str,
    timeout: int = 30,
    pausador: Pausador | None = None,
    intentos: int = 3,
    max_objetos: int = 8,
) -> list[Objeto]:
    """Los negocios mapeados alrededor del punto, del que más se parece al que menos.

    Args:
        lat, lon: el punto del DENUE.
        nombres: los nombres con que apareció el negocio, para la similitud.
        radio_m: radio de búsqueda.
        url: el servidor Overpass.
        timeout: segundos por petición.
        pausador: la pausa por dominio de la corrida.
        intentos: Overpass responde 429/504 cuando está saturado; se reintenta.
        max_objetos: cuántos se devuelven.

    Raises:
        requests.RequestException: si Overpass no contesta tras los intentos.
    """
    for intento in range(1, intentos + 1):
        if pausador is not None:
            pausador.esperar(url)
        r = requests.post(
            url,
            data={"data": consulta_overpass(lat, lon, radio_m)},
            headers={"User-Agent": _UA},
            timeout=timeout,
        )
        if r.status_code in (429, 504) and intento < intentos:
            time.sleep(10 * intento)
            continue
        r.raise_for_status()
        break
    objetos = [
        o for o in (_objeto(e, lat, lon, nombres) for e in r.json().get("elements", [])) if o
    ]
    objetos.sort(key=lambda o: (-o.similitud, o.distancia_m))
    return objetos[:max_objetos]


def _objeto(e: dict, lat: float, lon: float, nombres: list[str]) -> Objeto | None:
    """Un elemento de Overpass como `Objeto`, o None si no es un negocio."""
    tags: dict[str, str] = e.get("tags") or {}
    ciclo = tuple(
        f"{k}={v}" for k, v in tags.items() if k.split(":", 1)[0] in _CICLO_DE_VIDA and ":" in k
    )
    clase = next((f"{k}={tags[k]}" for k in _CLAVES_DE_NEGOCIO if k in tags), "")
    nombre = tags.get("name", "")
    similitud = max((_similitud(nombre, n) for n in nombres), default=0) if nombre else 0
    if not ciclo and not clase and similitud < 60:
        return None  # una calle, un rio, una colonia
    centro = e.get("center") or e
    if "lat" not in centro or "lon" not in centro:
        return None
    return Objeto(
        tipo=e.get("type", "node"),
        id=int(e.get("id", 0)),
        nombre=nombre,
        clase=clase,
        distancia_m=_haversine(lat, lon, float(centro["lat"]), float(centro["lon"])),
        similitud=similitud,
        editado=(e.get("timestamp") or "")[:10] or None,
        ciclo_de_vida=ciclo,
        etiquetas=tuple((k, tags[k]) for k in _ETIQUETAS_UTILES if k in tags),
    )


def _similitud(a: str, b: str) -> int:
    """Parecido de dos nombres, 0-100, sin acentos ni mayúsculas y sin importar el orden."""
    return int(fuzz.token_set_ratio(_plano(a), _plano(b)))


def _plano(texto: str) -> str:
    """Mayúsculas y sin acentos."""
    sin_acentos = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return sin_acentos.upper().strip()


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia en metros sobre la esfera."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _RADIO_TIERRA_M * math.asin(math.sqrt(a))
