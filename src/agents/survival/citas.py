"""Lo que hace válida una cita: que la pieza exista, que el extracto y la fecha estén escritos ahí.

El modo de fallo más barato de un LLM es atribuirle a una fuente lo que no
dice: un extracto parafraseado como si fuera textual, o una fecha deducida
("si publicó una promo, debe ser reciente"). La fecha es justo lo que separa
`activo/alta` de `activo/baja`, así que es lo que al modelo más le conviene
suponer. Aquí se comprueba, en código, contra el texto exacto que el modelo
tuvo enfrente.

Lo usan los tres roles: el especialista al informar, el decisor al concluir
y el juez al objetar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from src.agents.survival.recoleccion import Pieza
from src.agents.survival.sources.busqueda import FUENTE_DIRECTORIO_DENUE
from src.shared.enums import SenalEvidencia

_MIN_EXTRACTO = 8
"""Caracteres mínimos de un fragmento citado: menos ("abierto") aparece en cualquier lado."""

_MESES = {
    1: "ene(?:ro)?|jan(?:uary)?",
    2: "feb(?:rero|ruary)?",
    3: "mar(?:zo|ch)?",
    4: "abr(?:il)?|apr(?:il)?",
    5: "may(?:o)?",
    6: "jun(?:io|e)?",
    7: "jul(?:io|y)?",
    8: "ago(?:sto)?|aug(?:ust)?",
    9: "sep(?:t|tiembre|tember)?|set(?:iembre)?",
    10: "oct(?:ubre|ober)?",
    11: "nov(?:iembre|ember)?",
    12: "dic(?:iembre)?|dec(?:ember)?",
}
"""El nombre de cada mes como regex: completo o abreviado, en español y en inglés.

Va el nombre entero y no un prefijo para que "novedades 2026" o "martes
2024" no pasen por noviembre o marzo.
"""


@dataclass(frozen=True)
class Cita:
    """Una pieza del expediente citada, ya validada."""

    pieza: int
    extracto: str
    senal: str
    fecha_publicacion: date | None = None

    def como_dict(self) -> dict:
        """Para guardar en JSONB y para mostrársela a otro rol."""
        return {
            "pieza": self.pieza,
            "extracto": self.extracto,
            "senal": self.senal,
            "fecha_publicacion": self.fecha_publicacion.isoformat()
            if self.fecha_publicacion
            else None,
        }

    def como_texto(self) -> str:
        """Una línea legible para el modelo."""
        fecha = f" · {self.fecha_publicacion.isoformat()}" if self.fecha_publicacion else ""
        return f'[{self.pieza}] {self.senal}{fecha}: "{self.extracto}"'


def validar_citas(
    crudas: Any, expediente: list[Pieza], permitidas: set[int] | None = None
) -> tuple[list[Cita], list[str]]:
    """Valida la lista de citas que mandó el modelo.

    Args:
        crudas: lo que vino en el argumento `citas` / `evidencias`.
        expediente: todas las piezas, en orden: la cita `[n]` es `expediente[n-1]`.
        permitidas: los `[n]` que este rol puede citar (un especialista, los
            de su fuente; el decisor, los que citó algún especialista). None
            = cualquiera.

    Returns:
        tuple[list[Cita], list[str]]: las citas y los errores; con errores
        la lista no vale.
    """
    if crudas in (None, ""):
        return [], []
    if not isinstance(crudas, list):
        return [], ["las citas tienen que ser una lista"]
    citas: list[Cita] = []
    errores: list[str] = []
    for i, c in enumerate(crudas, start=1):
        if not isinstance(c, dict):
            errores.append(f"cita {i}: no es un objeto")
            continue
        try:
            n = int(c.get("pieza"))
        except (TypeError, ValueError):
            errores.append(f"cita {i}: 'pieza' tiene que ser el número [n] de una pieza")
            continue
        if not 1 <= n <= len(expediente):
            errores.append(f"cita {i}: no hay pieza [{n}]")
            continue
        if permitidas is not None and n not in permitidas:
            errores.append(f"cita {i}: la pieza [{n}] no es de las que puedes citar")
            continue
        pieza = expediente[n - 1]
        texto = pieza.como_texto(n)
        if not pieza.citable:
            errores.append(
                f"cita {i}: la pieza [{n}] es {pieza.estado.upper()} y no se puede citar"
            )
            continue
        extracto = str(c.get("extracto", "")).strip()
        if not extracto_en(extracto, texto):
            parecido = fragmento_parecido(extracto, texto)
            pista = f' Lo más parecido que dice la pieza es: "{parecido}"' if parecido else ""
            errores.append(
                f"cita {i}: el extracto no aparece textual en la pieza [{n}]; cópialo tal cual "
                f"(puedes recortar con '...').{pista}"
            )
        senal = str(c.get("senal", "")).strip().lower()
        if senal not in {s.value for s in SenalEvidencia}:
            errores.append(
                f"cita {i}: senal {senal!r} no es una de {[s.value for s in SenalEvidencia]}"
            )
        elif pieza.fuente == FUENTE_DIRECTORIO_DENUE and senal != SenalEvidencia.NEUTRAL.value:
            errores.append(
                f"cita {i}: la pieza [{n}] es de un directorio que copia al DENUE; no prueba "
                "que siga abierto ni que cerró, así que su señal es neutral"
            )
        fecha, error_fecha = parsear_fecha(c.get("fecha_publicacion"))
        if error_fecha:
            errores.append(f"cita {i}: fecha_publicacion: {error_fecha}")
        elif fecha and not fecha_sustentada(fecha, texto):
            errores.append(
                f"cita {i}: la fecha {c.get('fecha_publicacion')!r} no está escrita en la pieza "
                f"[{n}]. Si la pieza no trae fecha, manda null; no la deduzcas"
            )
        citas.append(Cita(n, extracto, senal, fecha))
    return citas, errores


def extracto_en(extracto: str, texto: str) -> bool:
    """Si el extracto está escrito en el texto, ignorando mayúsculas, espacios y comillas.

    El modelo puede recortar con "..." o "…": cada fragmento tiene que estar,
    y al menos uno con `_MIN_EXTRACTO` caracteres.
    """
    fuente = _plano(texto)
    fragmentos = [_plano(f) for f in re.split(r"\.\.\.|…", extracto)]
    fragmentos = [f for f in fragmentos if f]
    if not fragmentos or max(len(f) for f in fragmentos) < _MIN_EXTRACTO:
        return False
    return all(f in fuente for f in fragmentos)


_MIN_PARECIDO = 60
"""Por debajo de esto el fragmento más parecido no es lo que el modelo quiso citar."""


def fragmento_parecido(extracto: str, texto: str) -> str | None:
    """El tramo del texto que más se parece al extracto, para que el modelo lo copie bien.

    Sin esto, un modelo que parafrasea una cita la repite igual en cada
    vuelta hasta agotar el presupuesto (v6-piloto: cuatro veces la misma).
    Se le devuelve el texto real; sigue teniendo que copiarlo.
    """
    from rapidfuzz import fuzz

    plano_e, plano_t = _plano(extracto), _plano(texto)
    if len(plano_e) < _MIN_EXTRACTO or not plano_t:
        return None
    alineacion = fuzz.partial_ratio_alignment(plano_e, plano_t)
    if alineacion is None or alineacion.score < _MIN_PARECIDO:
        return None
    return plano_t[alineacion.dest_start : alineacion.dest_end][:300]


def fecha_sustentada(fecha: date, texto: str) -> bool:
    """Si el mes y el año de `fecha` están escritos en `texto`.

    Reconoce el mes en letra junto al año ("5 de noviembre de 2026",
    "nov. 2026", "Nov 5, 2026") y las formas numéricas ("2026-11-05",
    "05/11/2026", "11/2026"), lo que incluye la anotación `[≈ 2026-11]` que
    el navegador agrega a las fechas relativas de una página recién leída.
    El día no se comprueba. Una fecha relativa sin anotar ("hace 3 días" en
    el extracto de un buscador) NO sustenta nada: es relativa al día en que
    el motor rastreó la página, que no se conoce.

    Args:
        fecha: la fecha citada.
        texto: la pieza tal como la vio el modelo.

    Returns:
        bool: True si el texto la respalda.
    """
    t = texto.lower()
    a, m = fecha.year, fecha.month
    patrones = (
        # mes en letra y despues el anio: "noviembre de 2026", "nov 5, 2026"
        rf"\b(?:{_MESES[m]})\b\.?,?\s+(?:\d{{1,2}}(?:st|nd|rd|th)?,?\s+)?(?:de\s+|del\s+)?{a}(?!\d)",
        rf"(?<!\d){a}[-/.]0?{m}(?!\d)",  # 2026-11-05, 2026/11
        rf"(?<!\d)0?{m}[-/.]{a}(?!\d)",  # 05/11/2026, 11/2026
        rf"(?<!\d)0?{m}[-/.]\d{{1,2}}[-/.]{a}(?!\d)",  # 11/05/2026 (mes primero)
    )
    return any(re.search(p, t) for p in patrones)


def parsear_fecha(valor: Any) -> tuple[date | None, str | None]:
    """`AAAA-MM-DD` o `AAAA-MM` a `date`; (None, None) si viene vacío; (None, error) si no parsea."""
    if valor in (None, "", "null"):
        return None, None
    texto = str(valor).strip()
    for formato in ("%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime(texto, formato).date(), None
        except ValueError:
            continue
    return None, f"{texto!r} no es AAAA-MM-DD ni AAAA-MM"


def _plano(texto: str) -> str:
    """Minúsculas, sin comillas y con los espacios colapsados: para comparar citas."""
    t = texto.lower()
    t = re.sub(r"[\"'“”‘’«»]", "", t)
    return re.sub(r"\s+", " ", t).strip(" .,;:")
