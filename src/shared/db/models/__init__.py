"""Modelos ORM, un módulo por capa del warehouse.

Se importan **por módulo**, no plano:

    from src.shared.db.models import silver
    silver.Establecimiento        # 82.9M observaciones (negocio, periodo)

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

from src.shared.db.models import gold, meta, silver
from src.shared.db.models.silver import (
    ActividadEconomica,
    Entidad,
    EntidadEconomica,
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
    "gold",
    "meta",
    "silver",
    # Re-export plano de silver (compatibilidad; se retira en la Fase 5)
    "ActividadEconomica",
    "Entidad",
    "EntidadEconomica",
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
