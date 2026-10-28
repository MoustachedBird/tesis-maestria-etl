from enum import StrEnum


class OrigenClee(StrEnum):
    """Cómo se le asignó su identidad a una observación.

    Vive en `shared` porque lo escribe el ETL (la resolución CLEE), lo lee el
    API al serializar métricas de resolución, y Gold lo necesita para decidir
    qué identidades son defendibles como negocio real.

    Hoy la distinción solo se podía inferir con trucos de string sobre el
    prefijo `NEW_`, lo que hace imposible separar una identidad que INEGI
    publicó de una que el pipeline infirió por similitud. Para una tesis que
    compara métodos de resolución, esa separación es el dato.
    """

    DIRECTO = "DIRECTO"
    """La fila traía `clee` de INEGI y se copió tal cual.

    Es la identidad de mayor confianza: no hay inferencia de por medio. Cubre
    los cortes con cobertura CLEE del 100%.
    """

    PROPAGADO = "PROPAGADO"
    """Se heredó de una observación vecina ya resuelta, por similitud.

    El método (radio/BallTree o blocking H3) es la tabla donde vive la fila,
    y su evidencia —score, distancia, celda— son sus columnas propias.
    """

    NUEVO = "NUEVO"
    """No se encontró candidato y se generó un CLEE sintético `NEW_<pref>-<hex>`.

    **No es estable entre corridas:** el sufijo es aleatorio, así que una
    re-resolución le da otro id a la misma fila. Nunca usarlo como llave
    permanente hacia afuera.
    """


class EstatusNegocio(StrEnum):
    """Veredicto de la regla de cierre sobre un establecimiento.

    NO se almacena en `silver.permanencia_*`: lo calcula la vista
    `silver.estatus_<metodo>`. El protocolo de la tesis define la regla de
    cierre como *preliminar* --pendiente de validar contra fuentes
    externas-- asi que vive en una vista, que se redefine en un segundo,
    y no en una columna, que obligaria a reconstruir 10M de filas con cada
    ajuste.
    """

    ACTIVO = "ACTIVO"
    """Presente en el ultimo corte publicado: censura por la derecha.

    No significa "sigue abierto hoy", sino que el DENUE todavia no ha
    tenido oportunidad de dejar de verlo.
    """

    CERRADO = "CERRADO"
    """Ausente de `CORTES_PARA_CIERRE` cortes consecutivos sin reaparecer.

    Es el veredicto de la regla, no un hecho verificado: `docs/findings.md`
    §2 mide que la tasa de desaparicion oscila por un factor de 400 entre
    pares de cortes, lo que sugiere que buena parte de las ausencias son
    reasignacion de claves y no cierres.
    """

    INCIERTO = "INCIERTO"
    """Ausente de menos cortes que el umbral.

    Una sola ausencia no distingue un cierre de una omision del directorio,
    y el protocolo exige dos para no confundirlos.
    """
