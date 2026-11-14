"""Cliente HTTP de Ollama: una llamada de chat con herramientas, y nada mas.

Es `requests` contra `/api/chat` y no el SDK `ollama`, por la misma razon
que `crear_agentes.md` §9.5 da para el adjudicador: la superficie que se usa
son dos endpoints, y una dependencia mas es una cosa mas que fijar en
`uv.lock` para ahorrarse treinta lineas.

Lo que si hace, porque ningun agente deberia repetirlo:

- **Comprueba que el modelo este descargado** antes de la primera llamada,
  con un mensaje que dice que correr. Sin esto, el primer `chat` tarda lo
  que tarde Ollama en bajar 20 GB y parece un cuelgue.
- **Reintenta los errores de conexion**, no los de modelo: Ollama se
  reinicia, la red parpadea; una respuesta malformada no se arregla
  repitiendola.
- **Temperatura 0 por default.** El agente tiene que ser lo mas
  reproducible que un LLM permite; la variabilidad que queda es de la web,
  no del muestreo del modelo.
"""

from __future__ import annotations

import time
from typing import Any

import requests

Mensaje = dict[str, Any]
"""Un mensaje del protocolo de chat: `{"role": ..., "content": ..., "tool_calls": [...]}`."""

_REINTENTOS_CONEXION = 3
"""Cuantas veces se reintenta un error de red antes de rendirse."""

_ESPERA_REINTENTO_S = 5.0
"""Segundos entre reintentos de conexion."""


class OllamaNoDisponible(RuntimeError):
    """Ollama no responde o no tiene el modelo pedido; el mensaje dice que hacer."""


class OllamaCliente:
    """Una conversacion con un modelo de Ollama, con herramientas.

    Attributes:
        url: base del servidor (`http://host.docker.internal:11434`).
        modelo: tag del modelo (`qwen3:32b`).
        timeout: segundos maximos por llamada; un 32B razonando con 30k de
            contexto puede tardar minutos, asi que no es el timeout de un API.
        num_ctx: ventana de contexto que se pide al servidor. Ollama arranca
            en 4k por default, que no alcanza para dos paginas leidas.
        pensar: si el modelo razona antes de responder (`think` de Ollama).
            Lo entienden qwen3 y gpt-oss; el resto lo ignora.
    """

    def __init__(
        self,
        url: str,
        modelo: str,
        *,
        timeout: int = 600,
        num_ctx: int = 32768,
        pensar: bool = False,
        temperatura: float = 0.0,
    ):
        self.url = url.rstrip("/")
        self.modelo = modelo
        self.timeout = timeout
        self.num_ctx = num_ctx
        self.pensar = pensar
        self.temperatura = temperatura

    # ----- disponibilidad -----

    def comprobar(self) -> None:
        """Falla con instrucciones si el servidor no responde o no tiene el modelo.

        Raises:
            OllamaNoDisponible: con el comando que falta correr.
        """
        try:
            respuesta = requests.get(f"{self.url}/api/tags", timeout=10)
            respuesta.raise_for_status()
        except requests.RequestException as e:
            raise OllamaNoDisponible(
                f"Ollama no responde en {self.url} ({e.__class__.__name__}). "
                "Arrancalo en el host (`ollama serve`) o ajusta OLLAMA_URL."
            ) from e
        tags = {m.get("name", "") for m in respuesta.json().get("models", [])}
        # `qwen3:32b` y `qwen3:32b-q4_K_M` son el mismo modelo con y sin
        # sufijo de cuantizacion; `latest` tambien se omite al listar.
        base = self.modelo.split(":")[0]
        if self.modelo not in tags and not any(t.split(":")[0] == base for t in tags):
            raise OllamaNoDisponible(
                f"el modelo {self.modelo!r} no esta descargado en {self.url}. "
                f"Corre `ollama pull {self.modelo}`. Disponibles: {sorted(tags) or 'ninguno'}"
            )

    # ----- chat -----

    def chat(self, mensajes: list[Mensaje], herramientas: list[dict] | None = None) -> Mensaje:
        """Una vuelta de chat: devuelve el mensaje del asistente.

        Args:
            mensajes: la conversacion completa hasta ahora, en orden.
            herramientas: definiciones de funciones (formato OpenAI/Ollama).
                Si el modelo decide llamarlas, vienen en `tool_calls`.

        Returns:
            Mensaje: `{"role": "assistant", "content": str, "tool_calls": [...]}`
            mas `_tokens` con los conteos que reporta el servidor.

        Raises:
            OllamaNoDisponible: si la red falla tras los reintentos.
            requests.HTTPError: si Ollama rechaza la peticion (modelo sin
                soporte de herramientas, contexto excedido...).
        """
        cuerpo: dict[str, Any] = {
            "model": self.modelo,
            "messages": mensajes,
            "stream": False,
            "think": self.pensar,
            "options": {"num_ctx": self.num_ctx, "temperature": self.temperatura},
        }
        if herramientas:
            cuerpo["tools"] = herramientas

        ultimo_error: Exception | None = None
        for intento in range(1, _REINTENTOS_CONEXION + 1):
            try:
                respuesta = requests.post(f"{self.url}/api/chat", json=cuerpo, timeout=self.timeout)
                respuesta.raise_for_status()
                datos = respuesta.json()
                mensaje = dict(datos["message"])
                mensaje["_tokens"] = {
                    "prompt": datos.get("prompt_eval_count"),
                    "respuesta": datos.get("eval_count"),
                }
                return mensaje
            except (requests.ConnectionError, requests.Timeout) as e:
                ultimo_error = e
                if intento < _REINTENTOS_CONEXION:
                    time.sleep(_ESPERA_REINTENTO_S)
        raise OllamaNoDisponible(
            f"Ollama en {self.url} no respondio tras {_REINTENTOS_CONEXION} intentos: {ultimo_error}"
        )
