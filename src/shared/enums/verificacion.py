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


class Recolector(StrEnum):
    """Qué paso del recolector produjo una pieza del expediente.

    El expediente se arma siempre con los mismos pasos y en este orden, para
    que dos negocios se verifiquen con las mismas fuentes y sus veredictos
    sean comparables. Es el eje para medir qué aporta cada fuente ("¿cuántos
    veredictos firmes salieron de Maps?").
    """

    MAPS = "maps"
    WEB = "web"
    REDES = "redes"
    SITIO = "sitio"


class EstadoConsulta(StrEnum):
    """Cómo terminó una consulta del recolector.

    Se guarda también lo que falló: que Maps no tenga ficha o que Facebook
    pida sesión es información sobre la cobertura de cada fuente, y sin la
    fila no se distingue "no se consultó" de "se consultó y no había nada".
    """

    OK = "ok"
    SIN_RESULTADOS = "sin_resultados"
    ERROR = "error"


class Dictamen(StrEnum):
    """Lo que el juez concluye del veredicto que audita.

    `SOSTENIDO`: las piezas que el decisor citó dicen lo que les atribuye y
    alcanzan para ese estado con esa confianza. `NO_SOSTENIDO`: alguna cita
    no dice eso, es de un homónimo o de un directorio que copia al DENUE, o
    la confianza excede lo que las piezas permiten.
    """

    SOSTENIDO = "sostenido"
    NO_SOSTENIDO = "no_sostenido"


class Identificacion(StrEnum):
    """Si lo que trajo una fuente es de ESTE negocio, según su especialista.

    La identidad es nombre + lugar: una taquería homónima en otro municipio
    no dice nada de la del DENUE. Se separa de la señal porque son errores
    distintos --encontrar al negocio equivocado y leer mal al correcto-- y
    el análisis los cuenta por separado.
    """

    SI = "si"
    NO = "no"
    DUDOSO = "dudoso"
