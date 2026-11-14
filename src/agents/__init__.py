"""Agentes LLM sobre el warehouse: un subpaquete por agente.

La arquitectura es la de `crear_agentes.md` §9: este paquete es de primer
nivel porque sus agentes no son parte del ETL --el verificador de
supervivencia escribe en `verificacion`, no en Silver-- y solo puede
importar de `src.shared` y `src.meta` (lo hace valer
`tests/unit/test_layer_boundaries.py`).

El primero que existe es el verificador de supervivencia (`survival/`); el
adjudicador CLEE y el analista del plan vienen despues y comparten
`src.shared.llm`.
"""
