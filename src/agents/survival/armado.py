"""De la configuración a las fuentes: qué se consulta, con qué y en qué orden.

Un solo lugar arma las fuentes para `verificar` y para `sondear`, así que lo
que el sondeo prueba es exactamente lo que el equipo usará. La lista sale de
`AGENTE_FUENTES` (nombres de `Recolector`); `agrupar` pone después el orden
entre especialistas.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.agents.survival.fuentes_abiertas import (
    FuenteCommonCrawl,
    FuenteDominio,
    FuenteOsm,
    FuenteSat,
    FuenteWayback,
)
from src.agents.survival.recoleccion import (
    Fuente,
    FuenteMaps,
    FuentePlataformas,
    FuenteRedes,
    FuenteSitio,
    FuenteWeb,
)
from src.agents.survival.sources.buscadores import (
    BuscadorBing,
    BuscadorBravePagina,
    BuscadorDuckDuckGo,
    BuscadorGoogle,
    BuscadorVigilado,
)
from src.agents.survival.sources.busqueda import BuscadorBrave, BuscadorSearxng
from src.agents.survival.sources.cortesia import Cortacircuitos, Pausador
from src.agents.survival.sources.navegador import Navegador, leer_con_respaldo
from src.agents.survival.sources.paginas import leer_pagina
from src.agents.survival.sources.sat import ListaSat
from src.shared.enums import Recolector

DATOS_AGENTE = "data/agente"
"""Carpeta de lo que el agente guarda en disco (perfil de Chrome, listas, sondeos); no se versiona."""


def nombres_de_fuentes(valor: str) -> list[str]:
    """`AGENTE_FUENTES` como lista, sin repetidos y validada contra `Recolector`.

    Raises:
        ValueError: si algún nombre no es un recolector.
    """
    nombres: list[str] = []
    for crudo in valor.split(","):
        nombre = crudo.strip().lower()
        if nombre and nombre not in nombres:
            nombres.append(nombre)
    validos = {r.value for r in Recolector}
    malos = [x for x in nombres if x not in validos]
    if malos:
        raise ValueError(f"AGENTE_FUENTES: {malos} no son recolectores; válidos: {sorted(validos)}")
    return nombres


class Armado:
    """Todo lo que las fuentes comparten en una corrida: navegador, pausas, cortacircuitos."""

    def __init__(self, ajustes: Any, navegador: Navegador, pausador: Pausador):
        self.ajustes = ajustes
        self.navegador = navegador
        self.pausador = pausador
        self.cortacircuitos = Cortacircuitos(ajustes.AGENTE_MAX_BLOQUEOS)

    def fuentes(self, nombres: list[str]) -> list[Fuente]:
        """Las fuentes de `nombres`, en ese orden.

        Raises:
            ValueError: si alguna todavía no tiene implementación.
        """
        fabricas = self._fabricas()
        faltan = [x for x in nombres if x not in fabricas]
        if faltan:
            raise ValueError(
                f"AGENTE_FUENTES: {faltan} todavía no tienen implementación; "
                f"disponibles: {sorted(fabricas)}"
            )
        return [fabricas[x]() for x in nombres]

    # ----- las fábricas -----

    def _fabricas(self) -> dict[str, Callable[[], Fuente]]:
        a = self.ajustes
        caracteres = a.AGENTE_MAX_CARACTERES_PAGINA
        minimo = a.AGENTE_MIN_CARACTERES_PAGINA
        timeout = a.AGENTE_TIMEOUT_HTTP_SEGUNDOS
        return {
            Recolector.MAPS.value: lambda: FuenteMaps(self.navegador, caracteres, minimo),
            Recolector.WEB.value: lambda: self._web(self.buscador_web(), Recolector.WEB.value),
            Recolector.BING.value: lambda: self._web(
                BuscadorBing(timeout, self.pausador), Recolector.BING.value
            ),
            Recolector.DUCKDUCKGO.value: lambda: self._web(
                BuscadorDuckDuckGo(timeout, self.pausador), Recolector.DUCKDUCKGO.value
            ),
            Recolector.GOOGLE.value: lambda: self._web(
                BuscadorGoogle(self.navegador), Recolector.GOOGLE.value
            ),
            Recolector.BRAVE.value: lambda: self._web(
                BuscadorBravePagina(self.navegador), Recolector.BRAVE.value
            ),
            Recolector.REDES.value: lambda: FuenteRedes(
                self.navegador, a.AGENTE_MAX_REDES, caracteres, minimo
            ),
            Recolector.OSM.value: lambda: FuenteOsm(
                url=a.OVERPASS_URL,
                radio_m=a.AGENTE_RADIO_METROS,
                timeout=max(timeout, 45),
                pausador=self.pausador,
            ),
            Recolector.PLATAFORMAS.value: lambda: FuentePlataformas(
                self.navegador, a.AGENTE_MAX_PLATAFORMAS, caracteres, minimo
            ),
            Recolector.SITIO.value: lambda: FuenteSitio(
                lambda url: leer_con_respaldo(url, self.navegador, caracteres, timeout, minimo),
                a.AGENTE_MAX_SITIOS,
            ),
            Recolector.WAYBACK.value: lambda: FuenteWayback(
                lambda url: leer_pagina(url, caracteres, max(timeout, 30), minimo),
                max_urls=a.AGENTE_MAX_ARCHIVO,
                timeout=max(timeout, 30),
                pausador=self.pausador,
            ),
            Recolector.COMMON_CRAWL.value: lambda: FuenteCommonCrawl(
                max_urls=a.AGENTE_MAX_ARCHIVO,
                crawls=a.AGENTE_CRAWLS_COMMON_CRAWL,
                timeout=max(timeout, 60),
                pausador=self.pausador,
            ),
            Recolector.DOMINIO.value: lambda: FuenteDominio(timeout=timeout),
            Recolector.SAT.value: lambda: FuenteSat(
                ListaSat(
                    a.SAT_NO_LOCALIZADOS_URL,
                    Path(DATOS_AGENTE) / "sat",
                    dias_validez=a.AGENTE_CACHE_DIAS,
                ),
                a.SAT_NO_LOCALIZADOS_URL,
            ),
        }

    def buscador_web(self) -> Any:
        """El buscador de `web`: la API de Brave si hay llave, si no el SearXNG del compose."""
        a = self.ajustes
        if a.BRAVE_API_KEY:
            return BuscadorBrave(a.BRAVE_API_KEY, timeout=a.AGENTE_TIMEOUT_HTTP_SEGUNDOS)
        return BuscadorSearxng(a.SEARXNG_URL, timeout=a.AGENTE_TIMEOUT_HTTP_SEGUNDOS)

    def _web(self, buscador: Any, recolector: str) -> FuenteWeb:
        return FuenteWeb(
            BuscadorVigilado(buscador, self.cortacircuitos),
            self.ajustes.AGENTE_MAX_RESULTADOS,
            recolector,
        )
