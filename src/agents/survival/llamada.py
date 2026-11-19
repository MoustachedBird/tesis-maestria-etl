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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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

    Returns:
        Salida | None: la terminal válida, o None si se agotaron las vueltas.
    """
    nombres = ", ".join(sorted(terminales))
    for vuelta in range(1, max_vueltas + 1):
        respuesta = cliente.chat(mensajes, herramientas)
        mensajes.append({k: v for k, v in respuesta.items() if not k.startswith("_")})
        llamadas = respuesta.get("tool_calls") or []
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
