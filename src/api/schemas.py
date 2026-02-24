"""
Pydantic Schemas for API Request/Response Validation.

This module contains all Pydantic models used for data validation
and serialization in FastAPI endpoints.

Field names match the SQLAlchemy ORM models in src/db/models.py.
"""

from pydantic import BaseModel, Field, ConfigDict
from datetime import date
from typing import Optional


# ----------------------------------------------------------------------------
# Establecimiento Schemas
# ----------------------------------------------------------------------------
class EstablecimientoBase(BaseModel):
    """Base schema for Establecimiento with common fields."""

    nom_estab: Optional[str] = Field(
        None, max_length=500, description="Establishment name"
    )
    codigo_act: Optional[str] = Field(
        None, max_length=10, description="SCIAN economic activity code"
    )
    per_ocu: Optional[str] = Field(
        None, max_length=50, description="Personnel occupied (employee count range)"
    )
    tipounieco: Optional[str] = Field(
        None, max_length=20, description="Economic unit type: FIJO, SEMIFIJO, NINGUNO"
    )
    cve_ent: Optional[str] = Field(None, max_length=2, description="State code")
    cve_mun: Optional[str] = Field(None, max_length=3, description="Municipality code")
    cve_loc: Optional[str] = Field(None, max_length=4, description="Locality code")
    cod_postal: Optional[str] = Field(
        None, max_length=5, description="5-digit postal code"
    )
    latitud: Optional[float] = Field(None, description="Latitude (WGS84)")
    longitud: Optional[float] = Field(None, description="Longitude (WGS84)")
    period: Optional[date] = Field(None, description="Data period/snapshot date")


class EstablecimientoCreate(EstablecimientoBase):
    """Schema for creating a new Establecimiento."""

    id: str = Field(..., description="Unique establishment identifier (hash)")


class EstablecimientoUpdate(BaseModel):
    """Schema for updating an Establecimiento (all fields optional)."""

    nom_estab: Optional[str] = Field(None, max_length=500)
    codigo_act: Optional[str] = Field(None, max_length=10)
    per_ocu: Optional[str] = Field(None, max_length=50)
    tipounieco: Optional[str] = Field(None, max_length=20)
    cve_ent: Optional[str] = Field(None, max_length=2)
    cve_mun: Optional[str] = Field(None, max_length=3)
    cve_loc: Optional[str] = Field(None, max_length=4)
    cod_postal: Optional[str] = Field(None, max_length=5)
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    period: Optional[date] = None


class EstablecimientoResponse(EstablecimientoBase):
    """Schema for Establecimiento response with all fields."""

    id: str
    clee: Optional[str] = None
    raz_social: Optional[str] = None
    tipo_vial: Optional[str] = None
    nom_vial: Optional[str] = None
    numero_ext: Optional[str] = None
    tipo_asent: Optional[str] = None
    nomb_asent: Optional[str] = None
    telefono: Optional[str] = None
    correoelec: Optional[str] = None
    www: Optional[str] = None
    fecha_alta: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# ----------------------------------------------------------------------------
# Actividad Economica Schemas
# ----------------------------------------------------------------------------
class ActividadEconomicaBase(BaseModel):
    """Base schema for Actividad Economica."""

    codigo_act: str = Field(..., max_length=10, description="SCIAN activity code")
    nombre_act: str = Field(..., max_length=500, description="Activity description")


class ActividadEconomicaCreate(ActividadEconomicaBase):
    """Schema for creating a new Actividad Economica."""

    pass


class ActividadEconomicaResponse(ActividadEconomicaBase):
    """Schema for Actividad Economica response."""

    model_config = ConfigDict(from_attributes=True)


# ----------------------------------------------------------------------------
# Entidad Schemas
# ----------------------------------------------------------------------------
class EntidadBase(BaseModel):
    """Base schema for Entidad (State)."""

    cve_ent: str = Field(..., max_length=2, description="State code")
    entidad: str = Field(..., max_length=100, description="State name")


class EntidadCreate(EntidadBase):
    """Schema for creating a new Entidad."""

    pass


class EntidadResponse(EntidadBase):
    """Schema for Entidad response."""

    model_config = ConfigDict(from_attributes=True)


# ----------------------------------------------------------------------------
# Municipio Schemas
# ----------------------------------------------------------------------------
class MunicipioBase(BaseModel):
    """Base schema for Municipio (Municipality)."""

    cve_ent: str = Field(..., max_length=2, description="State code")
    cve_mun: str = Field(..., max_length=3, description="Municipality code")
    municipio: str = Field(..., max_length=100, description="Municipality name")


class MunicipioCreate(MunicipioBase):
    """Schema for creating a new Municipio."""

    pass


class MunicipioResponse(MunicipioBase):
    """Schema for Municipio response."""

    model_config = ConfigDict(from_attributes=True)


# ----------------------------------------------------------------------------
# Localidad Schemas
# ----------------------------------------------------------------------------
class LocalidadBase(BaseModel):
    """Base schema for Localidad (Locality)."""

    cve_ent: str = Field(..., max_length=2, description="State code")
    cve_mun: str = Field(..., max_length=3, description="Municipality code")
    cve_loc: str = Field(..., max_length=4, description="Locality code")
    localidad: str = Field(..., max_length=100, description="Locality name")


class LocalidadCreate(LocalidadBase):
    """Schema for creating a new Localidad."""

    pass


class LocalidadResponse(LocalidadBase):
    """Schema for Localidad response."""

    model_config = ConfigDict(from_attributes=True)


# ----------------------------------------------------------------------------
# Pagination and Filter Schemas
# ----------------------------------------------------------------------------
class PaginationParams(BaseModel):
    """Schema for pagination parameters."""

    skip: int = Field(default=0, ge=0, description="Number of records to skip")
    limit: int = Field(
        default=100, ge=1, le=1000, description="Maximum number of records to return"
    )


class EstablecimientoFilter(BaseModel):
    """Schema for filtering Establecimientos."""

    cve_ent: Optional[str] = Field(
        None, max_length=2, description="Filter by state code"
    )
    codigo_act: Optional[str] = Field(
        None, max_length=10, description="Filter by activity code"
    )
    period: Optional[date] = Field(None, description="Filter by data period")
    cve_mun: Optional[str] = Field(
        None, max_length=3, description="Filter by municipality code"
    )
    per_ocu: Optional[str] = Field(
        None, max_length=50, description="Filter by employee range"
    )
