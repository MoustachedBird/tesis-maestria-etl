from enum import StrEnum


class Section(StrEnum):
    """Secciones del pipeline seleccionables con el flag ``--only``.

    Los tokens atómicos mapean 1:1 a un paso; ``denue`` y ``clee`` son alias
    de grupo que se expanden a varias secciones atómicas. ``load`` solo
    aplica al comando ``pipeline`` (``transform`` y ``extract`` lo ignoran).

    Vive en `shared` —antes era un enum local de `src_etl/main.py`— porque el
    scheduler necesita nombrar qué sección corre en cada tick y el API las
    valida al encolar en `meta.run_requests`; ninguno de los dos debe
    importar del CLI del ETL.

    Los valores llevan guion (``normalize-dates``) porque son exactamente lo
    que el usuario teclea en la línea de comandos.

    Nota: ``normalize_dates`` y ``h3_cells`` desaparecen al migrar a
    PostgreSQL —el ``COPY`` normaliza la fecha y calcula las celdas H3 en
    vuelo, así que no quedan pasos que ejecutar (ver `arquitectura.md` §4.2
    y §4.4). Se conservan aquí mientras el pipeline siga sobre DuckDB.
    """

    ingest = "ingest"
    normalize_dates = "normalize-dates"
    h3_cells = "h3-cells"
    clee_radio = "clee-radio"
    clee_h3 = "clee-h3"
    sesnsp = "sesnsp"
    load = "load"
    denue = "denue"  # group -> all DENUE sub-steps
    clee = "clee"  # group -> clee-radio + clee-h3


DENUE_ATOMIC: tuple[Section, ...] = (
    Section.ingest,
    Section.normalize_dates,
    Section.h3_cells,
    Section.clee_radio,
    Section.clee_h3,
)
"""Secciones atómicas a las que se expande el alias de grupo ``denue``.

Excluye ``sesnsp`` a propósito: son fuentes distintas y ``--only denue`` debe
poder aislar una sin la otra.
"""

SECTION_GROUPS: dict[Section, tuple[Section, ...]] = {
    Section.denue: DENUE_ATOMIC,
    Section.clee: (Section.clee_radio, Section.clee_h3),
}
"""Mapa de alias de grupo -> secciones atómicas que representa.

Un token que no aparezca aquí es atómico y se usa tal cual. Lo consume
``_expand_only()`` del CLI para traducir un ``--only`` a la lista de pasos
que realmente se ejecutan.
"""
