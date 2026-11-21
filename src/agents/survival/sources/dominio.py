"""El registro de un dominio (RDAP o WHOIS) y si todavía resuelve en DNS.

Un negocio que cerró deja de pagar su dominio: el registro vence, se libera
o lo compra otro. Las fechas las pone el registro, no el negocio, así que se
pueden citar. Aplica al dominio del sitio y al del correo que publicó el
DENUE (si no es un correo gratuito).

- `.mx` (incluido `.com.mx`) se consulta por WHOIS en `whois.mx`: es su
  servicio público de siempre, y no se depende de que un intermediario RDAP
  lo cubra.
- El resto, por RDAP (`rdap.org`, que redirige al registro de cada TLD). En
  los gTLD el RDAP es obligatorio, así que un 404 es "no registrado".

El código compara la fecha de vencimiento con hoy y lo escribe: es aritmética,
no algo que el modelo deba hacer.
"""

from __future__ import annotations

import re
import socket
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlparse

import requests

_RDAP = "https://rdap.org/domain/{dominio}"
"""Intermediario de RDAP: redirige al servidor del registro del TLD."""

_BOOTSTRAP_IANA = "https://data.iana.org/rdap/dns.json"
"""La tabla oficial de IANA de qué servidor RDAP atiende cada TLD (respaldo de `_RDAP`)."""

_UA = "tesis-etl-agente/1.0 (verificacion academica de negocios)"
"""User-Agent: sin él rdap.org responde 403 (sondeo del 2026-11-21)."""

_servidores_rdap: dict[str, str] | None = None
"""La tabla de IANA ya descargada, TLD -> URL base; se baja una vez por proceso."""

_WHOIS_MX = ("whois.mx", 43)
"""El WHOIS de NIC México."""

_CORREO_GRATUITO = (
    "gmail.",
    "hotmail.",
    "outlook.",
    "live.",
    "yahoo.",
    "icloud.",
    "me.com",
    "msn.",
    "aol.",
    "prodigy.net.mx",
    "terra.",
    "infinitummail.com",
    "protonmail.",
    "gmx.",
)
"""Proveedores de correo gratuito: su dominio no dice nada del negocio.

Con punto al final es el inicio de una etiqueta (`gmail.` cubre `gmail.com`
y `gmail.com.mx`); sin él, un dominio completo (`me.com`). Ver `_es_de`.
"""

_PLATAFORMAS = (
    "facebook.com",
    "fb.com",
    "instagram.com",
    "wixsite.com",
    "wix.com",
    "blogspot.",
    "wordpress.com",
    "business.site",
    "negocio.site",
    "google.com",
    "linktr.ee",
    "tiktok.com",
    "youtube.com",
    "twitter.com",
    "x.com",
    "wa.me",
    "whatsapp.com",
    "mercadolibre.",
    "ueniweb.com",
    "jimdosite.com",
    "godaddysites.com",
)
"""Sitios alojados en una plataforma: el dominio es de la plataforma, no del negocio.

Misma convención que `_CORREO_GRATUITO` (ver `_es_de`).
"""

_SEGUNDO_NIVEL = ("com", "org", "net", "gob", "edu", "nom")
"""Segundos niveles de un ccTLD (`.com.mx`): el dominio registrable lleva tres etiquetas."""

_CAMPOS_WHOIS = ("created on", "expiration date", "last updated on", "registrar", "status")
"""Líneas del WHOIS de `.mx` que se le muestran al modelo, tal cual."""


@dataclass
class Registro:
    """Lo que se sabe del registro de un dominio."""

    dominio: str
    servicio: str
    """`whois.mx` o la URL de RDAP consultada."""
    registrado: bool | None
    """None si el servicio no contestó algo interpretable."""
    lineas: list[str] = field(default_factory=list)
    """Los datos del registro como texto (fechas en AAAA-MM-DD)."""
    vence: date | None = None


def dominios_del_negocio(sitios: list[str], correos: list[str]) -> list[tuple[str, str]]:
    """Los dominios registrables del sitio y del correo, con de dónde salió cada uno."""
    vistos: list[tuple[str, str]] = []
    for origen, crudos in (("sitio", sitios), ("correo", correos)):
        for crudo in crudos:
            host = crudo.split("@", 1)[1] if "@" in crudo else _host(crudo)
            d = registrable(host)
            if not d or _es_de(host, _PLATAFORMAS + _CORREO_GRATUITO):
                continue
            if d not in {x for x, _ in vistos}:
                vistos.append((d, origen))
    return vistos


def registrable(host: str) -> str:
    """El dominio que se registra (`tienda.tacospaco.com.mx` -> `tacospaco.com.mx`)."""
    partes = [p for p in host.lower().strip(". ").removeprefix("www.").split(".") if p]
    if len(partes) < 2:
        return ""
    if len(partes) >= 3 and len(partes[-1]) == 2 and partes[-2] in _SEGUNDO_NIVEL:
        return ".".join(partes[-3:])
    return ".".join(partes[-2:])


def consultar_registro(dominio: str, *, timeout: int = 20, hoy: date | None = None) -> Registro:
    """El registro de `dominio` por WHOIS (`.mx`) o RDAP (el resto).

    Raises:
        OSError, requests.RequestException: si el servicio no contesta.
    """
    hoy = hoy or date.today()
    registro = _whois_mx(dominio, timeout) if dominio.endswith(".mx") else _rdap(dominio, timeout)
    if registro.vence:
        estado = "VENCIDO" if registro.vence < hoy else "vigente"
        registro.lineas.append(
            f"el registro vence el {registro.vence.isoformat()}; hoy es {hoy.isoformat()}: {estado}"
        )
    return registro


def resuelve(dominio: str) -> str:
    """Si el dominio resuelve en DNS, en una línea para el modelo."""
    try:
        ips = sorted({i[4][0] for i in socket.getaddrinfo(dominio, 443)})
    except socket.gaierror as e:
        return f"DNS: {dominio} NO resuelve ({e.strerror or e})"
    return f"DNS: {dominio} resuelve ({', '.join(ips[:3])})"


# ----- internos -----


def _es_de(host: str, patrones: tuple[str, ...]) -> bool:
    """Si `host` es de alguno de los dominios de `patrones`, por etiquetas y no por subcadena.

    `x.com` no debe atrapar `netflix.com`, ni `live.` a `olive.com.mx`.
    """
    h = host.lower().removeprefix("www.")
    for p in patrones:
        if p.endswith("."):
            if h.startswith(p) or f".{p}" in h:
                return True
        elif h == p or h.endswith(f".{p}"):
            return True
    return False


def _host(url: str) -> str:
    u = url if "://" in url else f"https://{url}"
    return urlparse(u).netloc.split(":")[0]


def _whois_mx(dominio: str, timeout: int) -> Registro:
    """WHOIS de NIC México por el puerto 43."""
    with socket.create_connection(_WHOIS_MX, timeout=timeout) as s:
        s.sendall(f"{dominio}\r\n".encode())
        partes = []
        while True:
            bloque = s.recv(4096)
            if not bloque:
                break
            partes.append(bloque)
    texto = b"".join(partes).decode("utf-8", errors="replace")
    servicio = "whois.mx"
    if re.search(r"object_not_found|no_se_encontro|not found", texto, re.IGNORECASE):
        return Registro(dominio, servicio, False, [f"{dominio}: el WHOIS de .mx no lo encuentra"])
    lineas = []
    vence = None
    for linea in texto.splitlines():
        clave = linea.split(":", 1)[0].strip().lower()
        if clave in _CAMPOS_WHOIS and ":" in linea:
            lineas.append(re.sub(r"\s+", " ", linea.strip()))
            if clave == "expiration date":
                vence = _fecha(linea.split(":", 1)[1])
    return Registro(dominio, servicio, bool(lineas) or None, lineas, vence)


def _rdap(dominio: str, timeout: int) -> Registro:
    """RDAP por rdap.org; si no lo atiende, directo al servidor del TLD según IANA."""
    cabeceras = {"Accept": "application/rdap+json", "User-Agent": _UA}
    url = _RDAP.format(dominio=dominio)
    r = requests.get(url, timeout=timeout, headers=cabeceras)
    if r.status_code in (403, 429) or r.status_code >= 500:
        base = _servidor_rdap(dominio.rsplit(".", 1)[-1], timeout)
        if base:
            url = f"{base.rstrip('/')}/domain/{dominio}"
            r = requests.get(url, timeout=timeout, headers=cabeceras)
    if r.status_code == 404:
        return Registro(dominio, url, False, [f"{dominio}: el registro (RDAP) no lo encuentra"])
    r.raise_for_status()
    datos = r.json()
    lineas = []
    vence = None
    nombres = {
        "registration": "registrado",
        "expiration": "vence",
        "last changed": "última modificación",
    }
    for evento in datos.get("events", []):
        accion = evento.get("eventAction", "")
        fecha = _fecha(evento.get("eventDate", ""))
        if accion in nombres and fecha:
            lineas.append(f"{nombres[accion]}: {fecha.isoformat()}")
            if accion == "expiration":
                vence = fecha
    if datos.get("status"):
        lineas.append("estado: " + ", ".join(datos["status"]))
    return Registro(dominio, url, True, lineas, vence)


def _servidor_rdap(tld: str, timeout: int) -> str | None:
    """La URL base del servidor RDAP de un TLD, según la tabla de IANA."""
    global _servidores_rdap
    if _servidores_rdap is None:
        r = requests.get(_BOOTSTRAP_IANA, timeout=timeout, headers={"User-Agent": _UA})
        r.raise_for_status()
        _servidores_rdap = {
            t.lower(): urls[0] for tlds, urls in r.json().get("services", []) if urls for t in tlds
        }
    return _servidores_rdap.get(tld.lower())


def _fecha(texto: str) -> date | None:
    """La primera fecha AAAA-MM-DD de un texto."""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", texto or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
