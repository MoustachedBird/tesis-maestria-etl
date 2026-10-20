"""Registro de corridas, tareas y linaje del pipeline.

El unico punto de entrada es `MetadataTracker`, que escribe en el schema
`meta`. Las entidades las define el ORM en `shared/db/models/meta.py`; los
estados, los enums de `shared/enums` --ambos compartidos con el resto del
proyecto, que es por lo que no viven aqui.
"""

from src.meta.tracker import MetadataTracker

__all__ = ["MetadataTracker"]
