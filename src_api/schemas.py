"""
Pydantic Schemas for API Request/Response Validation.

This module contains all Pydantic models used for data validation
and serialization in FastAPI endpoints.

Field names match the SQLAlchemy ORM models in src/db/models.py.
"""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


# ----------------------------------------------------------------------------
# Establecimiento Schemas
# ----------------------------------------------------------------------------
class EstablecimientoBase(BaseModel):
    """Base schema for Establecimiento with common fields."""

    nom_estab: str | None = Field(None, max_length=500, description="Establishment name")
    codigo_act: str | None = Field(None, max_length=10, description="SCIAN economic activity code")
    per_ocu: str | None = Field(
        None, max_length=50, description="Personnel occupied (employee count range)"
    )
    tipounieco: str | None = Field(
        None, max_length=20, description="Economic unit type: FIJO, SEMIFIJO, NINGUNO"
    )
    cve_ent: str | None = Field(None, max_length=2, description="State code")
    cve_mun: str | None = Field(None, max_length=3, description="Municipality code")
    cve_loc: str | None = Field(None, max_length=4, description="Locality code")
    cod_postal: str | None = Field(None, max_length=5, description="5-digit postal code")
    latitud: float | None = Field(None, description="Latitude (WGS84)")
    longitud: float | None = Field(None, description="Longitude (WGS84)")
    period: date | None = Field(None, description="Data period/snapshot date")


class EstablecimientoCreate(EstablecimientoBase):
    """Schema for creating a new Establecimiento."""

    id: str = Field(..., description="Unique establishment identifier (hash)")


class EstablecimientoUpdate(BaseModel):
    """Schema for updating an Establecimiento (all fields optional)."""

    nom_estab: str | None = Field(None, max_length=500)
    codigo_act: str | None = Field(None, max_length=10)
    per_ocu: str | None = Field(None, max_length=50)
    tipounieco: str | None = Field(None, max_length=20)
    cve_ent: str | None = Field(None, max_length=2)
    cve_mun: str | None = Field(None, max_length=3)
    cve_loc: str | None = Field(None, max_length=4)
    cod_postal: str | None = Field(None, max_length=5)
    latitud: float | None = None
    longitud: float | None = None
    period: date | None = None


class EstablecimientoResponse(EstablecimientoBase):
    """Schema for Establecimiento response with all fields."""

    id: str
    clee: str | None = None
    raz_social: str | None = None
    tipo_vial: str | None = None
    nom_vial: str | None = None
    numero_ext: str | None = None
    tipo_asent: str | None = None
    nomb_asent: str | None = None
    telefono: str | None = None
    correoelec: str | None = None
    www: str | None = None
    fecha_alta_str: str | None = None
    fecha_alta_date: date | None = None

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

    cve_ent: str | None = Field(None, max_length=2, description="Filter by state code")
    codigo_act: str | None = Field(None, max_length=10, description="Filter by activity code")
    period: date | None = Field(None, description="Filter by data period")
    cve_mun: str | None = Field(None, max_length=3, description="Filter by municipality code")
    per_ocu: str | None = Field(None, max_length=50, description="Filter by employee range")
