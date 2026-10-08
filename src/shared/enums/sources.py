from enum import StrEnum


class SourceSystem(StrEnum):
    """Fuentes de datos externas que alimenta el pipeline.

    Compartido entre el ETL (un extractor y un transformer por fuente), el
    tracker de metadatos (etiqueta cada corrida con su origen) y el API
    (reporta frescura por fuente). Vive en `shared` porque hoy la fuente es
    implícita —se deduce del nombre del job, p. ej. `"denue_downloader"`— y
    eso obliga a parsear strings para saber qué se descargó.

    Las dos tienen cadencias muy distintas, que es la razón de que el cron
    semanal sea un "revisar si hay algo nuevo" y no un "procesar":
    DENUE publica 1–2 cortes al año, SESNSP uno al mes.
    """

    DENUE = "denue"
    SESNSP = "sesnsp"
