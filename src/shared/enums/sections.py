from enum import StrEnum


class Section(StrEnum):
    """Secciones del pipeline seleccionables con el flag ``--only``.

    Los tokens atómicos mapean 1:1 a un paso; ``denue`` y ``clee`` son alias
    de grupo que se expanden a varias secciones atómicas. ``load`` solo
    aplica al comando ``pipeline`` (``transform`` y ``extract`` lo ignoran).

    Vive en `shared`, no en el CLI del ETL, porque el scheduler necesita
    nombrar qué sección corre en cada tick y el API las valida al encolar en
    `meta.run_requests`; ninguno de los dos debe importar del CLI.

    Los valores llevan guion (``normalize-dates``) porque son exactamente lo
    que el usuario teclea en la línea de comandos.

    No hay secciones para la fecha ni para las celdas H3: el ingest las
    deriva antes del ``COPY`` (ver `transformers/enrich.py`), así que no son
    pasos ejecutables por separado.
    """

    ingest = "ingest"
    clee_radio = "clee-radio"
    clee_h3 = "clee-h3"
    permanencia_radio = "permanencia-radio"
    permanencia_h3 = "permanencia-h3"
    sesnsp = "sesnsp"
    denue = "denue"  # group -> all DENUE sub-steps
    clee = "clee"  # group -> clee-radio + clee-h3
    permanencia = "permanencia"  # group -> permanencia-radio + permanencia-h3


DENUE_ATOMIC: tuple[Section, ...] = (
    Section.ingest,
    Section.clee_radio,
    Section.clee_h3,
    Section.permanencia_radio,
    Section.permanencia_h3,
)
"""Secciones atómicas a las que se expande el alias de grupo ``denue``.

Excluye ``sesnsp`` a propósito: son fuentes distintas y ``--only denue`` debe
poder aislar una sin la otra.
"""

SECTION_GROUPS: dict[Section, tuple[Section, ...]] = {
    Section.denue: DENUE_ATOMIC,
    Section.clee: (Section.clee_radio, Section.clee_h3),
    Section.permanencia: (Section.permanencia_radio, Section.permanencia_h3),
}
"""Mapa de alias de grupo -> secciones atómicas que representa.

Un token que no aparezca aquí es atómico y se usa tal cual. Lo consume
``_expand_only()`` del CLI para traducir un ``--only`` a la lista de pasos
que realmente se ejecutan.
"""
