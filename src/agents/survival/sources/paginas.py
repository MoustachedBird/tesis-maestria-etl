"""Leer una página como texto plano, acotado, para dárselo al modelo.

`requests` + BeautifulSoup, que ya están en las dependencias. Sin navegador:
lo que un negocio chico deja en línea --una ficha, una página de Facebook
pública, una nota local-- se lee sin JavaScript, y lo que no (Maps en
vivo) se cubre por el extracto del buscador y por Street View no se cubre.

Los errores se devuelven como TEXTO, no como excepción: el que lee es el
modelo, y "la página respondió 404" es información que le sirve para
decidir (un sitio caído es señal), mientras que una excepción tiraría el
veredicto entero por una URL.
"""

from __future__ import annotations

import re

import requests
from bs4 import BeautifulSoup

_UA = "Mozilla/5.0 (X11; Linux x86_64) tesis-etl-agente/1.0"
"""User-Agent de las peticiones: identifica al agente sin fingir un navegador."""

SIN_CONTENIDO = "no entregó contenido legible"
"""Frase del `ERROR` de una página vacía; `navegador.leer_con_respaldo` la busca para reintentar con Chrome."""

_RUIDO = ("script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe")
"""Etiquetas que nunca llevan el contenido que importa y sí llenan el contexto."""


def leer_pagina(
    url: str, max_caracteres: int = 6000, timeout: int = 20, min_caracteres: int = 200
) -> str:
    """El texto visible de una página, recortado.

    Una página que responde 200 con casi nada de texto es un muro de inicio
    de sesión (Facebook, Instagram) o un cascarón que se llena con
    JavaScript. Se devuelve como `ERROR`, igual que un 404: entregársela al
    modelo como lectura lo deja creer que leyó la fuente.

    Args:
        url: qué leer.
        max_caracteres: tope del texto devuelto; lo útil suele estar al
            principio y el contexto del modelo es finito.
        timeout: segundos por petición.
        min_caracteres: texto visible mínimo (sin contar el título) para que
            la página cuente como leída.

    Returns:
        str: el texto, o una línea `ERROR: ...` que el modelo puede leer.
    """
    try:
        respuesta = requests.get(
            url, headers={"User-Agent": _UA, "Accept-Language": "es-MX,es;q=0.9"}, timeout=timeout
        )
    except requests.RequestException as e:
        return f"ERROR: no se pudo abrir {url} ({e.__class__.__name__}: {e})"
    if respuesta.status_code >= 400:
        return f"ERROR: {url} respondio HTTP {respuesta.status_code}"
    tipo = respuesta.headers.get("content-type", "")
    if "html" not in tipo and "text" not in tipo:
        return f"ERROR: {url} no es texto ni HTML (content-type: {tipo})"

    sopa = BeautifulSoup(respuesta.text, "html.parser")
    for etiqueta in sopa(_RUIDO):
        etiqueta.decompose()
    titulo = sopa.title.get_text(strip=True) if sopa.title else ""
    texto = re.sub(r"\s+", " ", sopa.get_text(" ", strip=True))
    if len(texto) < min_caracteres:
        return (
            f"ERROR: {url} {SIN_CONTENIDO} ({len(texto)} caracteres; suele ser "
            "un muro de inicio de sesión o una página que necesita JavaScript). NO la leíste: "
            "de esta URL solo sabes lo que mostró el buscador."
        )
    cuerpo = f"TITULO: {titulo}\n{texto}" if titulo else texto
    if len(cuerpo) > max_caracteres:
        return cuerpo[:max_caracteres] + f"\n[... recortado a {max_caracteres} caracteres]"
    return cuerpo
