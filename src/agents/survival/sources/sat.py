"""La lista de contribuyentes NO LOCALIZADOS del SAT (art. 69 del Código Fiscal).

El SAT publica, como datos abiertos, a los contribuyentes que no encontró en
su domicilio fiscal, con la fecha de publicación. Para un negocio con razón
social es una señal de cierre fechada y oficial.

De las listas del art. 69 solo esta es señal: "cancelados" son CRÉDITOS
fiscales cancelados (deudas), no negocios dados de baja, y las demás
(firmes, exigibles, condonados) hablan de adeudos.

**El cruce es por nombre exacto**, comparando solo letras y números (así
`TACOS PACO, S.A. DE C.V.` y `TACOS PACO SA DE CV` coinciden). El SAT
identifica por RFC y el DENUE no lo publica, así que un homónimo es posible;
la pieza lo dice.

La lista se descarga una vez y vale `AGENTE_CACHE_DIAS`; vive en
`data/agente/sat/`, que no se versiona.
"""

from __future__ import annotations

import csv
import io
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import requests

from src.shared.logging import get_stage_logger

logger = get_stage_logger(__name__, Path("./data/agente.log"))

_UA = "tesis-etl-agente/1.0 (verificacion academica de negocios)"
"""User-Agent para la descarga; sin datos personales."""


@dataclass(frozen=True)
class Coincidencia:
    """Una fila de la lista cuyo nombre coincide con la razón social buscada."""

    razon_social: str
    fila: str
    """La fila completa como `COLUMNA: valor; ...`, que es lo que se cita."""


class ListaSat:
    """La lista descargada, indexada por nombre normalizado; se carga al primer uso."""

    def __init__(self, url: str, carpeta: Path, *, dias_validez: int = 30, timeout: int = 120):
        """
        Args:
            url: el CSV del SAT (`SAT_NO_LOCALIZADOS_URL`).
            carpeta: dónde se guarda la copia local.
            dias_validez: edad máxima de la copia antes de volver a bajarla.
            timeout: segundos de la descarga.
        """
        self.url = url
        self.archivo = carpeta / "no_localizados.csv"
        self.dias_validez = dias_validez
        self.timeout = timeout
        self._indice: dict[str, list[Coincidencia]] | None = None
        self.descargada: date | None = None
        self.filas = 0

    def buscar(self, razon_social: str) -> list[Coincidencia]:
        """Las filas cuyo nombre es exactamente esta razón social (sin puntuación ni espacios).

        Raises:
            requests.RequestException, ValueError: si la lista no se pudo bajar o leer.
        """
        if self._indice is None:
            self._indice = self._cargar()
        return self._indice.get(llave(razon_social), [])

    def _cargar(self) -> dict[str, list[Coincidencia]]:
        vieja = (
            not self.archivo.exists()
            or time.time() - self.archivo.stat().st_mtime > self.dias_validez * 86400
        )
        if vieja:
            logger.info(f"         🏛  descargando la lista del SAT: {self.url}")
            self.archivo.parent.mkdir(parents=True, exist_ok=True)
            r = requests.get(self.url, headers={"User-Agent": _UA}, timeout=self.timeout)
            r.raise_for_status()
            self.archivo.write_bytes(r.content)
        self.descargada = date.fromtimestamp(self.archivo.stat().st_mtime)
        indice, self.filas = indexar(self.archivo.read_bytes())
        logger.info(f"         🏛  lista del SAT: {self.filas:,} registros ({self.descargada})")
        return indice


def indexar(contenido: bytes) -> tuple[dict[str, list[Coincidencia]], int]:
    """El CSV del SAT como índice nombre -> filas.

    Tolerante al formato: la codificación (UTF-8 o Latin-1), las líneas de
    título antes de la cabecera y el nombre exacto de las columnas cambian
    entre publicaciones. La cabecera es la primera fila con una celda "RFC";
    la columna del nombre, la primera que diga "RAZ" o "NOMBRE".

    Raises:
        ValueError: si no hay cabecera reconocible.
    """
    try:
        texto = contenido.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = contenido.decode("latin-1")
    filas = list(csv.reader(io.StringIO(texto)))
    inicio = next(
        (i for i, f in enumerate(filas) if any(c.strip().upper() == "RFC" for c in f)), None
    )
    if inicio is None:
        raise ValueError("la lista del SAT no trae una cabecera con columna RFC")
    cabecera = [c.strip() for c in filas[inicio]]
    col_nombre = next(
        (i for i, c in enumerate(cabecera) if "RAZ" in _plano(c) or "NOMBRE" in _plano(c)),
        None,
    )
    if col_nombre is None:
        raise ValueError(f"la lista del SAT no trae columna de nombre: {cabecera}")
    indice: dict[str, list[Coincidencia]] = {}
    n = 0
    for f in filas[inicio + 1 :]:
        if len(f) <= col_nombre or not f[col_nombre].strip():
            continue
        n += 1
        nombre = f[col_nombre].strip()
        fila = "; ".join(
            f"{c}: {v.strip()}" for c, v in zip(cabecera, f, strict=False) if v.strip()
        )
        indice.setdefault(llave(nombre), []).append(Coincidencia(nombre, fila))
    return indice, n


def llave(nombre: str) -> str:
    """Solo letras y números, en mayúsculas y sin acentos: la llave del cruce exacto."""
    return re.sub(r"[^A-Z0-9Ñ]", "", _plano(nombre))


def _plano(texto: str) -> str:
    """Mayúsculas sin acentos (la Ñ se conserva)."""
    t = texto.upper().replace("Ñ", "\0")
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    return t.replace("\0", "Ñ")
