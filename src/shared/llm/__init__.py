"""Infraestructura de modelos de lenguaje: el cliente de Ollama.

Sin dominio ni herramientas: lo que hay aqui le sirve igual al verificador
de supervivencia, al adjudicador CLEE y a cualquier agente que venga
(`crear_agentes.md` §9.3). Vive en `shared` porque `agents` y `etl` lo
necesitan sin depender uno del otro.
"""

from src.shared.llm.client import Mensaje, OllamaCliente, OllamaNoDisponible

__all__ = ["Mensaje", "OllamaCliente", "OllamaNoDisponible"]
