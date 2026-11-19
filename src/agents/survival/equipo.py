"""El equipo que verifica un negocio: un especialista por fuente y un decisor.

Es la arquitectura de "equipo de agentes con roles" que propone la
literatura de sistemas multiagente, adaptada a un problema de MEDICIÓN:

- Cada **especialista** tiene un contexto chico y una sola tarea --leer lo
  que trajo su fuente y decir si es este negocio y qué indica--, en vez de
  un agente generalista con el expediente entero encima. Puede pedir unas
  pocas consultas extra dentro de su fuente.
- El **decisor** no busca: integra los informes. Solo puede apoyarse en
  piezas que algún especialista citó.
- El **juez** (`juez.py`) está fuera del equipo a propósito: no ve los
  informes, relee el expediente crudo.

Lo que NO decide ningún agente es qué fuentes se consultan: la recolección
base es la misma para todos los negocios (`recoleccion.py`). Es lo que
mantiene comparables los veredictos.

Todo rol termina en una herramienta validada en código (`citas.py`); si el
modelo no produce una salida válida, el resultado es `automatico` y dice
por qué, en vez de inventarse un veredicto.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from src.agents.survival.citas import Cita, parsear_fecha, validar_citas
from src.agents.survival.insumo import Negocio
from src.agents.survival.llamada import pedir
from src.agents.survival.prompts import (
    FOCOS,
    HERRAMIENTA_CONCLUIR,
    HERRAMIENTA_INFORMAR,
    SISTEMA_DECISOR,
    formatear_insumo,
    sistema_especialista,
)
from src.agents.survival.recoleccion import Fuente, Pieza, formatear_expediente
from src.shared.enums import (
    Confianza,
    EstadoVerificado,
    Identificacion,
    SenalEvidencia,
)
from src.shared.logging import get_stage_logger

logger = get_stage_logger(__name__, Path("./data/agente.log"))

MIN_TEXTO = 20
"""Caracteres mínimos de un resumen o razonamiento: menos no es un porqué."""


# ----- especialista -----


@dataclass
class Informe:
    """Lo que el especialista de una fuente concluyó de ella."""

    especialista: str
    identificado: str
    senal: str
    resumen: str
    citas: list[Cita] = field(default_factory=list)
    automatico: bool = False
    duracion_s: float = 0.0

    @property
    def fecha_mas_reciente(self) -> date | None:
        """La fecha más reciente entre sus citas, si alguna la trae."""
        fechas = [c.fecha_publicacion for c in self.citas if c.fecha_publicacion]
        return max(fechas) if fechas else None

    def como_texto(self) -> str:
        """El informe para el decisor."""
        nombre = FOCOS[self.especialista][0]
        lineas = [
            f"INFORME DE {nombre.upper()} ({self.especialista})",
            f"identificado: {self.identificado} · señal: {self.senal}",
            f"resumen: {self.resumen}",
        ]
        if self.citas:
            lineas.append("citas:")
            lineas += [f"  {c.como_texto()}" for c in self.citas]
        else:
            lineas.append("citas: ninguna")
        return "\n".join(lineas)


class Especialista:
    """El agente que lee UNA fuente; uno solo sirve para todas, cambia el prompt."""

    def __init__(self, cliente: Any, *, max_ampliaciones: int, max_intentos: int):
        """
        Args:
            cliente: `OllamaCliente` o cualquier objeto con `.chat(mensajes, herramientas)`.
            max_ampliaciones: consultas extra que puede pedir dentro de su fuente.
            max_intentos: vueltas para producir un informe válido.
        """
        self.cliente = cliente
        self.max_ampliaciones = max_ampliaciones
        self.max_intentos = max_intentos

    def informar(self, n: Negocio, fuente: Fuente, expediente: list[Pieza]) -> Informe:
        """Lee lo que trajo `fuente` y produce su informe.

        `expediente` se modifica en sitio: las consultas extra que pida el
        especialista se agregan al final, con su número, y quedan guardadas
        aunque no las cite.
        """
        inicio = time.monotonic()
        recolector = fuente.recolector

        def propias() -> list[int]:
            return [i for i, p in enumerate(expediente, 1) if p.recolector == recolector]

        puede_ampliar = fuente.herramienta is not None and self.max_ampliaciones > 0
        if not any(expediente[i - 1].citable for i in propias()) and not puede_ampliar:
            return Informe(
                especialista=recolector,
                identificado=Identificacion.NO.value,
                senal=SenalEvidencia.NEUTRAL.value,
                resumen=_sin_nada(expediente, propias()),
                automatico=True,
                duracion_s=time.monotonic() - inicio,
            )

        numeros = propias()
        contenido = [
            formatear_insumo(n),
            "",
            f"LO QUE TRAJO {FOCOS[recolector][0].upper()}",
            "",
            formatear_expediente([expediente[i - 1] for i in numeros], numeros),
        ]
        extra = fuente.contexto(expediente)
        if extra:
            contenido += ["", extra]
        mensajes = [
            {
                "role": "system",
                "content": sistema_especialista(
                    recolector, self.max_ampliaciones if puede_ampliar else 0
                ),
            },
            {"role": "user", "content": "\n".join(contenido)},
        ]
        herramientas = [HERRAMIENTA_INFORMAR] + ([fuente.herramienta] if puede_ampliar else [])
        usadas = 0

        def ejecutar(_nombre: str, args: dict) -> str:
            nonlocal usadas
            if usadas >= self.max_ampliaciones:
                return "PRESUPUESTO AGOTADO: no hay más consultas. Informa con lo que tienes."
            usadas += 1
            nuevas = fuente.ampliar(n, args, expediente)
            desde = len(expediente) + 1
            expediente.extend(nuevas)
            logger.info(f"         + {recolector}: {args} -> {len(nuevas)} piezas")
            if not nuevas:
                return "(sin resultados nuevos)"
            return formatear_expediente(nuevas, list(range(desde, desde + len(nuevas))))

        def validar(_nombre: str, args: dict) -> tuple[Informe, list[str]]:
            errores: list[str] = []
            identificado = str(args.get("identificado", "")).strip().lower()
            senal = str(args.get("senal", "")).strip().lower()
            resumen = str(args.get("resumen", "")).strip()
            if identificado not in {i.value for i in Identificacion}:
                errores.append(
                    f"identificado {identificado!r} no es uno de {_valores(Identificacion)}"
                )
            if senal not in {s.value for s in SenalEvidencia}:
                errores.append(f"senal {senal!r} no es una de {_valores(SenalEvidencia)}")
            if len(resumen) < MIN_TEXTO:
                errores.append("resumen vacío o demasiado corto")
            citas, errores_citas = validar_citas(args.get("citas"), expediente, set(propias()))
            errores += errores_citas
            if identificado != Identificacion.SI.value and senal != SenalEvidencia.NEUTRAL.value:
                errores.append("si el negocio no está identificado, la señal es neutral")
            if senal != SenalEvidencia.NEUTRAL.value and not any(c.senal == senal for c in citas):
                errores.append(f"una señal {senal!r} necesita al menos una cita con esa señal")
            informe = Informe(recolector, identificado, senal, resumen, citas)
            return informe, errores

        salida = pedir(
            self.cliente,
            mensajes,
            herramientas,
            {"informar"},
            validar,
            ejecutar=ejecutar if puede_ampliar else None,
            max_vueltas=self.max_ampliaciones + self.max_intentos + 1,
        )
        if salida is None:
            return Informe(
                especialista=recolector,
                identificado=Identificacion.DUDOSO.value,
                senal=SenalEvidencia.NEUTRAL.value,
                resumen=(
                    "El especialista no produjo un informe válido dentro de su presupuesto; "
                    "esta fuente no aporta al veredicto."
                ),
                automatico=True,
                duracion_s=time.monotonic() - inicio,
            )
        informe: Informe = salida.valor
        informe.duracion_s = time.monotonic() - inicio
        return informe


# ----- decisor -----


@dataclass
class Decision:
    """El veredicto del equipo."""

    estado: str
    confianza: str
    razonamiento: str
    fecha_cierre_estimada: date | None = None
    evidencias: list[Cita] = field(default_factory=list)
    automatica: bool = False


class Decisor:
    """Integra los informes de los especialistas y concluye; no busca."""

    def __init__(self, cliente: Any, *, max_intentos: int):
        """
        Args:
            cliente: `OllamaCliente` o cualquier objeto con `.chat(mensajes, herramientas)`.
            max_intentos: vueltas para producir una conclusión válida.
        """
        self.cliente = cliente
        self.max_intentos = max_intentos

    def decidir(self, n: Negocio, informes: list[Informe], expediente: list[Pieza]) -> Decision:
        """El veredicto a partir de los informes.

        Si ningún especialista citó nada, no hay nada que integrar: el
        veredicto es `indeterminado` sin llamar al modelo. El silencio en
        línea no prueba un cierre --muchos negocios chicos nunca tuvieron
        huella--, y la regla lo dice igual.
        """
        citables = {c.pieza for i in informes for c in i.citas}
        if not citables:
            return Decision(
                estado=EstadoVerificado.INDETERMINADO.value,
                confianza=Confianza.BAJA.value,
                razonamiento=(
                    "Ninguna fuente identificó al negocio con una pieza citable "
                    f"({_identificaciones(informes)}). Sin huella en línea no se puede afirmar "
                    "si sigue activo o cerró."
                ),
                automatica=True,
            )

        mensajes = [
            {"role": "system", "content": SISTEMA_DECISOR},
            {
                "role": "user",
                "content": "\n".join(
                    [
                        formatear_insumo(n),
                        "",
                        "INFORMES DE LOS ESPECIALISTAS",
                        "",
                        "\n\n".join(i.como_texto() for i in informes),
                    ]
                ),
            },
        ]

        def validar(_nombre: str, args: dict) -> tuple[Decision, list[str]]:
            return _validar_conclusion(args, n, expediente, citables)

        salida = pedir(
            self.cliente,
            mensajes,
            [HERRAMIENTA_CONCLUIR],
            {"concluir"},
            validar,
            max_vueltas=self.max_intentos,
        )
        if salida is None:
            return Decision(
                estado=EstadoVerificado.INDETERMINADO.value,
                confianza=Confianza.BAJA.value,
                razonamiento=(
                    f"El decisor no produjo una conclusión válida en {self.max_intentos} "
                    "intentos. Se registra como indeterminado."
                ),
                automatica=True,
            )
        return salida.valor


def _validar_conclusion(
    args: dict, n: Negocio, expediente: list[Pieza], citables: set[int]
) -> tuple[Decision, list[str]]:
    """Revisa la conclusión del decisor contra el vocabulario, las citas y las reglas que se pueden comprobar."""
    errores: list[str] = []
    estado = str(args.get("estado", "")).strip().lower()
    confianza = str(args.get("confianza", "")).strip().lower()
    razonamiento = str(args.get("razonamiento", "")).strip()
    if estado not in {e.value for e in EstadoVerificado}:
        errores.append(f"estado {estado!r} no es uno de {_valores(EstadoVerificado)}")
    if confianza not in {c.value for c in Confianza}:
        errores.append(f"confianza {confianza!r} no es una de {_valores(Confianza)}")
    if len(razonamiento) < MIN_TEXTO:
        errores.append("razonamiento vacío o demasiado corto: di por qué, con fuentes y fechas")
    fecha_cierre, error_fecha = parsear_fecha(args.get("fecha_cierre_estimada"))
    if error_fecha:
        errores.append(f"fecha_cierre_estimada: {error_fecha}")

    evidencias, errores_citas = validar_citas(args.get("evidencias"), expediente, citables)
    errores += [e.replace("cita ", "evidencia ", 1) for e in errores_citas]
    if estado in (EstadoVerificado.ACTIVO.value, EstadoVerificado.CERRADO.value) and not any(
        e.senal == estado for e in evidencias
    ):
        errores.append(f"un veredicto {estado!r} necesita al menos una evidencia con esa señal")
    if estado == EstadoVerificado.ACTIVO.value and confianza == Confianza.ALTA.value:
        corte = n.ultimo_corte_ventana
        if not any(
            e.senal == SenalEvidencia.ACTIVO.value
            and e.fecha_publicacion
            and e.fecha_publicacion >= corte
            for e in evidencias
        ):
            errores.append(
                f"activo/alta exige una evidencia activo FECHADA en o después del último corte "
                f"del DENUE ({corte:%Y-%m}); sin ella, la confianza es media o baja"
            )
    decision = Decision(estado, confianza, razonamiento, fecha_cierre, evidencias)
    return decision, errores


# ----- utilidades -----


def _valores(enum: type) -> list[str]:
    return [v.value for v in enum]


def _sin_nada(expediente: list[Pieza], numeros: list[int]) -> str:
    """Resumen de un informe automático: qué se consultó y cómo terminó."""
    if not numeros:
        return "La fuente no tenía nada que consultar para este negocio."
    estados = ", ".join(f"[{i}] {expediente[i - 1].estado}" for i in numeros)
    return f"La fuente no devolvió nada citable ({estados})."


def _identificaciones(informes: list[Informe]) -> str:
    return "; ".join(f"{i.especialista}: {i.identificado}" for i in informes)
