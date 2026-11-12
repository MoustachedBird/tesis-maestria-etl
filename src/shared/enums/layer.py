from enum import StrEnum


class Layer(StrEnum):
    """Capas del warehouse, una por schema de PostgreSQL.

    Compartido entre el ETL (que escribe `silver` y deriva `gold`), el API
    (que solo lee `gold`), el tracker de metadatos (que etiqueta cada tarea
    con la capa que tocó) y el agente verificador (que escribe
    `verificacion`). Vive en `shared` porque todos lo necesitan sin tomar
    una dependencia cruzada entre módulos.

    El valor es literalmente el nombre del schema: se usa para calificar
    tablas, en los `GRANT` por capa y en `pg_dump -n`. Cambiar un valor
    renombra un schema, así que no son etiquetas libres.

    `verificacion` no es una capa del medallón: es el schema de lo que **no
    se deriva de los datos** sino que se observa fuera de ellos --el estado
    real de un negocio, buscado en línea por un agente--. Va aparte de `gold`
    porque `gold` se trunca y reconstruye a voluntad y esto no puede
    perderse con ella (`verificacion_cierre.md`).

    Nota de uso: `silver` y `gold` se califican siempre con la capa y el rol
    `etl_rw` corre con `search_path = ''` — ver `arquitectura.md` §2.3.1.
    """

    SILVER = "silver"
    GOLD = "gold"
    META = "meta"
    VERIFICACION = "verificacion"
