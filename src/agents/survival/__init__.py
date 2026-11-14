"""El agente de supervivencia: ¿el negocio que dejó de aparecer en el DENUE cerró?

Tres pasos, tres módulos, un comando (`src.agents.main`):

    muestreo.py      saca la muestra estratificada de gold y la escribe en
                     verificacion.muestras / muestra_negocios
    verificador.py   busca cada negocio en línea con un modelo de Ollama y
                     escribe verificacion.veredictos / evidencias
    metricas.py      cruza el veredicto contra la hipótesis de gold, por estrato

El diseño completo --criterio, estratos, protocolo de búsqueda, reglas de
decisión-- está en `verificacion_cierre.md`; aquí solo hay código.
"""
