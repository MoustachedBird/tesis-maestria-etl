"""
Pydantic Schemas for API Request/Response Validation.

This module contains all Pydantic models used for data validation
and serialization in FastAPI endpoints.
"""

from pydantic import BaseModel, Field, ConfigDict
from datetime import datetime
from typing import Optional


# ----------------------------------------------------------------------------
# Establecimiento Schemas
# ----------------------------------------------------------------------------
class EstablecimientoBase(BaseModel):
    """Base schema for Establecimiento with common fields."""

    nombre_establecimiento: str = Field(
        ..., max_length=500, description="Establishment name"
    )
    codigo_actividad: str = Field(
        ..., max_length=10, description="SCIAN economic activity code"
    )
    personal_ocupado: int = Field(default=0, ge=0, description="Number of employees")
    tamano_empresa: str = Field(..., max_length=20, description="Company size category")
    cve_entidad: str = Field(..., max_length=2, description="State code")
    entidad: str = Field(..., max_length=100, description="State name")
    municipio: Optional[str] = Field(
        None, max_length=100, description="Municipality name"
    )
    latitud: Optional[float] = Field(None, description="Latitude")
    longitud: Optional[float] = Field(None, description="Longitude")
    coords_validas: bool = Field(
        default=False, description="Coordinates validation flag"
    )
    year_carga: int = Field(..., description="Year of data load")


class EstablecimientoCreate(EstablecimientoBase):
    """Schema for creating a new Establecimiento."""

    clave: str = Field(..., max_length=50, description="Unique establishment key")


class EstablecimientoUpdate(BaseModel):
    """Schema for updating an Establecimiento (all fields optional)."""

    nombre_establecimiento: Optional[str] = Field(None, max_length=500)
    codigo_actividad: Optional[str] = Field(None, max_length=10)
    personal_ocupado: Optional[int] = Field(None, ge=0)
    tamano_empresa: Optional[str] = Field(None, max_length=20)
    cve_entidad: Optional[str] = Field(None, max_length=2)
    entidad: Optional[str] = Field(None, max_length=100)
    municipio: Optional[str] = Field(None, max_length=100)
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    coords_validas: Optional[bool] = None
    year_carga: Optional[int] = None


class EstablecimientoResponse(EstablecimientoBase):
    """Schema for Establecimiento response with all fields."""

    clave: str
    fecha_procesamiento: datetime

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

    cve_entidad: Optional[str] = Field(
        None, max_length=2, description="Filter by state code"
    )
    codigo_actividad: Optional[str] = Field(
        None, max_length=10, description="Filter by activity code"
    )
    year_carga: Optional[int] = Field(None, description="Filter by load year")
    municipio: Optional[str] = Field(
        None, max_length=100, description="Filter by municipality"
    )
    tamano_empresa: Optional[str] = Field(
        None, max_length=20, description="Filter by company size"
    )
