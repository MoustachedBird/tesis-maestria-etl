"""Las fuentes del equipo: qué se consulta de cada una, siempre igual, y cómo se amplía.

Cada fuente tiene dos partes:

- **Recolección base, en código.** Las mismas consultas para todos los
  negocios y en el mismo orden (Maps, buscador, redes, sitio propio). Es lo
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

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Protocol

from src.agents.survival.insumo import Negocio
from src.agents.survival.sources.busqueda import Buscador, Resultado, clasificar_fuente
from src.shared.enums import (
    BUSCADORES,
    ESPECIALIDAD_DE,
    Especialidad,
    EstadoConsulta,
    Recolector,
)


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


class Navegable(Protocol):
    """Lo que las fuentes necesitan del navegador (`sources.navegador.Navegador`)."""

    def leer(self, url: str, max_caracteres: int = ..., min_caracteres: int = ...) -> str: ...

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

    @property
    def herramientas(self) -> list[dict]:
        """Las herramientas de ampliación de sus fuentes (las que tienen)."""
        return [f.herramienta for f in self.fuentes if f.herramienta]

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
            nuevas += f.recolectar(n, expediente + nuevas)
        return nuevas

    def contexto(self, expediente: list[Pieza]) -> str:
        """Lo que sus fuentes quieren que el especialista sepa del resto del expediente."""
        return "\n\n".join(c for c in (f.contexto(expediente) for f in self.fuentes) if c)


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
        """Por nombre + lugar y, si el DENUE publicó domicilio, por nombre + domicilio."""
        consultas = [f"{nombre_de(n)} {lugar_de(n)}"]
        if n.domicilios:
            consultas.append(f"{nombre_de(n)} {n.domicilios[0]} {n.municipio or ''}".strip())
        return [self._buscar(c) for c in consultas]

    def ampliar(self, n: Negocio, args: dict[str, Any], expediente: list[Pieza]) -> list[Pieza]:
        """Una búsqueda más con la consulta que propuso el especialista."""
        consulta = str(args.get("consulta", "")).strip()
        if not consulta:
            return []
        return [replace(self._buscar(consulta), ampliada=True)]

    def contexto(self, expediente: list[Pieza]) -> str:
        """Maps no necesita nada del resto del expediente."""
        return ""

    def _buscar(self, consulta: str) -> Pieza:
        url, texto = self.navegador.buscar_en_maps(
            consulta, self.max_caracteres, self.min_caracteres
        )
        return Pieza(
            recolector=self.recolector,
            consulta=consulta,
            url=url,
            contenido=texto,
            estado=_estado_de(texto),
        )


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
        if n.domicilios:
            consultas.append(f"{nombre_de(n)} {n.domicilios[0]} {n.municipio or ''}".strip())
        if n.telefonos:
            consultas.append(n.telefonos[0])
        consultas.append(f"{nombre_de(n)} {n.municipio or ''} facebook".strip())
        consultas.append(f"{nombre_de(n)} {n.municipio or ''} opiniones".strip())
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
        try:
            resultados = self.buscador.buscar(consulta, n=self.max_resultados)
        except Exception as e:  # noqa: BLE001  (un buscador caido es una pieza ERROR, no una caida)
            return [
                Pieza(
                    recolector=self.recolector,
                    consulta=consulta,
                    contenido=f"ERROR del buscador {self.buscador.nombre}: {e.__class__.__name__}: {e}",
                    estado=EstadoConsulta.ERROR.value,
                )
            ]
        if not resultados:
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
        return piezas


class FuenteRedes:
    """Facebook e Instagram: las páginas que trajo el buscador, abiertas con navegador y sin sesión."""

    recolector = Recolector.REDES.value
    herramienta = {
        "type": "function",
        "function": {
            "name": "leer_red",
            "description": "Abre otra página de Facebook o Instagram de la lista que se te dio.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    }

    def __init__(
        self, navegador: Navegable, max_redes: int, max_caracteres: int, min_caracteres: int
    ):
        self.navegador = navegador
        self.max_redes = max_redes
        self.max_caracteres = max_caracteres
        self.min_caracteres = min_caracteres

    def recolectar(self, n: Negocio, expediente: list[Pieza]) -> list[Pieza]:
        """Las primeras `max_redes` páginas de redes que aparecieron en el buscador."""
        return [self._leer(url) for url in _urls_de_redes(expediente)[: self.max_redes]]

    def ampliar(self, n: Negocio, args: dict[str, Any], expediente: list[Pieza]) -> list[Pieza]:
        """Abre otra de las que aparecieron. Una URL que no salió del buscador no se abre."""
        url = str(args.get("url", "")).strip()
        if clave_url(url) not in {clave_url(u) for u in _urls_de_redes(expediente)}:
            return [
                Pieza(
                    recolector=self.recolector,
                    url=url or None,
                    contenido="ERROR: esa URL no está en la lista de redes del expediente.",
                    estado=EstadoConsulta.ERROR.value,
                    ampliada=True,
                )
            ]
        return [replace(self._leer(url), ampliada=True)]

    def contexto(self, expediente: list[Pieza]) -> str:
        """Las páginas de redes que el buscador trajo y todavía no se abrieron."""
        abiertas = {
            clave_url(p.url) for p in expediente if p.recolector == self.recolector and p.url
        }
        pendientes = [u for u in _urls_de_redes(expediente) if clave_url(u) not in abiertas]
        if not pendientes:
            return "No quedan otras páginas de redes en el expediente."
        return "Páginas de redes que salieron en el buscador y no se han abierto:\n" + "\n".join(
            f"- {u}" for u in pendientes
        )

    def _leer(self, url: str) -> Pieza:
        texto = self.navegador.leer(url, self.max_caracteres, self.min_caracteres)
        return Pieza(recolector=self.recolector, url=url, contenido=texto, estado=_estado_de(texto))


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


def formatear_expediente(piezas: list[Pieza], numeros: list[int] | None = None) -> str:
    """Piezas para el modelo, con su número del expediente, separadas.

    Args:
        piezas: lo que se muestra.
        numeros: el `[n]` de cada pieza en el expediente completo; por
            default 1..len. Un especialista ve solo sus piezas, pero con el
            número global, para que todo el equipo cite con la misma llave.
    """
    if not piezas:
        return "(ninguna pieza)"
    numeros = numeros or list(range(1, len(piezas) + 1))
    return "\n\n---\n\n".join(p.como_texto(i) for i, p in zip(numeros, piezas, strict=True))


# ----- utilidades -----


def nombre_de(n: Negocio) -> str:
    """El nombre con que se busca: el de gold, o el primero que publicó el DENUE."""
    return n.nombre or (n.nombres[0] if n.nombres else "")


def lugar_de(n: Negocio) -> str:
    """`municipio, entidad`, con lo que haya."""
    return ", ".join(x for x in (n.municipio, n.entidad) if x)


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


def _urls_de_redes(expediente: list[Pieza]) -> list[str]:
    """URLs de Facebook/Instagram que trajeron los buscadores."""
    return urls_halladas(expediente, es_red)


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
