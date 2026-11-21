"""Las fuentes de v5 que no son buscadores ni Maps: datos abiertos y páginas halladas.

Misma forma que las de `recoleccion.py` (`Fuente`): una recolección base
igual para todos los negocios y, si la tiene, una herramienta de ampliación.
Las que traen un dato estructurado (OSM, archivo web, dominio, SAT) no amplían:
su consulta queda determinada por lo que publicó el DENUE, y lo que aporta el
modelo es leerla, no elegirla.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.agents.survival.insumo import Negocio
from src.agents.survival.recoleccion import Pieza, nombre_de
from src.agents.survival.sources import osm
from src.agents.survival.sources.cortesia import Pausador
from src.shared.enums import EstadoConsulta, Recolector
from src.shared.logging import get_stage_logger

logger = get_stage_logger(__name__, Path("./data/agente.log"))


class FuenteOsm:
    """OpenStreetMap alrededor de las coordenadas del DENUE."""

    recolector = Recolector.OSM.value
    herramienta = None

    def __init__(self, *, url: str, radio_m: int, timeout: int, pausador: Pausador | None = None):
        self.url = url
        self.radio_m = radio_m
        self.timeout = timeout
        self.pausador = pausador

    def recolectar(self, n: Negocio, expediente: list[Pieza]) -> list[Pieza]:
        """Una sola pieza: los objetos cercanos, ordenados por parecido del nombre."""
        consulta = f"objetos de OSM a ≤{self.radio_m} m del punto del DENUE"
        if n.lat is None or n.lon is None:
            return [_pieza(self.recolector, consulta, "(el DENUE no publicó coordenadas)", False)]
        mapa = f"https://www.openstreetmap.org/#map=19/{n.lat:.5f}/{n.lon:.5f}"
        nombres = list(dict.fromkeys([nombre_de(n), *n.nombres])) or [""]
        try:
            objetos = osm.buscar_cerca(
                n.lat,
                n.lon,
                nombres,
                radio_m=self.radio_m,
                url=self.url,
                timeout=self.timeout,
                pausador=self.pausador,
            )
        except Exception as e:  # noqa: BLE001  (Overpass caido es una pieza ERROR, no una caida)
            return [
                Pieza(
                    recolector=self.recolector,
                    consulta=consulta,
                    url=mapa,
                    contenido=f"ERROR de Overpass: {e.__class__.__name__}: {e}",
                    estado=EstadoConsulta.ERROR.value,
                )
            ]
        if not objetos:
            return [
                Pieza(
                    recolector=self.recolector,
                    consulta=consulta,
                    url=mapa,
                    contenido=f"(ningún negocio mapeado a ≤{self.radio_m} m)",
                    estado=EstadoConsulta.SIN_RESULTADOS.value,
                )
            ]
        encabezado = (
            f"OpenStreetMap: negocios mapeados a ≤{self.radio_m} m de {n.lat:.5f}, {n.lon:.5f}, "
            "del nombre más parecido al menos parecido."
        )
        return [
            Pieza(
                recolector=self.recolector,
                consulta=consulta,
                url=mapa,
                contenido="\n".join([encabezado, *(o.como_texto() for o in objetos)]),
                estado=EstadoConsulta.OK.value,
            )
        ]

    def ampliar(self, n: Negocio, args: dict[str, Any], expediente: list[Pieza]) -> list[Pieza]:
        """OSM no se amplía: la consulta es el punto del DENUE."""
        return []

    def contexto(self, expediente: list[Pieza]) -> str:
        """OSM no necesita nada del resto del expediente."""
        return ""


def _pieza(recolector: str, consulta: str, contenido: str, citable: bool) -> Pieza:
    """Una pieza sin URL: algo que no se pudo consultar o que no devolvió nada."""
    estado = EstadoConsulta.OK if citable else EstadoConsulta.SIN_RESULTADOS
    return Pieza(recolector=recolector, consulta=consulta, contenido=contenido, estado=estado.value)
