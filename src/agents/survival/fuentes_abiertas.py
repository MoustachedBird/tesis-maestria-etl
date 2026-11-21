"""Las fuentes de v5 que no son buscadores ni Maps: datos abiertos y páginas halladas.

Misma forma que las de `recoleccion.py` (`Fuente`): una recolección base
igual para todos los negocios y, si la tiene, una herramienta de ampliación.
Las que traen un dato estructurado (OSM, archivo web, dominio, SAT) no amplían:
su consulta queda determinada por lo que publicó el DENUE, y lo que aporta el
modelo es leerla, no elegirla.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from src.agents.survival.insumo import Negocio
from src.agents.survival.recoleccion import Pieza, clave_url, es_red, nombre_de, urls_halladas
from src.agents.survival.sources import archivo, dominio, osm, sat
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


# ----- huella digital: archivo web y dominio -----


def urls_para_archivo(n: Negocio, expediente: list[Pieza], maximo: int) -> list[str]:
    """Las URLs cuyo historial se busca: los sitios del DENUE y luego las redes halladas."""
    sitios = [s if s.lower().startswith("http") else f"https://{s}" for s in n.sitios]
    urls: list[str] = []
    for u in sitios + urls_halladas(expediente, es_red):
        if clave_url(u) not in {clave_url(x) for x in urls}:
            urls.append(u)
    return urls[:maximo]


class FuenteWayback:
    """El Internet Archive: cuándo se capturó por última vez el sitio y las redes, y qué decían."""

    recolector = Recolector.WAYBACK.value
    herramienta = None

    def __init__(
        self,
        leer_pagina: Callable[[str], str],
        *,
        max_urls: int,
        timeout: int = 30,
        pausador: Pausador | None = None,
    ):
        self.leer_pagina = leer_pagina
        self.max_urls = max_urls
        self.timeout = timeout
        self.pausador = pausador

    def recolectar(self, n: Negocio, expediente: list[Pieza]) -> list[Pieza]:
        """Por URL: el índice de capturas y la lectura de la más reciente que respondió 200."""
        urls = urls_para_archivo(n, expediente, self.max_urls)
        if not urls:
            return [
                _pieza(self.recolector, "capturas", "(no hay sitio ni redes que buscar)", False)
            ]
        piezas: list[Pieza] = []
        for url in urls:
            piezas += self._historial(url)
        return piezas

    def _historial(self, url: str) -> list[Pieza]:
        llave = clave_url(url)
        consulta = f"capturas de {llave}"
        indice = f"https://web.archive.org/web/*/{llave}"
        logger.info(f"         🗄  Internet Archive: capturas de {llave}")
        try:
            primera, recientes = archivo.capturas_wayback(
                llave, timeout=self.timeout, pausador=self.pausador
            )
        except Exception as e:  # noqa: BLE001  (archivo caido es una pieza ERROR)
            return [_error(self.recolector, consulta, indice, "el Internet Archive", e)]
        if not recientes:
            return [
                Pieza(
                    recolector=self.recolector,
                    consulta=consulta,
                    url=indice,
                    contenido=f"(el Internet Archive no tiene capturas de {llave})",
                    estado=EstadoConsulta.SIN_RESULTADOS.value,
                )
            ]
        lineas = [f"Internet Archive, capturas de {llave}:"]
        if primera:
            lineas.append(
                f"primera captura: {primera.fecha.isoformat()} (HTTP {primera.estado_http})"
            )
        lineas.append(
            "capturas más recientes (una por mes): "
            + ", ".join(f"{c.fecha.isoformat()} (HTTP {c.estado_http})" for c in recientes)
        )
        piezas = [
            Pieza(
                recolector=self.recolector,
                consulta=consulta,
                url=indice,
                contenido="\n".join(lineas),
                estado=EstadoConsulta.OK.value,
            )
        ]
        buenas = [c for c in recientes if c.estado_http == "200"]
        if buenas:
            ultima = buenas[-1]
            texto = self.leer_pagina(archivo.url_de_captura(ultima))
            if not texto.startswith("ERROR"):
                texto = (
                    f"Captura del Internet Archive del {ultima.fecha.isoformat()} "
                    f"de {ultima.original}:\n{texto}"
                )
            piezas.append(
                Pieza(
                    recolector=self.recolector,
                    consulta=f"captura más reciente de {llave}",
                    url=archivo.url_de_captura(ultima, cruda=False),
                    contenido=texto,
                    estado=_estado(texto),
                )
            )
        return piezas

    def ampliar(self, n: Negocio, args: dict[str, Any], expediente: list[Pieza]) -> list[Pieza]:
        """El archivo no se amplía: las URLs son las del DENUE y las que hallaron los buscadores."""
        return []

    def contexto(self, expediente: list[Pieza]) -> str:
        """Nada más que decir."""
        return ""


class FuenteCommonCrawl:
    """Common Crawl: si el sitio propio apareció en los crawls más recientes.

    Solo el sitio: Facebook e Instagram no se dejan rastrear por Common
    Crawl, y consultarlos costaba ~70 s por URL para nada (sondeo del
    2026-11-21).
    """

    recolector = Recolector.COMMON_CRAWL.value
    herramienta = None

    def __init__(
        self, *, max_urls: int, crawls: int, timeout: int = 60, pausador: Pausador | None = None
    ):
        self.max_urls = max_urls
        self.n_crawls = crawls
        self.timeout = timeout
        self.pausador = pausador
        self._crawls: list[dict] | None = None

    def recolectar(self, n: Negocio, expediente: list[Pieza]) -> list[Pieza]:
        """Una pieza por sitio, con su presencia en cada crawl reciente."""
        urls = urls_para_archivo(n, [], self.max_urls)  # sin las redes halladas
        if not urls:
            return [_pieza(self.recolector, "capturas", "(el DENUE no publicó sitio)", False)]
        try:
            if self._crawls is None:
                self._crawls = archivo.crawls_recientes(self.n_crawls, timeout=self.timeout)
        except Exception as e:  # noqa: BLE001  (indice caido es una pieza ERROR)
            return [
                _error(
                    self.recolector, "crawls", "https://index.commoncrawl.org/", "Common Crawl", e
                )
            ]
        return [self._presencia(u) for u in urls]

    def _presencia(self, url: str) -> Pieza:
        llave = clave_url(url)
        logger.info(f"         🗄  Common Crawl: {llave} en {len(self._crawls or [])} crawls")
        lineas = [
            f"Common Crawl, capturas de {llave} (y páginas bajo ella) en los crawls más recientes:"
        ]
        con_capturas = errores = 0
        for crawl in self._crawls or []:
            nombre = f"{crawl.get('id', '?')} ({crawl.get('name', '')})"
            try:
                capturas = archivo.capturas_common_crawl(
                    llave, crawl, timeout=self.timeout, pausador=self.pausador
                )
            except Exception as e:  # noqa: BLE001  (un crawl caido es una linea, no la pieza)
                errores += 1
                lineas.append(f"- {nombre}: no contestó ({e.__class__.__name__})")
                continue
            if not capturas:
                lineas.append(f"- {nombre}: sin capturas")
                continue
            con_capturas += 1
            ultima = max(capturas, key=lambda c: c.marca)
            lineas.append(
                f"- {nombre}: capturada el {ultima.fecha.isoformat()} "
                f"(HTTP {ultima.estado_http}) {ultima.original}"
            )
        if con_capturas:
            estado = EstadoConsulta.OK
        elif errores == len(self._crawls or []):
            estado = EstadoConsulta.ERROR
        else:
            estado = EstadoConsulta.SIN_RESULTADOS
        return Pieza(
            recolector=self.recolector,
            consulta=f"capturas de {llave}",
            url=f"https://index.commoncrawl.org/?url={llave}",
            contenido="\n".join(lineas),
            estado=estado.value,
        )

    def ampliar(self, n: Negocio, args: dict[str, Any], expediente: list[Pieza]) -> list[Pieza]:
        """No se amplía."""
        return []

    def contexto(self, expediente: list[Pieza]) -> str:
        """Nada más que decir."""
        return ""


class FuenteDominio:
    """El registro y el DNS de los dominios propios del sitio y del correo del DENUE."""

    recolector = Recolector.DOMINIO.value
    herramienta = None

    def __init__(self, *, timeout: int = 20, hoy: Callable[[], date] = date.today):
        self.timeout = timeout
        self._hoy = hoy

    def recolectar(self, n: Negocio, expediente: list[Pieza]) -> list[Pieza]:
        """Una pieza por dominio propio."""
        dominios = dominio.dominios_del_negocio(n.sitios, n.correos)
        if not dominios:
            return [
                _pieza(
                    self.recolector,
                    "dominios propios",
                    "(el DENUE no publicó sitio ni correo con dominio propio)",
                    False,
                )
            ]
        return [self._consultar(d, origen) for d, origen in dominios]

    def _consultar(self, d: str, origen: str) -> Pieza:
        url = f"whois://whois.mx/{d}" if d.endswith(".mx") else f"https://rdap.org/domain/{d}"
        logger.info(f"         🏷  registro de {d} ({origen})")
        consulta = f"registro de {d}"
        try:
            registro = dominio.consultar_registro(d, timeout=self.timeout, hoy=self._hoy())
        except Exception as e:  # noqa: BLE001  (registro caido es una pieza ERROR)
            return _error(self.recolector, consulta, url, "el registro del dominio", e)
        lineas = [
            f"dominio {d} (del {origen} que publicó el DENUE), consultado en {registro.servicio}:",
            *registro.lineas,
            dominio.resuelve(d),
        ]
        estado = EstadoConsulta.OK if registro.registrado is not None else EstadoConsulta.ERROR
        return Pieza(
            recolector=self.recolector,
            consulta=consulta,
            url=url,
            contenido="\n".join(lineas),
            estado=estado.value,
        )

    def ampliar(self, n: Negocio, args: dict[str, Any], expediente: list[Pieza]) -> list[Pieza]:
        """No se amplía."""
        return []

    def contexto(self, expediente: list[Pieza]) -> str:
        """Nada más que decir."""
        return ""


# ----- registros oficiales -----


class FuenteSat:
    """La lista de no localizados del SAT, por razón social exacta."""

    recolector = Recolector.SAT.value
    herramienta = None

    def __init__(self, lista: sat.ListaSat, url: str):
        self.lista = lista
        self.url = url

    def recolectar(self, n: Negocio, expediente: list[Pieza]) -> list[Pieza]:
        """Una pieza: las coincidencias exactas, o que no hubo."""
        razones = list(dict.fromkeys(r for r in n.razones_sociales if r.strip()))[:3]
        consulta = "no localizados del SAT: " + " | ".join(razones) if razones else "no localizados"
        if not razones:
            return [_pieza(self.recolector, consulta, "(el DENUE no publicó razón social)", False)]
        try:
            hallazgos = [(r, self.lista.buscar(r)) for r in razones]
        except Exception as e:  # noqa: BLE001  (lista inaccesible es una pieza ERROR)
            return [_error(self.recolector, consulta, self.url, "la lista del SAT", e)]
        encabezado = (
            "Lista de contribuyentes NO LOCALIZADOS del SAT (art. 69 CFF), "
            f"{self.lista.filas:,} registros, descargada el {self.lista.descargada}."
        )
        coinciden = [(r, c) for r, cs in hallazgos for c in cs]
        if not coinciden:
            return [
                Pieza(
                    recolector=self.recolector,
                    consulta=consulta,
                    url=self.url,
                    contenido=f"{encabezado}\nNinguna razón social del DENUE aparece en la lista.",
                    estado=EstadoConsulta.SIN_RESULTADOS.value,
                )
            ]
        lineas = [
            encabezado,
            "Coincidencias EXACTAS del nombre (el SAT identifica por RFC, que el DENUE no "
            "publica: un homónimo es posible):",
            *(f"- buscada «{r}»: {c.fila}" for r, c in coinciden),
        ]
        return [
            Pieza(
                recolector=self.recolector,
                consulta=consulta,
                url=self.url,
                contenido="\n".join(lineas),
                estado=EstadoConsulta.OK.value,
            )
        ]

    def ampliar(self, n: Negocio, args: dict[str, Any], expediente: list[Pieza]) -> list[Pieza]:
        """No se amplía."""
        return []

    def contexto(self, expediente: list[Pieza]) -> str:
        """Nada más que decir."""
        return ""


def _error(recolector: str, consulta: str, url: str, servicio: str, e: Exception) -> Pieza:
    """Un servicio que no contestó, como pieza no citable."""
    return Pieza(
        recolector=recolector,
        consulta=consulta,
        url=url,
        contenido=f"ERROR: {servicio} no contestó ({e.__class__.__name__}: {e})",
        estado=EstadoConsulta.ERROR.value,
    )


def _estado(texto: str) -> str:
    """Una lectura que devolvió `ERROR: ...` no se puede citar."""
    return EstadoConsulta.ERROR.value if texto.startswith("ERROR") else EstadoConsulta.OK.value
