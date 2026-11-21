"""Las fuentes del equipo: qué se consulta de cada una, siempre igual, y cómo se amplía.

Cada fuente tiene dos partes:

- **Recolección base, en código.** Las mismas consultas para todos los
  negocios y en el mismo orden (`AGENTE_FUENTES`, agrupadas por `agrupar`). Es lo
  que vuelve comparables los veredictos: si cada agente eligiera qué mirar,
  un negocio se verificaría con Maps y otro no.
- **Ampliación, a pedido del especialista.** El agente especialista de la
  fuente puede pedir unas pocas consultas extra DENTRO de su fuente (otro
  nombre con que apareció, el domicilio). Esas piezas se marcan `ampliada`,
  así que el análisis puede separar lo que dio la base de lo que agregó el
  agente.

Se guarda TODO lo que cada consulta devolvió, incluidas las que fallaron.
Cada pieza lleva el texto EXACTO que verá el modelo (`contenido`), que es
contra lo que se comprueba que un extracto citado o una fecha estén ahí.

Cada fuente es un `Recolector` (la fuente concreta) y la lee el
especialista de su `Especialidad` (el grupo): `agrupar` las junta en el
orden fijo de los especialistas. Agregar una fuente es una clase más con la
forma de `Fuente`, su valor en `Recolector` y su fila en `ESPECIALIDAD_DE`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from src.agents.survival.insumo import Negocio
from src.agents.survival.sources.busqueda import Buscador, Resultado, clasificar_fuente
from src.shared.enums import (
    BUSCADORES,
    ESPECIALIDAD_DE,
    Especialidad,
    EstadoConsulta,
    Recolector,
)
from src.shared.logging import get_stage_logger

logger = get_stage_logger(__name__, Path("./data/agente.log"))


@dataclass(frozen=True)
class Pieza:
    """Una pieza del expediente: lo que devolvió UNA consulta a una fuente."""

    recolector: str
    contenido: str
    estado: str
    consulta: str | None = None
    url: str | None = None
    titulo: str | None = None
    fecha: date | None = None
    """La fecha que reportó el buscador para el resultado; None si no la dio."""
    ampliada: bool = False
    """True si la pidió el especialista; False si es de la recolección base."""

    def __post_init__(self) -> None:
        """Quita los caracteres que PostgreSQL no admite en un TEXT (el NUL, `\x00`).

        Hay páginas que los traen (sondeo del 2026-11-21: una tumbó el
        guardado de la muestra). Se limpian aquí, al nacer la pieza, y no al
        guardar, para que el texto que ve el modelo, el que valida las citas
        y el que queda en `consultas` sean el mismo.
        """
        for campo in ("contenido", "consulta", "url", "titulo"):
            valor = getattr(self, campo)
            if isinstance(valor, str) and "\x00" in valor:
                object.__setattr__(self, campo, sin_nul(valor))

    @property
    def fuente(self) -> str:
        """El tipo de fuente por dominio; `ninguna` si la pieza no tiene URL."""
        return clasificar_fuente(self.url) if self.url else "ninguna"

    @property
    def citable(self) -> bool:
        """Si se puede citar: solo las consultas que devolvieron algo."""
        return self.estado == EstadoConsulta.OK.value

    def como_texto(self, n: int) -> str:
        """La pieza como la ve el modelo, con su número `[n]`.

        Es también el texto contra el que se valida una cita: lo que no está
        aquí, el modelo no lo vio.
        """
        cabecera = f"[{n}] {self.recolector} · {self.fuente}"
        if not self.citable:
            cabecera += f" · {self.estado.upper()} (no se puede citar)"
        lineas = [cabecera]
        if self.consulta:
            lineas.append(f"consulta: {self.consulta}")
        if self.url:
            lineas.append(f"url: {self.url}")
        if self.titulo:
            lineas.append(f"título: {self.titulo}")
        if self.fecha:
            lineas.append(f"fecha de publicación (buscador): {self.fecha.isoformat()}")
        lineas.append(self.contenido)
        return "\n".join(lineas)


def sin_nul(texto: str) -> str:
    """El texto sin caracteres NUL, que PostgreSQL rechaza en columnas de texto."""
    return texto.replace("\x00", "")


class Navegable(Protocol):
    """Lo que las fuentes necesitan del navegador (`sources.navegador.Navegador`)."""

    ultima_url: str | None

    def leer(self, url: str, max_caracteres: int = ..., min_caracteres: int = ...) -> str: ...

    def resenas_recientes(self, max_caracteres: int = ..., min_caracteres: int = ...) -> str: ...

    def buscar_en_maps(
        self, consulta: str, max_caracteres: int = ..., min_caracteres: int = ...
    ) -> tuple[str, str]: ...


class Fuente(Protocol):
    """Una fuente del equipo: su recolección base y, si la tiene, su herramienta de ampliación."""

    recolector: str
    herramienta: dict | None
    """Definición de la herramienta que su especialista puede usar; None si no amplía."""

    def recolectar(self, n: Negocio, expediente: list[Pieza]) -> list[Pieza]:
        """La recolección base. `expediente` es lo que ya trajeron las fuentes anteriores."""
        ...

    def ampliar(self, n: Negocio, args: dict[str, Any], expediente: list[Pieza]) -> list[Pieza]:
        """Una consulta extra que pidió el especialista; las piezas salen con `ampliada=True`."""
        ...

    def contexto(self, expediente: list[Pieza]) -> str:
        """Algo más que el especialista necesita saber del expediente ("" si nada)."""
        ...


@dataclass
class Grupo:
    """Las fuentes que lee un mismo especialista, en el orden en que se consultan."""

    especialidad: str
    fuentes: list[Fuente]

    @property
    def recolectores(self) -> set[str]:
        """Los recolectores del grupo: las piezas que el especialista puede citar."""
        return {f.recolector for f in self.fuentes}

    def herramientas(self, expediente: list[Pieza] | None = None) -> list[dict]:
        """Las herramientas de ampliación de sus fuentes que tienen algo que ampliar.

        Una fuente de páginas halladas sin ninguna pendiente no ofrece su
        herramienta: no habría qué abrir, y ofrecerla hacía llamar al modelo
        para un grupo vacío (plataformas, sondeo del 2026-11-21).
        """
        return [
            f.herramienta
            for f in self.fuentes
            if f.herramienta
            and (expediente is None or getattr(f, "tiene_pendientes", lambda e: True)(expediente))
        ]

    def fuente_de(self, herramienta: str) -> Fuente | None:
        """La fuente a la que pertenece una herramienta, por su nombre."""
        for f in self.fuentes:
            if f.herramienta and f.herramienta["function"]["name"] == herramienta:
                return f
        return None

    def recolectar(self, n: Negocio, expediente: list[Pieza]) -> list[Pieza]:
        """La recolección base de cada fuente, en orden; cada una ve lo que trajeron las anteriores."""
        nuevas: list[Pieza] = []
        for f in self.fuentes:
            nuevas += recolectar_con_bitacora(f, n, expediente + nuevas)
        return nuevas

    def contexto(self, expediente: list[Pieza]) -> str:
        """Lo que sus fuentes quieren que el especialista sepa del resto del expediente."""
        return "\n\n".join(c for c in (f.contexto(expediente) for f in self.fuentes) if c)


def recolectar_con_bitacora(fuente: Fuente, n: Negocio, expediente: list[Pieza]) -> list[Pieza]:
    """La recolección base de una fuente, con una línea de log al empezar y otra al terminar.

    Una fuente con Chrome y pausas puede tardar minutos; sin esto el log se
    ve detenido.
    """
    logger.info(f"      ▶ {fuente.recolector}: consultando...")
    inicio = time.monotonic()
    piezas = fuente.recolectar(n, expediente)
    conteo = {e.value: sum(p.estado == e.value for p in piezas) for e in EstadoConsulta}
    error = next((p.contenido for p in piezas if p.estado == EstadoConsulta.ERROR.value), "")
    logger.info(
        f"      ✔ {fuente.recolector}: {len(piezas)} piezas "
        f"(ok {conteo['ok']}, sin resultados {conteo['sin_resultados']}, error {conteo['error']}) "
        f"en {time.monotonic() - inicio:.0f}s" + (f" · {error}" if error else "")
    )
    return piezas


def agrupar(fuentes: list[Fuente]) -> list[Grupo]:
    """Las fuentes por especialista, en el orden de `Especialidad`.

    Ese orden es el de la recolección: los buscadores van antes que redes,
    plataformas y archivo, que abren lo que ellos encontraron. Dentro de un
    grupo se respeta el orden en que llegaron las fuentes.
    """
    grupos = []
    for especialidad in Especialidad:
        propias = [f for f in fuentes if ESPECIALIDAD_DE[f.recolector] == especialidad.value]
        if propias:
            grupos.append(Grupo(especialidad.value, propias))
    return grupos


# ----- las fuentes -----


class FuenteMaps:
    """Google Maps: la ficha del lugar (estado, horario, reseñas), con navegador."""

    recolector = Recolector.MAPS.value
    herramienta = {
        "type": "function",
        "function": {
            "name": "buscar_en_maps",
            "description": "Otra búsqueda en Google Maps (otro nombre con que apareció, o el domicilio).",
            "parameters": {
                "type": "object",
                "properties": {"consulta": {"type": "string"}},
                "required": ["consulta"],
            },
        },
    }

    def __init__(self, navegador: Navegable, max_caracteres: int, min_caracteres: int):
        self.navegador = navegador
        self.max_caracteres = max_caracteres
        self.min_caracteres = min_caracteres

    def recolectar(self, n: Negocio, expediente: list[Pieza]) -> list[Pieza]:
        """Por nombre + lugar y, si el DENUE publicó domicilio, por nombre + domicilio.

        Cada búsqueda que cae en una ficha (no en una lista) trae además sus
        reseñas más recientes: la ficha muestra las "más relevantes", y lo que
        fecha la actividad es la última.
        """
        consultas = [f"{nombre_de(n)} {lugar_de(n)}"]
        if domicilio_de(n):
            consultas.append(f"{nombre_de(n)} {con_lugar(n, domicilio_de(n))}")
        consultas += [f"{otro} {lugar_de(n)}" for otro in nombres_de_busqueda(n)[1:]]
        consultas += [
            f"{nombre_de(n)} {con_lugar(n, otro)}" for otro in domicilios_de_busqueda(n)[1:]
        ]
        piezas: list[Pieza] = []
        for c in consultas:
            piezas += self._buscar(c, expediente + piezas)
        return piezas

    def ampliar(self, n: Negocio, args: dict[str, Any], expediente: list[Pieza]) -> list[Pieza]:
        """Una búsqueda más con la consulta que propuso el especialista."""
        consulta = str(args.get("consulta", "")).strip()
        if not consulta:
            return []
        return [replace(p, ampliada=True) for p in self._buscar(consulta, expediente)]

    def contexto(self, expediente: list[Pieza]) -> str:
        """Maps no necesita nada del resto del expediente."""
        return ""

    def _buscar(self, consulta: str, previas: list[Pieza]) -> list[Pieza]:
        url, texto = self.navegador.buscar_en_maps(
            consulta, self.max_caracteres, self.min_caracteres
        )
        piezas = [
            Pieza(
                recolector=self.recolector,
                consulta=consulta,
                url=url,
                contenido=texto,
                estado=_estado_de(texto),
            )
        ]
        ficha = getattr(self.navegador, "ultima_url", None) or ""
        ya_leidas = {_clave_ficha(p.url) for p in previas if p.consulta == _RESENAS}
        if piezas[0].citable and _VISTA_LIMITADA in piezas[0].contenido.lower():
            # Google le da a un navegador sin sesion una version recortada de
            # Maps, sin pestana de resenas: buscarla solo gasta tiempo.
            logger.info(
                "         🌐 Maps en vista limitada: sin reseñas para un visitante sin sesión"
            )
            return piezas
        if piezas[0].citable and "/maps/place/" in ficha and _clave_ficha(ficha) not in ya_leidas:
            resenas = self.navegador.resenas_recientes(self.max_caracteres, self.min_caracteres)
            piezas.append(
                Pieza(
                    recolector=self.recolector,
                    consulta=_RESENAS,
                    url=ficha,
                    contenido=resenas,
                    estado=_estado_de(resenas),
                )
            )
        return piezas


class FuenteWeb:
    """Un buscador web: extractos de resultados, incluidos plataformas, directorios y noticias.

    La misma clase sirve para todos los buscadores (SearXNG, Bing, DuckDuckGo,
    Google, Brave): cambian el `Buscador` y el recolector. Un resultado que
    otro buscador ya trajo no se repite, así que los siguientes solo suman
    URLs nuevas.
    """

    def __init__(
        self, buscador: Buscador, max_resultados: int, recolector: str = Recolector.WEB.value
    ):
        self.buscador = buscador
        self.max_resultados = max_resultados
        self.recolector = recolector
        nombre = "buscar_web" if recolector == Recolector.WEB.value else f"buscar_en_{recolector}"
        self.herramienta = {
            "type": "function",
            "function": {
                "name": nombre,
                "description": (
                    f"Otra búsqueda en {buscador.nombre} (título, URL, extracto y fecha si la da)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"consulta": {"type": "string"}},
                    "required": ["consulta"],
                },
            },
        }

    def recolectar(self, n: Negocio, expediente: list[Pieza]) -> list[Pieza]:
        """Nombre + lugar, nombre + domicilio, teléfono, y dos dirigidas: redes y reseñas."""
        consultas = [f'"{nombre_de(n)}" {lugar_de(n)}']
        if domicilio_de(n):
            consultas.append(f"{nombre_de(n)} {con_lugar(n, domicilio_de(n))}")
        if telefono_de(n):
            consultas.append(telefono_de(n))
        consultas.append(f"{nombre_de(n)} {n.municipio or ''} facebook".strip())
        consultas.append(f"{nombre_de(n)} {n.municipio or ''} opiniones".strip())
        consultas += [f'"{otro}" {lugar_de(n)}' for otro in nombres_de_busqueda(n)[1:]]
        consultas += [
            f"{nombre_de(n)} {con_lugar(n, otro)}" for otro in domicilios_de_busqueda(n)[1:]
        ]
        piezas: list[Pieza] = []
        for consulta in consultas:
            piezas += self._buscar(consulta, expediente + piezas)
        return piezas

    def ampliar(self, n: Negocio, args: dict[str, Any], expediente: list[Pieza]) -> list[Pieza]:
        """Una búsqueda más; lo que ya estaba en el expediente no se repite."""
        consulta = str(args.get("consulta", "")).strip()
        if not consulta:
            return []
        return [replace(p, ampliada=True) for p in self._buscar(consulta, expediente)]

    def contexto(self, expediente: list[Pieza]) -> str:
        """El buscador no necesita nada del resto del expediente."""
        return ""

    def _buscar(self, consulta: str, previas: list[Pieza]) -> list[Pieza]:
        """Los resultados como piezas, sin los que ya están en `previas`."""
        inicio = time.monotonic()
        try:
            resultados = self.buscador.buscar(consulta, n=self.max_resultados)
            sin_respuesta = getattr(self.buscador, "sin_respuesta", None)
            if sin_respuesta:
                logger.info(f"         {self.buscador.nombre}: no contestaron {sin_respuesta}")
        except Exception as e:  # noqa: BLE001  (un buscador caido es una pieza ERROR, no una caida)
            logger.info(
                f"         🔎 {self.recolector} «{consulta}»: ERROR {e.__class__.__name__}: "
                f"{str(e)[:120]}"
            )
            return [
                Pieza(
                    recolector=self.recolector,
                    consulta=consulta,
                    contenido=f"ERROR del buscador {self.buscador.nombre}: {e.__class__.__name__}: {e}",
                    estado=EstadoConsulta.ERROR.value,
                )
            ]
        if not resultados:
            logger.info(
                f"         🔎 {self.recolector} «{consulta}»: 0 resultados · "
                f"{time.monotonic() - inicio:.0f}s"
            )
            return [
                Pieza(
                    recolector=self.recolector,
                    consulta=consulta,
                    contenido="(sin resultados)",
                    estado=EstadoConsulta.SIN_RESULTADOS.value,
                )
            ]
        vistas = {clave_url(p.url) for p in previas if p.url}
        piezas = []
        for r in resultados:
            if clave_url(r.url) in vistas:
                continue
            vistas.add(clave_url(r.url))
            piezas.append(_pieza_de_resultado(self.recolector, consulta, r))
        logger.info(
            f"         🔎 {self.recolector} «{consulta}»: {len(resultados)} resultados, "
            f"{len(piezas)} nuevos · {time.monotonic() - inicio:.0f}s"
        )
        return piezas


class FuenteHallada:
    """Páginas que trajeron los buscadores, de cierto tipo, abiertas en Chrome sin sesión.

    La base para redes y plataformas: abre las primeras `maximo` que
    encontraron los buscadores y el especialista puede pedir otra, pero
    solo de la lista: una URL que proponga el modelo no se abre.
    """

    recolector: str
    tipo: str
    """Cómo se llaman estas páginas para el modelo ("de redes", "de plataformas")."""
    nombre_herramienta: str

    def __init__(self, navegador: Navegable, maximo: int, max_caracteres: int, min_caracteres: int):
        self.navegador = navegador
        self.maximo = maximo
        self.max_caracteres = max_caracteres
        self.min_caracteres = min_caracteres
        self.herramienta = {
            "type": "function",
            "function": {
                "name": self.nombre_herramienta,
                "description": f"Abre otra página {self.tipo} de la lista que se te dio.",
                "parameters": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            },
        }

    def es(self, url: str) -> bool:
        """Si la URL es de las que abre esta fuente."""
        raise NotImplementedError

    def prioridad(self, url: str) -> int:
        """Orden en que se abren (menor primero); por default, el de los buscadores."""
        return 0

    def halladas(self, expediente: list[Pieza]) -> list[str]:
        """Las de este tipo que trajeron los buscadores, por prioridad y luego en su orden."""
        urls = urls_halladas(expediente, self.es)
        return sorted(urls, key=lambda u: (self.prioridad(u), urls.index(u)))

    def recolectar(self, n: Negocio, expediente: list[Pieza]) -> list[Pieza]:
        """Las primeras `maximo` páginas de este tipo que aparecieron en los buscadores."""
        return [self._leer(url) for url in self.halladas(expediente)[: self.maximo]]

    def ampliar(self, n: Negocio, args: dict[str, Any], expediente: list[Pieza]) -> list[Pieza]:
        """Abre otra de las que aparecieron. Una URL que no salió de un buscador no se abre."""
        url = str(args.get("url", "")).strip()
        if clave_url(url) not in {clave_url(u) for u in self.halladas(expediente)}:
            return [
                Pieza(
                    recolector=self.recolector,
                    url=url or None,
                    contenido=f"ERROR: esa URL no está en la lista de páginas {self.tipo} del expediente.",
                    estado=EstadoConsulta.ERROR.value,
                    ampliada=True,
                )
            ]
        return [replace(self._leer(url), ampliada=True)]

    def contexto(self, expediente: list[Pieza]) -> str:
        """Las páginas de este tipo que trajeron los buscadores y todavía no se abrieron."""
        abiertas = {
            clave_url(p.url) for p in expediente if p.recolector == self.recolector and p.url
        }
        pendientes = [u for u in self.halladas(expediente) if clave_url(u) not in abiertas]
        if not pendientes:
            return f"No quedan otras páginas {self.tipo} en el expediente."
        return f"Páginas {self.tipo} que salieron en los buscadores y no se han abierto:\n" + (
            "\n".join(f"- {u}" for u in pendientes)
        )

    def tiene_pendientes(self, expediente: list[Pieza]) -> bool:
        """Si queda alguna página de este tipo sin abrir (si vale la pena ofrecer la herramienta)."""
        abiertas = {
            clave_url(p.url) for p in expediente if p.recolector == self.recolector and p.url
        }
        return any(clave_url(u) not in abiertas for u in self.halladas(expediente))

    def _leer(self, url: str) -> Pieza:
        texto = self.navegador.leer(url, self.max_caracteres, self.min_caracteres)
        return Pieza(recolector=self.recolector, url=url, contenido=texto, estado=_estado_de(texto))


def prioridad_red(url: str) -> int:
    """0 la página del negocio, 1 una publicación suelta, 2 un grupo.

    En Facebook `/p/<nombre>` es una página; en Instagram `/p/<código>` es
    una publicación.
    """
    u = urlparse(url.lower())
    if "/groups/" in u.path:
        return 2
    publicacion = ("/posts/", "/photos/", "/videos/", "/reel", "/stories/", "/permalink")
    if any(f in u.path for f in publicacion):
        return 1
    if "instagram.com" in u.netloc and u.path.startswith("/p/"):
        return 1
    return 0


class FuenteRedes(FuenteHallada):
    """Facebook e Instagram: las páginas que trajeron los buscadores."""

    recolector = Recolector.REDES.value
    tipo = "de redes"
    nombre_herramienta = "leer_red"

    def es(self, url: str) -> bool:
        """Facebook o Instagram."""
        return es_red(url)

    def prioridad(self, url: str) -> int:
        """Primero la página del negocio, luego sus publicaciones, al final los grupos.

        Una publicación suelta o un grupo de compraventa dicen poco del
        negocio; la página trae el encabezado, el horario y la última
        publicación (sondeo del 2026-11-21: se abrían dos publicaciones y la
        página quedaba fuera).
        """
        return prioridad_red(url)


class FuentePlataformas(FuenteHallada):
    """Fichas de reseñas y de entrega (TripAdvisor, Yelp, Rappi...) que trajeron los buscadores."""

    recolector = Recolector.PLATAFORMAS.value
    tipo = "de plataformas"
    nombre_herramienta = "leer_plataforma"

    def es(self, url: str) -> bool:
        """Una ficha de alguna de `PLATAFORMAS`."""
        return es_plataforma(url)


class FuenteSitio:
    """Los sitios web que el propio DENUE publicó del negocio."""

    recolector = Recolector.SITIO.value
    herramienta = None

    def __init__(self, leer_pagina: Callable[[str], str], max_sitios: int):
        self.leer_pagina = leer_pagina
        self.max_sitios = max_sitios

    def recolectar(self, n: Negocio, expediente: list[Pieza]) -> list[Pieza]:
        """Lee cada sitio; uno que no responde también es dato."""
        piezas = []
        for sitio in n.sitios[: self.max_sitios]:
            url = sitio if sitio.lower().startswith("http") else f"https://{sitio}"
            texto = self.leer_pagina(url)
            piezas.append(
                Pieza(
                    recolector=self.recolector, url=url, contenido=texto, estado=_estado_de(texto)
                )
            )
        return piezas

    def ampliar(self, n: Negocio, args: dict[str, Any], expediente: list[Pieza]) -> list[Pieza]:
        """El sitio no se amplía: son las URLs que publicó el DENUE y nada más."""
        return []

    def contexto(self, expediente: list[Pieza]) -> str:
        """El sitio no necesita nada del resto del expediente."""
        return ""


CARACTERES_POR_TOKEN = 3.0
"""Caracteres de texto en español por token, a la baja (Qwen anda en ~3.5).

Sirve para estimar sin tokenizar si un expediente cabe en el contexto. Es
una propiedad del tokenizador, no algo que se calibre por corrida.
"""

FRACCION_DEL_CONTEXTO = 0.6
"""Parte del contexto que puede ocupar el expediente; el resto es para el
prompt, el insumo, las vueltas de herramientas y la respuesta."""

_MIN_CARACTERES_POR_PIEZA = 300
"""Por debajo de esto una pieza recortada ya no dice nada: es el piso del recorte."""

_RECORTE = "\n[... recortado para caber en el contexto del modelo]"
"""La marca de una pieza recortada; el modelo sabe que no vio el resto."""


def presupuesto_de(num_ctx: int) -> int:
    """Caracteres de expediente que caben en un contexto de `num_ctx` tokens."""
    return int(num_ctx * CARACTERES_POR_TOKEN * FRACCION_DEL_CONTEXTO)


def formatear_expediente(
    piezas: list[Pieza], numeros: list[int] | None = None, presupuesto: int | None = None
) -> str:
    """Piezas para el modelo, con su número del expediente, separadas.

    Si no caben en `presupuesto` caracteres se recortan las más largas, todas
    al mismo tope, en vez de dejar que el servidor corte el mensaje en
    silencio (Ollama descarta el principio de lo que no cabe en `num_ctx`).
    El recorte es parejo y no depende de qué se citó: el juez estima a
    ciegas, y recortar distinto lo citado le diría qué citó el equipo.

    Args:
        piezas: lo que se muestra.
        numeros: el `[n]` de cada pieza en el expediente completo; por
            default 1..len. Un especialista ve solo sus piezas, pero con el
            número global, para que todo el equipo cite con la misma llave.
        presupuesto: caracteres máximos (`presupuesto_de`); None = sin tope.
    """
    if not piezas:
        return "(ninguna pieza)"
    numeros = numeros or list(range(1, len(piezas) + 1))
    textos = [p.como_texto(i) for i, p in zip(numeros, piezas, strict=True)]
    separador = "\n\n---\n\n"
    if presupuesto is not None:
        textos = _recortar(textos, presupuesto - len(separador) * len(textos))
    return separador.join(textos)


def _recortar(textos: list[str], presupuesto: int) -> list[str]:
    """Los textos con un mismo tope de largo, el mayor que deja la suma dentro del presupuesto."""
    if sum(len(t) for t in textos) <= presupuesto:
        return textos
    bajo, alto = _MIN_CARACTERES_POR_PIEZA, max(len(t) for t in textos)
    while bajo < alto:
        tope = (bajo + alto + 1) // 2
        if sum(min(len(t), tope) for t in textos) <= presupuesto:
            bajo = tope
        else:
            alto = tope - 1
    recortadas = sum(len(t) > bajo for t in textos)
    logger.info(
        f"         expediente recortado: {recortadas} de {len(textos)} piezas a {bajo} caracteres"
    )
    return [t if len(t) <= bajo else t[:bajo] + _RECORTE for t in textos]


# ----- utilidades -----


def nombre_de(n: Negocio) -> str:
    """El nombre principal de búsqueda: el primero del planificador, o el de gold, o el primero del DENUE."""
    if n.nombres_busqueda:
        return n.nombres_busqueda[0]
    return n.nombre or (n.nombres[0] if n.nombres else "")


def con_lugar(n: Negocio, domicilio: str | None) -> str:
    """El domicilio para buscarse: tal cual si lo escribió el planificador (él decide qué lugar
    lleva); si es el crudo del DENUE, con el lugar al final."""
    if not domicilio:
        return ""
    if n.domicilios_busqueda:
        return domicilio
    return f"{domicilio}, {lugar_de(n)}".strip(" ,")


def domicilios_de_busqueda(n: Negocio) -> list[str]:
    """Los domicilios con que se busca: los del planificador, o el más reciente del DENUE."""
    return list(n.domicilios_busqueda) or n.domicilios[:1]


def domicilio_de(n: Negocio) -> str | None:
    """El domicilio principal de búsqueda (el primero de `domicilios_de_busqueda`)."""
    domicilios = domicilios_de_busqueda(n)
    return domicilios[0] if domicilios else None


def telefono_de(n: Negocio) -> str | None:
    """El teléfono con que se busca: el del planificador, o el más reciente del DENUE."""
    return n.telefono_busqueda or (n.telefonos[0] if n.telefonos else None)


def nombres_de_busqueda(n: Negocio) -> list[str]:
    """Todos los nombres con que se busca: los del planificador, o solo el principal."""
    return list(n.nombres_busqueda) or [nombre_de(n)]


def lugar_de(n: Negocio) -> str:
    """Cómo se nombra el lugar en las búsquedas por nombre.

    Lo decide el planificador (`n.lugar_busqueda`): qué de la localidad, el
    municipio y el estado ayuda a encontrar al negocio no tiene una regla.
    Si no decidió (no hubo modelo o no dio una respuesta válida), el respaldo
    es la localidad, el municipio y el estado sin repetir.
    """
    if n.lugar_busqueda:
        return n.lugar_busqueda
    partes: list[str] = []
    for x in (n.localidad, n.municipio, n.entidad):
        if x and _llave_lugar(x) not in {_llave_lugar(p) for p in partes}:
            partes.append(x)
    return ", ".join(partes)


def _llave_lugar(texto: str) -> str:
    """Un nombre de lugar comparable: sin acentos, mayúsculas ni puntuación ("Xalapa" = "XALAPA")."""
    import unicodedata

    plano = unicodedata.normalize("NFKD", texto.upper()).encode("ascii", "ignore").decode()
    return " ".join("".join(c if c.isalnum() else " " for c in plano).split())


def clave_url(url: str | None) -> str:
    """URL comparable: sin esquema, sin `www.`, sin barra final, en minúsculas."""
    u = (url or "").strip().lower()
    for prefijo in ("https://", "http://"):
        u = u.removeprefix(prefijo)
    return u.removeprefix("www.").rstrip("/")


def urls_halladas(expediente: list[Pieza], es: Callable[[str], bool]) -> list[str]:
    """URLs que trajeron los buscadores y cumplen `es(url)`, sin repetir, en orden.

    Es como redes, plataformas y archivo saben qué abrir: solo lo que algún
    buscador encontró, nunca una URL que proponga el modelo.
    """
    urls: list[str] = []
    vistas: set[str] = set()
    for p in expediente:
        if p.recolector in BUSCADORES and p.url and es(p.url):
            clave = clave_url(p.url)
            if clave not in vistas:
                vistas.add(clave)
                urls.append(p.url)
    return urls


def es_red(url: str) -> bool:
    """Si la URL es de Facebook o Instagram."""
    return clasificar_fuente(url) in ("facebook", "instagram")


PLATAFORMAS: tuple[str, ...] = (
    "tripadvisor.",
    "yelp.",
    "foursquare.com",
    "seccionamarilla.",
    "restaurantguru.",
    "opentable.",
    "rappi.",
    "ubereats.com",
    "didi-food.com",
    "food.didiglobal",
)
"""Dominios de plataformas con estado o reseñas propias del negocio (no copias del DENUE).

Fragmentos del dominio: `tripadvisor.` cubre `.com.mx` y `.com`.
"""


_RUTAS_DE_FICHA: dict[str, tuple[str, ...]] = {
    "tripadvisor.": ("restaurant_review", "attraction_review", "hotel_review", "showuserreviews"),
    "yelp.": ("/biz/",),
    "foursquare.com": ("/v/",),
    "seccionamarilla.": ("/informacion/", "/negocio/"),
    "opentable.": ("/r/",),
    "rappi.": ("/restaurantes/", "/tiendas/"),
    "ubereats.com": ("/store/",),
    "didi-food.com": ("/store/",),
    "food.didiglobal": ("/store/",),
}
"""Por plataforma, fragmentos de la ruta que solo tiene la ficha de UN negocio.

Sin esto se abría cualquier página del dominio: la portada de Sección
Amarilla o "Atracciones en Anchorage, Alaska" de TripAdvisor (v6-piloto).
Una plataforma sin entrada (Restaurant Guru) acepta cualquier ruta que no
sea la portada.
"""


def es_plataforma(url: str) -> bool:
    """Si la URL es la FICHA de un negocio en una plataforma de `PLATAFORMAS` (no una portada ni una lista)."""
    u = urlparse(url.lower())
    plataforma = next((p for p in PLATAFORMAS if p in u.netloc), None)
    if plataforma is None or u.path.strip("/") == "":
        return False
    rutas = _RUTAS_DE_FICHA.get(plataforma)
    return rutas is None or any(r in u.path for r in rutas)


_VISTA_LIMITADA = "vista limitada de google maps"
"""Lo que Maps escribe cuando sirve su versión recortada (sin reseñas) a un visitante sin sesión."""

_RESENAS = "reseñas más recientes de la ficha"
"""La `consulta` de la pieza con las reseñas ordenadas por fecha de una ficha de Maps."""


def _clave_ficha(url: str | None) -> str:
    """Una ficha de Maps sin la parte de la URL que cambia entre visitas (`/@lat,lon`, `/data=`)."""
    return clave_url((url or "").split("/@")[0].split("/data=")[0])


def _estado_de(texto: str) -> str:
    """Una lectura que devolvió `ERROR: ...` no se puede citar."""
    return EstadoConsulta.ERROR.value if texto.startswith("ERROR") else EstadoConsulta.OK.value


def _pieza_de_resultado(recolector: str, consulta: str, r: Resultado) -> Pieza:
    """Un resultado del buscador como pieza: el extracto es lo que se puede citar."""
    return Pieza(
        recolector=recolector,
        consulta=consulta,
        url=r.url,
        titulo=r.titulo or None,
        fecha=r.fecha,
        contenido=r.extracto or "(el buscador no dio extracto)",
        estado=EstadoConsulta.OK.value,
    )
