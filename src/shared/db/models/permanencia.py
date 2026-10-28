"""Permanencia de cada establecimiento en el directorio — una tabla por método.

El grano cambia respecto a `silver.establecimientos`, y esa es toda la razón
de ser de estas tablas:

    silver.establecimientos    82.9M   una fila cada vez que INEGI lo vio
    silver.permanencia_<m>     ~10M    una fila por establecimiento

Cada fila responde "¿desde cuándo y hasta cuándo estuvo este establecimiento
en el directorio?", que es lo que el modelo de supervivencia necesita y lo
que la tabla de hechos no puede dar sin un `GROUP BY` de minutos sobre 82.9M
filas unidas a la tabla de identidades.

**No guardan el veredicto de cierre, solo el tiempo observado.** Eso es
deliberado: el protocolo de la tesis define la regla de cierre como
*preliminar* --"se considerará cerrado el establecimiento que deja de
aparecer en dos cortes consecutivos"-- y la validación externa puede
cambiarla. Guardar `cerrado: bool` obligaría a reconstruir 10M de filas con
cada ajuste de la regla; guardar los ingredientes (`idx_primer`,
`idx_ultimo`, `cortes_observados`, `huecos`, `cortes_tras_ultimo`) permite
aplicar cualquier regla aguas abajo sin tocar Silver.

Una tabla por método y no una con columna `metodo`, por el mismo motivo que
`silver.clee_radio` y `silver.clee_h3`: el método va en el `FROM`, así que
una consulta no puede mezclarlos por descuido. Con una sola tabla, un
`WHERE metodo = 'radio'` olvidado devuelve dos filas por establecimiento y
los agregados salen al doble, en silencio y sin error.
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Double,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from src.shared.db.base import Base
from src.shared.enums import Layer

SCHEMA = Layer.SILVER.value
"""Derivadas de Silver y consumidas por el análisis: siguen siendo Silver.

La capa de consumo congelada y fechada es Gold (Fase 5), que aplicará la
regla de cierre sobre el método elegido.
"""


class PermanenciaMixin:
    """Núcleo común a toda tabla de permanencia.

    Se declara una vez para que agregar un método (p. ej. `permanencia_llm`)
    sea una tabla y una migración, sin repetir treinta columnas.
    """

    # ===== IDENTIDAD =====
    clee_resuelto: Mapped[str] = mapped_column(
        String(50), primary_key=True, comment="Identidad resuelta por este metodo"
    )
    es_sintetica: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="True si la identidad es NEW_* (sin pareja encontrada)"
    )

    # ===== VENTANA OBSERVADA =====
    # Las fechas para reportar; los indices para calcular. Los cortes del
    # DENUE distan entre 4 y 12 meses, asi que el protocolo mide el tiempo
    # en PERIODOS entre cortes y no en dias: `idx_*` es la posicion en la
    # secuencia ordenada de cortes (0 = el mas antiguo).
    primer_periodo: Mapped[date] = mapped_column(
        Date, nullable=False, comment="Primer corte en que se observo"
    )
    ultimo_periodo: Mapped[date] = mapped_column(
        Date, nullable=False, comment="Ultimo corte en que se observo"
    )
    idx_primer: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Posicion del primer corte en la secuencia (0-based)"
    )
    idx_ultimo: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Posicion del ultimo corte en la secuencia"
    )
    cortes_observados: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="En cuantos cortes distintos aparecio"
    )
    huecos: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment=(
            "Cortes dentro de su ventana en que NO aparecio: "
            "(idx_ultimo - idx_primer + 1) - cortes_observados. "
            "Mayor que 0 = desaparecio y volvio"
        ),
    )
    cortes_tras_ultimo: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment=(
            "Cortes publicados despues del ultimo en que aparecio. "
            "0 = presente en el corte final (censura por la derecha); "
            "es el campo sobre el que se aplica la regla de cierre"
        ),
    )
    fecha_alta_date: Mapped[date | None] = mapped_column(
        Date, comment="Fecha de alta en el RENEM mas antigua observada (no inicio de operaciones)"
    )

    # ===== ALIAS Y VARIANTES =====
    # No son adorno: son control de calidad de la resolucion de identidad.
    # Un "establecimiento" que cambio de municipio Y de actividad es casi
    # seguro una fusion de dos distintos. Y varias de estas columnas sirven
    # como variables del modelo, no solo como diagnostico.
    nombres: Mapped[list[str] | None] = mapped_column(
        ARRAY(String), comment="Nombres distintos observados a lo largo de su ventana"
    )
    n_nombres: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Cuantos nombres distintos tuvo"
    )
    razones_sociales: Mapped[list[str] | None] = mapped_column(
        ARRAY(String), comment="Razones sociales distintas observadas"
    )
    codigos_act: Mapped[list[str] | None] = mapped_column(
        ARRAY(String), comment="Codigos SCIAN distintos observados"
    )
    cambio_actividad: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="True si cambio de actividad economica entre cortes"
    )
    municipios: Mapped[list[str] | None] = mapped_column(
        ARRAY(String), comment="Municipios distintos observados, como cve_ent||cve_mun"
    )
    cambio_municipio: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment="True si cambio de municipio: la senal mas fuerte de fusion indebida",
    )
    cod_postales: Mapped[list[str] | None] = mapped_column(
        ARRAY(String), comment="Codigos postales distintos observados"
    )
    desplazamiento_m: Mapped[float | None] = mapped_column(
        Double,
        comment=(
            "Diagonal del rectangulo que contiene todas sus coordenadas, en metros. "
            "Cota SUPERIOR de cuanto se movio, no la distancia exacta: se calcula "
            "con cuatro agregados (min/max de lat y lon) en vez de comparar todos "
            "los pares de observaciones"
        ),
    )

    # ===== REPRESENTANTE (del corte mas reciente) =====
    # Los atributos con que el modelo describe al establecimiento. Se toman
    # del corte mas reciente en que se observo: es el estado con que
    # "termino", y para un establecimiento que cerro es el ultimo conocido.
    nom_estab: Mapped[str | None] = mapped_column(String, comment="Nombre en el corte mas reciente")
    raz_social: Mapped[str | None] = mapped_column(
        String, comment="Razon social en el corte mas reciente"
    )
    codigo_act: Mapped[str | None] = mapped_column(
        String(10), comment="SCIAN en el corte mas reciente"
    )
    cve_ent: Mapped[str | None] = mapped_column(
        String(2), comment="Entidad en el corte mas reciente"
    )
    cve_mun: Mapped[str | None] = mapped_column(
        String(3), comment="Municipio en el corte mas reciente"
    )
    cod_postal: Mapped[str | None] = mapped_column(
        String, comment="Codigo postal en el corte mas reciente"
    )
    latitud: Mapped[float | None] = mapped_column(Double, comment="Latitud del corte mas reciente")
    longitud: Mapped[float | None] = mapped_column(
        Double, comment="Longitud del corte mas reciente"
    )
    h3_cell_9: Mapped[int | None] = mapped_column(
        BigInteger, comment="Celda H3 res 9 del corte mas reciente (int64, no cabe en Integer)"
    )

    construida_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="Cuando se construyo esta fila: la tabla es derivada y se reconstruye entera",
    )

    @declared_attr.directive
    def __table_args__(cls) -> tuple:
        """Índices del acceso real, iguales para todo método.

        No se particiona: el grano es el establecimiento, que **cruza** los
        periodos, así que no hay clave de partición natural. Son ~10M filas
        angostas y el acceso es por filtros de atributos, no por corte.
        """
        tabla = cls.__tablename__
        return (
            # El analisis de supervivencia filtra y estratifica por aqui.
            Index(f"ix_{tabla}_ventana", "idx_primer", "idx_ultimo"),
            Index(f"ix_{tabla}_censura", "cortes_tras_ultimo"),
            Index(f"ix_{tabla}_geo", "cve_ent", "cve_mun"),
            Index(f"ix_{tabla}_actividad", "codigo_act"),
            # Separa lo que INEGI identifico de lo que el pipeline infirio.
            Index(f"ix_{tabla}_sintetica", "es_sintetica"),
            {"schema": SCHEMA},
        )


class PermanenciaRadio(PermanenciaMixin, Base):
    """Permanencia según la identidad resuelta por radio/BallTree.

    El método de referencia de la tesis: es de esta tabla de la que sale la
    unidad de análisis.
    """

    __tablename__ = "permanencia_radio"

    def __repr__(self) -> str:
        return (
            f"<PermanenciaRadio({self.clee_resuelto}, "
            f"{self.primer_periodo}..{self.ultimo_periodo}, n={self.cortes_observados})>"
        )


class PermanenciaH3(PermanenciaMixin, Base):
    """Permanencia según la identidad resuelta por blocking H3.

    Pista de comparación: se mide contra `permanencia_radio` para saber si el
    blocking reproduce la misma ventana de vida.
    """

    __tablename__ = "permanencia_h3"

    def __repr__(self) -> str:
        return (
            f"<PermanenciaH3({self.clee_resuelto}, "
            f"{self.primer_periodo}..{self.ultimo_periodo}, n={self.cortes_observados})>"
        )
