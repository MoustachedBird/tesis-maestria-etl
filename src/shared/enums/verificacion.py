"""Vocabulario del agente verificador: qué puede concluir y con qué apoyo.

Son enums y no texto libre porque las tres columnas que los usan son las que
se cuentan: la tasa de acierto de la regla de cierre sale de cruzar
`gold.negocios_clee_oficial.cerrado` contra `EstadoVerificado`, y un agente
que escribiera "CERRADO", "cerrado " o "closed" rompería ese conteo sin
error. El `CHECK` de la tabla se genera de estos valores.
"""

from enum import StrEnum


class EstadoVerificado(StrEnum):
    """Lo que el agente concluyó sobre el negocio, buscándolo en línea.

    Tres valores y no dos a propósito: `INDETERMINADO` es un resultado
    legítimo --el negocio no deja huella en línea, o la que deja no permite
    decidir-- y forzarlo a `activo` o `cerrado` contaminaría la medida de la
    regla con adivinanzas. La proporción de indeterminados es, además, un
    dato: dice qué tan verificable es la cohorte.
    """

    ACTIVO = "activo"
    CERRADO = "cerrado"
    INDETERMINADO = "indeterminado"


class Confianza(StrEnum):
    """Cuánto respalda la evidencia al veredicto, en tres niveles.

    Ordinal y no un número entre 0 y 1: un LLM que reporta 0.83 no está
    midiendo nada, y tres niveles son lo que un lector puede interpretar y
    lo que permite filtrar la muestra ("solo los veredictos con confianza
    alta") sin discutir un umbral decimal.
    """

    ALTA = "alta"
    MEDIA = "media"
    BAJA = "baja"


class SenalEvidencia(StrEnum):
    """Hacia dónde apunta UNA pieza de evidencia, por separado del veredicto.

    Se guarda por pieza y no solo en el veredicto porque la evidencia suele
    ser contradictoria (una ficha de Google Maps "cerrado permanentemente"
    y una página de Facebook con una publicación del mes pasado), y lo que
    vuelve auditable al agente es ver cómo pesó cada una.
    """

    CERRADO = "cerrado"
    ACTIVO = "activo"
    NEUTRAL = "neutral"
