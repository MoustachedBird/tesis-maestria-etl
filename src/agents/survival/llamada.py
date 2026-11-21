"""Una conversación con el modelo que tiene que terminar en una herramienta válida.

Los tres roles del equipo (especialista, decisor, juez) hablan con el
modelo igual: le dan herramientas, el modelo puede usar algunas para
consultar y tiene que terminar llamando a una herramienta "terminal" cuyos
argumentos se validan en código. Si la validación falla, se le devuelve el
error y vuelve a intentar, no se corrige por él. Este módulo es ese bucle,
una sola vez.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.shared.logging import get_stage_logger

logger = get_stage_logger(__name__, Path("./data/agente.log"))

_MAX_ARGS_LOG = 160
"""Caracteres de los argumentos de una herramienta que se escriben en el log."""

LATIDO_SEGUNDOS = 60
"""Cada cuánto se escribe "sigue pensando" mientras una llamada al modelo no vuelve.

Sin esto, una llamada de varios minutos (un modelo recién cargado, un
contexto grande) se ve en el log igual que un proceso muerto.
"""

Validador = Callable[[str, dict], tuple[Any, list[str]]]
"""`(herramienta, argumentos) -> (valor, errores)`; con errores, el valor no vale."""

Ejecutor = Callable[[str, dict], str]
"""`(herramienta, argumentos) -> texto` para las herramientas que consultan."""


@dataclass
class Salida:
    """Cómo terminó una conversación: la herramienta terminal y su valor validado."""

    herramienta: str
    valor: Any
    vueltas: int


def pedir(
    cliente: Any,
    mensajes: list[dict],
    herramientas: list[dict],
    terminales: set[str],
    validar: Validador,
    *,
    ejecutar: Ejecutor | None = None,
    max_vueltas: int,
    rol: str = "modelo",
) -> Salida | None:
    """Conversa hasta que el modelo llame una herramienta terminal con argumentos válidos.

    `mensajes` se modifica en sitio: al volver tiene la conversación completa,
    así que quien llama puede seguirla (el juez la usa para su segundo paso).

    Args:
        cliente: `OllamaCliente` o cualquier objeto con `.chat(mensajes, herramientas)`.
        mensajes: la conversación hasta ahora (sistema + usuario como mínimo).
        herramientas: definiciones que se le ofrecen al modelo.
        terminales: nombres de las herramientas que cierran la conversación.
        validar: revisa los argumentos de una terminal.
        ejecutar: atiende las herramientas no terminales; None si no hay.
        max_vueltas: tope de llamadas al modelo.
        rol: quién habla ("especialista web", "decisor", "juez paso 1"), para el log.

    Returns:
        Salida | None: la terminal válida, o None si se agotaron las vueltas.
    """
    nombres = ", ".join(sorted(terminales))
    for vuelta in range(1, max_vueltas + 1):
        logger.info(f"         🤖 {rol} · vuelta {vuelta}/{max_vueltas}: pensando...")
        inicio = time.monotonic()
        with _latido(rol, inicio):
            respuesta = cliente.chat(mensajes, herramientas)
        mensajes.append({k: v for k, v in respuesta.items() if not k.startswith("_")})
        llamadas = respuesta.get("tool_calls") or []
        tokens = respuesta.get("_tokens") or {}
        logger.info(
            f"         🤖 {rol} · vuelta {vuelta}: {time.monotonic() - inicio:.0f}s · "
            f"{tokens.get('prompt') or '?'} tokens leídos, {tokens.get('respuesta') or '?'} escritos · "
            + (
                "llamó " + ", ".join(_resumen(*desempacar(x)) for x in llamadas)
                if llamadas
                else "respondió en prosa, sin herramienta"
            )
        )
        if not llamadas:
            mensajes.append(
                {"role": "user", "content": f"Responde únicamente llamando a: {nombres}."}
            )
            continue
        for llamada in llamadas:
            nombre, args = desempacar(llamada)
            if nombre in terminales:
                valor, errores = validar(nombre, args)
                if errores:
                    logger.info(f"         ✗ {rol}: {nombre} rechazado: {'; '.join(errores)[:300]}")
                    mensajes.append(tool(nombre, "RECHAZADO:\n- " + "\n- ".join(errores)))
                    continue
                return Salida(nombre, valor, vuelta)
            if ejecutar is not None and any(h["function"]["name"] == nombre for h in herramientas):
                mensajes.append(tool(nombre, ejecutar(nombre, args)))
                continue
            mensajes.append(tool(nombre, f"ERROR: no existe la herramienta {nombre!r}"))
        if vuelta == max_vueltas - 1:
            mensajes.append(
                {"role": "user", "content": f"Última oportunidad: llama a {nombres} AHORA."}
            )
    return None


class _latido:
    """Un hilo que escribe cada `LATIDO_SEGUNDOS` que la llamada sigue en curso."""

    def __init__(self, rol: str, inicio: float):
        self.rol = rol
        self.inicio = inicio
        self._fin = threading.Event()
        self._hilo = threading.Thread(target=self._latir, daemon=True)

    def __enter__(self) -> _latido:
        self._hilo.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._fin.set()

    def _latir(self) -> None:
        while not self._fin.wait(LATIDO_SEGUNDOS):
            minutos = (time.monotonic() - self.inicio) / 60
            logger.info(f"         ⏳ {self.rol} sigue pensando · {minutos:.0f} min")


def _resumen(nombre: str, args: dict) -> str:
    """Una llamada a herramienta en una línea: lo que decide, no las citas completas."""
    partes = []
    for clave, valor in args.items():
        if isinstance(valor, list):
            partes.append(f"{clave}=[{len(valor)}]")
        elif clave in ("resumen", "razonamiento", "motivo"):
            continue
        else:
            partes.append(f"{clave}={valor}")
    texto = f"{nombre}({', '.join(partes)})"
    return texto if len(texto) <= _MAX_ARGS_LOG else texto[:_MAX_ARGS_LOG] + "…)"


def tool(nombre: str, contenido: str) -> dict:
    """Un mensaje de resultado de herramienta, como lo espera Ollama."""
    return {"role": "tool", "tool_name": nombre, "content": contenido}


def desempacar(llamada: dict) -> tuple[str, dict]:
    """Nombre y argumentos de una `tool_call`; los argumentos pueden venir como JSON en texto."""
    funcion = llamada.get("function", llamada)
    args = funcion.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    return str(funcion.get("name", "")), dict(args) if isinstance(args, dict) else {}
