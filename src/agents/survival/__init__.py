"""El agente de supervivencia: ¿el negocio que dejó de aparecer en el DENUE cerró?

Un comando (`src.agents.main`) y cuatro etapas:

    muestreo.py      saca la muestra estratificada de gold y la escribe en
                     verificacion.muestras / muestra_negocios
    verificador.py   el equipo: por fuente, recolección base (recoleccion.py)
                     y un especialista; un decisor integra (equipo.py)
    juez.py          audita cada veredicto contra el expediente guardado
    metricas.py      cruza el veredicto contra la hipótesis de gold, por estrato

El diseño completo --criterio, estratos, protocolo, reglas de decisión-- está
en `verificacion_cierre.md`; la arquitectura del equipo, en AGENTS.md N7.
"""
