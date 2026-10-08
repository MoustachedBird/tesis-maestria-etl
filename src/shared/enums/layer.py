from enum import StrEnum


class Layer(StrEnum):
    """Capas del warehouse, una por schema de PostgreSQL.

    Compartido entre el ETL (que escribe `silver` y deriva `gold`), el API
    (que solo lee `gold`) y el tracker de metadatos (que etiqueta cada tarea
    con la capa que tocó). Vive en `shared` porque los tres lo necesitan sin
    tomar una dependencia cruzada entre módulos.

    El valor es literalmente el nombre del schema: se usa para calificar
    tablas, en los `GRANT` por capa y en `pg_dump -n`. Cambiar un valor
    renombra un schema, así que no son etiquetas libres.

    Nota de uso: `silver` y `gold` tienen una tabla `establecimientos` cada
    una, con **granos distintos** (observación por periodo vs negocio
    resuelto). Por eso los nombres se califican siempre con la capa y el rol
    `etl_rw` corre con `search_path = ''` — ver `arquitectura.md` §2.3.1.
    """

    SILVER = "silver"
    GOLD = "gold"
    META = "meta"
