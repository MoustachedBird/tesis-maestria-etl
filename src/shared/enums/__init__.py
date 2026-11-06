"""Enums de dominio compartidos entre el ETL, el API, el scheduler y meta.

Punto único de importación: `from src.shared.enums import JobStatus, Layer`.
Cada módulo documenta en su docstring de clase por qué el enum vive aquí y
no en el paquete que lo usa — la regla es que si más de un paquete lo
necesita, va en `shared` para que ninguno dependa del otro.
"""

from src.shared.enums.clee import OrigenClee
from src.shared.enums.jobs import ErrorType, JobStatus, TaskStatus, TriggerType
from src.shared.enums.layer import Layer
from src.shared.enums.sections import DENUE_ATOMIC, SECTION_GROUPS, Section
from src.shared.enums.sources import SourceSystem

__all__ = [
    "DENUE_ATOMIC",
    "SECTION_GROUPS",
    "ErrorType",
    "JobStatus",
    "Layer",
    "OrigenClee",
    "Section",
    "SourceSystem",
    "TaskStatus",
    "TriggerType",
]
