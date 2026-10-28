"""Modelos ORM, un módulo por capa del warehouse.

Se importan **por módulo**, no plano:

    from src.shared.db.models import silver
    silver.Establecimiento        # 82.9M observaciones (negocio, periodo)

    from src.shared.db.models import clee
    clee.CleeRadio                # identidad resuelta, una tabla por método

    from src.shared.db.models import gold
    gold.Establecimiento          # 10.45M negocios resueltos

Esa es la razón de partirlo así: las dos capas tienen una tabla
`establecimientos` con el MISMO nombre y **grano distinto**, y escribir
`silver.Establecimiento` hace imposible confundirlas al leer el código. Es
la misma salvaguarda de `arquitectura.md` §2.3.1 aplicada a Python, en vez
de depender de dos clases con nombres parecidos.

Las clases de `silver` también se re-exportan planas por compatibilidad con
los imports que ya existían (`from src.shared.db.models import
Establecimiento`). Ese re-export se retira cuando `gold` tenga su propia
`Establecimiento` (Fase 5), porque ahí el nombre pasa a ser ambiguo.
"""

from src.shared.db.models import clee, gold, meta, permanencia, silver
from src.shared.db.models.clee import CleeH3, CleeRadio
from src.shared.db.models.permanencia import PermanenciaH3, PermanenciaRadio
from src.shared.db.models.silver import (
    ActividadEconomica,
    Entidad,
    Establecimiento,
    EstratoPersonal,
    Localidad,
    Municipio,
    SesnspDelito,
    SesnspIncidenciaMunicipal,
    TipoAsentamiento,
    TipoUnidadEconomica,
    TipoVialidad,
)

__all__ = [
    # Módulos por capa — la forma preferida de importar
    "clee",
    "gold",
    "permanencia",
    "meta",
    "silver",
    # Re-export plano de silver (compatibilidad; se retira en la Fase 5)
    "ActividadEconomica",
    "CleeH3",
    "CleeRadio",
    "PermanenciaH3",
    "PermanenciaRadio",
    "Entidad",
    "Establecimiento",
    "EstratoPersonal",
    "Localidad",
    "Municipio",
    "SesnspDelito",
    "SesnspIncidenciaMunicipal",
    "TipoAsentamiento",
    "TipoUnidadEconomica",
    "TipoVialidad",
]
