from enum import StrEnum


class Section(StrEnum):
    """Secciones del pipeline seleccionables con el flag ``--only``.

    Los tokens atómicos mapean 1:1 a un paso; ``denue``, ``clee`` y
    ``permanencia`` son alias de grupo que se expanden a varias secciones
    atómicas (ver `SECTION_GROUPS`).

    Vive en `shared`, no en el CLI del ETL, porque el scheduler necesita
    nombrar qué sección corre en cada tick y el API las valida al encolar;
    ninguno de los dos debe importar del CLI.

    Los valores llevan guion (``clee-radio``) porque son exactamente lo que
    el usuario teclea en la línea de comandos.

    No hay secciones para la fecha ni para las celdas H3: el ingest las
    deriva antes del ``COPY`` (ver `transformers/enrich.py`), así que no son
    pasos ejecutables por separado.
    """

    ingest = "ingest"
    clee_radio = "clee-radio"
    clee_h3 = "clee-h3"
    clee_celda_h3_7 = "clee-celda-h3-7"
    permanencia_radio = "permanencia-radio"
    permanencia_h3 = "permanencia-h3"
    gold = "gold"
    sesnsp = "sesnsp"
    denue = "denue"  # group -> all DENUE sub-steps
    clee = "clee"  # group -> clee-radio + clee-h3 + clee-celda
    permanencia = "permanencia"  # group -> permanencia-radio + permanencia-h3


DENUE_ATOMIC: tuple[Section, ...] = (
    Section.ingest,
    Section.clee_radio,
    Section.clee_h3,
    Section.clee_celda_h3_7,
    Section.permanencia_radio,
    Section.permanencia_h3,
    Section.gold,
)
"""Secciones atómicas a las que se expande el alias de grupo ``denue``.

Excluye ``sesnsp`` a propósito: son fuentes distintas y ``--only denue`` debe
poder aislar una sin la otra.

``gold`` sí entra, y va al final: es la cohorte de consumo y se construye
sobre lo que el ingest acaba de escribir. No es un grupo porque hoy es una
sola tabla madre con su evidencia; si se construye la cohorte propagada
(`gold_cohorte_2021.md` §7) ahí sí habría que partirla en dos tokens.
"""

SECTION_GROUPS: dict[Section, tuple[Section, ...]] = {
    Section.denue: DENUE_ATOMIC,
    Section.clee: (Section.clee_radio, Section.clee_h3, Section.clee_celda_h3_7),
    Section.permanencia: (Section.permanencia_radio, Section.permanencia_h3),
}
"""Mapa de alias de grupo -> secciones atómicas que representa.

Un token que no aparezca aquí es atómico y se usa tal cual. Lo consume
``_expand_only()`` del CLI para traducir un ``--only`` a la lista de pasos
que realmente se ejecutan.
"""
