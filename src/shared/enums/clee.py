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
