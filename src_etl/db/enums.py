"""
DENUE Database ENUM Definitions.

This module contains all ENUM type definitions used in the DENUE database schema.
These correspond to the official INEGI catalog values and are stored as ENUMs in DuckDB.
"""

from enum import Enum


class TipoVial(str, Enum):
    """
    Road/Street Type Classifications (33 values).

    Official INEGI catalog for types of thoroughfares and streets.
    """

    NINGUNO = "NINGUNO"
    SIN_REFERENCIA = "SIN REFERENCIA"
    ANDADOR = "ANDADOR"
    AMPLIACION = "AMPLIACION"
    AVENIDA = "AVENIDA"
    BOULEVARD = "BOULEVARD"
    CALLE = "CALLE"
    CALLEJON = "CALLEJON"
    CALZADA = "CALZADA"
    CAMINO = "CAMINO"
    CARRETERA = "CARRETERA"
    CERRADA = "CERRADA"
    CIRCUITO = "CIRCUITO"
    CIRCUNVALACION = "CIRCUNVALACION"
    CONTINUACION = "CONTINUACION"
    CORREDOR = "CORREDOR"
    DIAGONAL = "DIAGONAL"
    EJE_VIAL = "EJE VIAL"
    PASAJE = "PASAJE"
    PEATONAL = "PEATONAL"
    PERIFERICO = "PERIFERICO"
    PLAZA = "PLAZA"
    PRIVADA = "PRIVADA"
    PROLONGACION = "PROLONGACION"
    RETORNO = "RETORNO"
    TERRACERIA = "TERRACERIA"
    VIADUCTO = "VIADUCTO"
    AUTOPISTA = "AUTOPISTA"
    BRECHA = "BRECHA"
    VEREDA = "VEREDA"
    LIBRAMIENTO = "LIBRAMIENTO"
    COSTERA = "COSTERA"
    OTRA = "OTRA (ESPECIFICAR)"


class TipoAsentamiento(str, Enum):
    """
    Settlement Type Classifications (45 values).

    Official INEGI catalog for types of human settlements and zones.
    """

    NINGUNO = "NINGUNO"
    AEROPUERTO = "AEROPUERTO"
    AMPLIACION = "AMPLIACION"
    BARRIO = "BARRIO"
    CANTON = "CANTON"
    CIUDAD = "CIUDAD"
    CIUDAD_INDUSTRIAL = "CIUDAD INDUSTRIAL"
    COLONIA = "COLONIA"
    CONDOMINIO = "CONDOMINIO"
    CONJUNTO_HABITACIONAL = "CONJUNTO HABITACIONAL"
    CORREDOR_INDUSTRIAL = "CORREDOR INDUSTRIAL"
    COTO = "COTO"
    CUARTEL = "CUARTEL"
    EJIDO = "EJIDO"
    EXHACIENDA = "EXHACIENDA"
    FRACCION = "FRACCION"
    FRACCIONAMIENTO = "FRACCIONAMIENTO"
    GRANJA = "GRANJA"
    HACIENDA = "HACIENDA"
    INGENIO = "INGENIO"
    MANZANA = "MANZANA"
    PARAJE = "PARAJE"
    PARQUE_INDUSTRIAL = "PARQUE INDUSTRIAL"
    PRIVADA = "PRIVADA"
    PROLONGACION = "PROLONGACION"
    PUEBLO = "PUEBLO"
    PUERTO = "PUERTO"
    RANCHO = "RANCHO"
    RANCHERIA = "RANCHERIA"
    REGION = "REGION"
    RESIDENCIAL = "RESIDENCIAL"
    RINCONADA = "RINCONADA"
    SECCION = "SECCION"
    SECTOR = "SECTOR"
    SUPERMANZANA = "SUPERMANZANA"
    UNIDAD = "UNIDAD"
    UNIDAD_HABITACIONAL = "UNIDAD HABITACIONAL"
    VILLA = "VILLA"
    ZONA_FEDERAL = "ZONA FEDERAL"
    ZONA_INDUSTRIAL = "ZONA INDUSTRIAL"
    ZONA_MILITAR = "ZONA MILITAR"
    ZONA_NAVAL = "ZONA NAVAL"
    LOCALIDAD = "LOCALIDAD"
    PARQUE = "PARQUE"
    OTRA = "OTRA (ESPECIFICAR)"


class TipoUnidadEconomica(str, Enum):
    """
    Economic Unit Type (3 values).

    Classification of establishment permanence type.
    """

    NINGUNO = "NINGUNO"
    FIJO = "FIJO"  # Fixed/Permanent establishment
    SEMIFIJO = "SEMIFIJO"  # Semi-fixed/Temporary establishment


class PersonalOcupado(str, Enum):
    """
    Employee Count Ranges (7 values).

    Standard employee size categories used by INEGI for business classification.
    """

    NINGUNO = "NINGUNO"
    PERSONAS_0_5 = "0 A 5 PERSONAS"  # Micro: 0-5 employees
    PERSONAS_6_10 = "6 A 10 PERSONAS"  # Micro: 6-10 employees
    PERSONAS_11_30 = "11 A 30 PERSONAS"  # Small: 11-30 employees
    PERSONAS_31_50 = "31 A 50 PERSONAS"  # Small: 31-50 employees
    PERSONAS_51_100 = "51 A 100 PERSONAS"  # Medium: 51-100 employees
    PERSONAS_101_250 = "101 A 250 PERSONAS"  # Medium: 101-250 employees
    PERSONAS_251_MAS = "251 Y MAS PERSONAS"  # Large: 251+ employees

    def get_range(self) -> tuple[int, int]:
        """
        Convert employee ENUM to numeric range (min, max).

        Returns:
            Tuple of (minimum, maximum) employee count

        Examples:
            >>> PersonalOcupado.PERSONAS_11_30.get_range()
            (11, 30)
            >>> PersonalOcupado.PERSONAS_251_MAS.get_range()
            (251, 999999)
        """
        mapping = {
            "NINGUNO": (0, 0),
            "0 A 5 PERSONAS": (0, 5),
            "6 A 10 PERSONAS": (6, 10),
            "11 A 30 PERSONAS": (11, 30),
            "31 A 50 PERSONAS": (31, 50),
            "51 A 100 PERSONAS": (51, 100),
            "101 A 250 PERSONAS": (101, 250),
            "251 Y MAS PERSONAS": (251, 999999),
        }
        return mapping.get(self.value, (0, 0))

    def get_size_category(self) -> str:
        """
        Get business size category based on employee count.

        Returns:
            Business size: "Micro", "Small", "Medium", or "Large"

        Examples:
            >>> PersonalOcupado.PERSONAS_0_5.get_size_category()
            'Micro'
            >>> PersonalOcupado.PERSONAS_251_MAS.get_size_category()
            'Large'
        """
        if self in (PersonalOcupado.PERSONAS_0_5, PersonalOcupado.PERSONAS_6_10):
            return "Micro"
        elif self in (PersonalOcupado.PERSONAS_11_30, PersonalOcupado.PERSONAS_31_50):
            return "Small"
        elif self in (
            PersonalOcupado.PERSONAS_51_100,
            PersonalOcupado.PERSONAS_101_250,
        ):
            return "Medium"
        elif self == PersonalOcupado.PERSONAS_251_MAS:
            return "Large"
        return "Unknown"
