"""El equipo que verifica un negocio: un especialista por grupo de fuentes y un decisor.

Es la arquitectura de "equipo de agentes con roles" que propone la
literatura de sistemas multiagente, adaptada a un problema de MEDICIÓN:

- Cada **especialista** tiene un contexto chico y una sola tarea --leer lo
  que trajeron sus fuentes (un `Grupo`: los mapas, los buscadores, la huella
  digital...) y decir si es este negocio y qué indica--, en vez de un agente
  generalista con el expediente entero encima. Puede pedir unas pocas
  consultas extra dentro de sus fuentes.
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

import re
import time
import unicodedata
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
    HERRAMIENTA_ELEGIR_NOMBRES,
    HERRAMIENTA_INFORMAR,
    SISTEMA_DECISOR,
    SISTEMA_PLANIFICADOR,
    formatear_insumo,
    sistema_especialista,
)
from src.agents.survival.recoleccion import Grupo, Pieza, formatear_expediente, nombre_de
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


# ----- planificador de nombres -----


@dataclass
class Planeacion:
    """Con qué nombres se busca al negocio, y por qué."""

    nombres: list[str]
    razonamiento: str
    automatica: bool = False
    duracion_s: float = 0.0

    def como_dict(self) -> dict:
        """Para `veredictos.nombres_buscados`."""
        return {
            "nombres": self.nombres,
            "razonamiento": self.razonamiento,
            "automatica": self.automatica,
        }


class Planificador:
    """El agente que decide con qué nombres buscar, antes de la recolección.

    El nombre del DENUE cambia entre cortes (artículos, orden, errores de
    captura, un cambio de nombre real), y buscar solo el de gold pierde al
    negocio cuando en línea aparece de otra forma. Qué criterio usar
    --repetición, recencia, combinación-- lo decide el modelo; el código solo
    exige que no invente: toda palabra que proponga tiene que estar en algún
    nombre o razón social del DENUE.
    """

    def __init__(self, cliente: Any, *, max_nombres: int, max_intentos: int):
        """
        Args:
            cliente: `OllamaCliente` o cualquier objeto con `.chat(mensajes, herramientas)`.
            max_nombres: cuántos nombres puede elegir (`AGENTE_MAX_NOMBRES`).
            max_intentos: vueltas para producir una elección válida.
        """
        self.cliente = cliente
        self.max_nombres = max_nombres
        self.max_intentos = max_intentos

    def elegir(self, n: Negocio) -> Planeacion:
        """Los nombres de búsqueda; si el modelo no da una elección válida, el de gold."""
        inicio = time.monotonic()
        respaldo = nombre_de(n)
        if not respaldo:
            return Planeacion([], "El DENUE no publicó ningún nombre.", automatica=True)
        mensajes = [
            {"role": "system", "content": SISTEMA_PLANIFICADOR.format(k=self.max_nombres)},
            {"role": "user", "content": formatear_insumo(n)},
        ]
        vocabulario = _palabras(" ".join([*n.nombres, n.nombre or "", *n.razones_sociales]))

        def validar(_nombre: str, args: dict) -> tuple[Planeacion, list[str]]:
            return _validar_nombres(args, vocabulario, self.max_nombres)

        salida = pedir(
            self.cliente,
            mensajes,
            [HERRAMIENTA_ELEGIR_NOMBRES],
            {"elegir_nombres"},
            validar,
            max_vueltas=self.max_intentos,
            rol="planificador de nombres",
        )
        if salida is None:
            return Planeacion(
                [respaldo] if respaldo else [],
                f"El planificador no eligió nombres válidos en {self.max_intentos} intentos; "
                "se busca con el nombre de gold.",
                automatica=True,
                duracion_s=time.monotonic() - inicio,
            )
        plan: Planeacion = salida.valor
        plan.duracion_s = time.monotonic() - inicio
        return plan


def _validar_nombres(
    args: dict, vocabulario: set[str], maximo: int
) -> tuple[Planeacion, list[str]]:
    """Que sean de 1 a `maximo`, distintos, y sin palabras que el DENUE no publicó."""
    errores: list[str] = []
    crudos = args.get("nombres")
    if not isinstance(crudos, list):
        crudos = []
    nombres: list[str] = []
    for x in crudos:
        nombre = " ".join(str(x).split())
        if len(nombre) < 3:
            continue
        if _palabras_llave(nombre) in {_palabras_llave(y) for y in nombres}:
            continue  # el mismo nombre otra vez (mayusculas, acentos): se cuenta una
        nuevas = sorted(_palabras(nombre) - vocabulario)
        if nuevas:
            errores.append(
                f"«{nombre}» usa palabras que el DENUE no publicó: {nuevas}. Usa solo palabras "
                "de los nombres o la razón social (puedes quitar, reordenar y combinar)"
            )
        nombres.append(nombre)
    if not nombres:
        errores.append("da al menos un nombre")
    if len(nombres) > maximo:
        errores.append(f"son {len(nombres)} nombres; el máximo es {maximo}")
    razonamiento = str(args.get("razonamiento", "")).strip()
    if len(razonamiento) < MIN_TEXTO:
        errores.append("razonamiento vacío o demasiado corto: di por qué esos nombres")
    return Planeacion(nombres, razonamiento), errores


def _palabras(texto: str) -> set[str]:
    """Las palabras de un texto, en mayúsculas, sin acentos ni puntuación."""
    plano = unicodedata.normalize("NFKD", texto.upper()).encode("ascii", "ignore").decode()
    return set(re.findall(r"[A-Z0-9]+", plano))


def _palabras_llave(texto: str) -> frozenset[str]:
    """Un nombre como conjunto de palabras: para no contar dos veces el mismo."""
    return frozenset(_palabras(texto))


# ----- especialista -----


@dataclass
class Informe:
    """Lo que el especialista de un grupo de fuentes concluyó de ellas."""

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
    """El agente que lee UN grupo de fuentes; uno solo sirve para todos, cambia el prompt."""

    def __init__(
        self,
        cliente: Any,
        *,
        max_ampliaciones: int,
        max_intentos: int,
        presupuesto: int | None = None,
    ):
        """
        Args:
            cliente: `OllamaCliente` o cualquier objeto con `.chat(mensajes, herramientas)`.
            max_ampliaciones: consultas extra que puede pedir dentro de sus fuentes (en total).
            max_intentos: vueltas para producir un informe válido.
            presupuesto: caracteres de piezas que caben en su contexto
                (`recoleccion.presupuesto_de`); None = sin tope.
        """
        self.cliente = cliente
        self.max_ampliaciones = max_ampliaciones
        self.max_intentos = max_intentos
        self.presupuesto = presupuesto

    def informar(self, n: Negocio, grupo: Grupo, expediente: list[Pieza]) -> Informe:
        """Lee lo que trajeron las fuentes de `grupo` y produce su informe.

        `expediente` se modifica en sitio: las consultas extra que pida el
        especialista se agregan al final, con su número, y quedan guardadas
        aunque no las cite.
        """
        inicio = time.monotonic()
        especialidad = grupo.especialidad
        recolectores = grupo.recolectores

        def propias() -> list[int]:
            return [i for i, p in enumerate(expediente, 1) if p.recolector in recolectores]

        herramientas_ampliar = grupo.herramientas(expediente) if self.max_ampliaciones > 0 else []
        puede_ampliar = bool(herramientas_ampliar)
        if not any(expediente[i - 1].citable for i in propias()) and not puede_ampliar:
            return Informe(
                especialista=especialidad,
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
            f"LO QUE TRAJERON {FOCOS[especialidad][0].upper()}",
            "",
            formatear_expediente([expediente[i - 1] for i in numeros], numeros, self.presupuesto),
        ]
        extra = grupo.contexto(expediente)
        if extra:
            contenido += ["", extra]
        logger.info(
            f"         📖 especialista {especialidad}: lee {len(numeros)} piezas "
            f"({sum(expediente[i - 1].citable for i in numeros)} citables, "
            f"{sum(len(c) for c in contenido):,} caracteres)"
        )
        mensajes = [
            {
                "role": "system",
                "content": sistema_especialista(
                    especialidad,
                    self.max_ampliaciones if puede_ampliar else 0,
                    herramientas_ampliar,
                ),
            },
            {"role": "user", "content": "\n".join(contenido)},
        ]
        herramientas = [HERRAMIENTA_INFORMAR] + herramientas_ampliar
        usadas = 0

        def ejecutar(nombre: str, args: dict) -> str:
            nonlocal usadas
            fuente = grupo.fuente_de(nombre)
            if fuente is None:
                return f"ERROR: {nombre} no es una de tus herramientas."
            if usadas >= self.max_ampliaciones:
                return "PRESUPUESTO AGOTADO: no hay más consultas. Informa con lo que tienes."
            usadas += 1
            nuevas = fuente.ampliar(n, args, expediente)
            desde = len(expediente) + 1
            expediente.extend(nuevas)
            logger.info(f"         + {fuente.recolector}: {args} -> {len(nuevas)} piezas")
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
            informe = Informe(especialidad, identificado, senal, resumen, citas)
            return informe, errores

        salida = pedir(
            self.cliente,
            mensajes,
            herramientas,
            {"informar"},
            validar,
            ejecutar=ejecutar if puede_ampliar else None,
            max_vueltas=self.max_ampliaciones + self.max_intentos + 1,
            rol=f"especialista {especialidad}",
        )
        if salida is None:
            return Informe(
                especialista=especialidad,
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

        logger.info(
            f"      ⚖  decisor: integra {len(informes)} informes ({len(citables)} piezas citadas)"
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
            rol="decisor",
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
