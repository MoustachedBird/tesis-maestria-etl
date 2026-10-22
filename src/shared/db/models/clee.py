"""Resultado de la resolución de identidad CLEE — una tabla por método.

Viven en el schema `silver` pero en su propio módulo, porque la frontera que
describen es de escritura: `silver.establecimientos` la escribe **solo** el
ingest, y cada tabla de aquí la escribe **solo** su método de resolución.
Ningún proceso toca la tabla de otro.

Una tabla por método y no columnas de una misma tabla (decidido 2026-10-22,
ver `docs/separacion_ingest_clee.md`):

- **Aislamiento por construcción.** Con columnas, re-resolver un método
  exigiría construir la partición nueva acarreando los valores del otro: un
  read-modify-write donde un bug en el acarreo pisa al método que no se
  estaba tocando.
- **La evidencia difiere.** Radio tiene distancia y score, el blocking H3
  tiene celda y anillo, un método por LLM tendría modelo y confianza. En una
  sola tabla eso es ancho lleno de NULLs y pérdida de procedencia, que es
  justo lo que la comparación de métodos tiene que poder defender.
- **Agregar un método es aditivo**: una tabla y una migración, sin `ALTER`
  sobre una particionada ni reconstruir lo que ya existe.

Las columnas de evidencia se agregan junto con el código que las llena, no
antes: hoy `_best_candidate` calcula el score y lo descarta, así que
declararlo ahora sería una columna condenada a NULL.
"""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from src.shared.db.base import Base
from src.shared.enums import Layer

SCHEMA = Layer.SILVER.value
"""Las tablas de resolución son parte de Silver: son derivadas, no consumo."""


class ResolucionCleeMixin:
    """Núcleo común a toda tabla de resolución: la identidad asignada.

    Se declara una vez aquí para que `clee_radio`, `clee_h3` y las que
    vengan no repitan la llave ni el particionado. Cada tabla concreta
    agrega sus columnas de evidencia y nada más.

    No es una tabla: es un mixin sin `__tablename__`, así que SQLAlchemy no
    lo mapea ni crea DDL para él.
    """

    id: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        comment="FK lógica a silver.establecimientos.id (hash del contenido de la fila)",
    )

    # Parte de la PK porque PostgreSQL exige la clave de particion dentro de
    # toda PK/UNIQUE. `id` ya es unico por si solo --`period` es uno de los
    # cinco componentes de su hash-- asi que esto no desambigua nada: existe
    # para poder particionar, que es lo que da el reemplazo atomico por
    # ATTACH PARTITION. Y sin `period` el join con establecimientos no puede
    # ser partition-wise: seria un hash join de 82.9M x 82.9M en vez de 20
    # joins de ~4M. Ver docs/separacion_ingest_clee.md §3.
    period: Mapped[date] = mapped_column(
        Date, primary_key=True, comment="Corte al que pertenece (clave de particion)"
    )

    clee_resuelto: Mapped[str] = mapped_column(
        String,
        nullable=False,
        comment="Identidad asignada a la observacion por este metodo",
    )

    origen: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        comment="Valor de OrigenClee: DIRECTO, PROPAGADO o NUEVO",
    )

    # Fecha de la corrida que produjo la fila. Es lo que permite citar un
    # resultado congelado en la tesis: sin ella, re-resolver cambia los
    # numeros en silencio y no queda rastro de cuando se midio.
    resuelto_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    @declared_attr.directive
    def __table_args__(cls) -> tuple:
        """Índices y particionado, iguales para toda tabla de resolución.

        `clee_resuelto` lleva índice porque la consulta que define la unidad
        de análisis agrupa por identidad (`GROUP BY clee_resuelto` para
        derivar la ventana de vida de cada negocio).

        `period` NO lleva índice: es la clave de partición, así que dentro de
        cada partición es una columna constante — 20 árboles que mantener en
        cada escritura masiva para cero selectividad. Mismo criterio que
        `silver.establecimientos`.

        Particionado por LIST y no RANGE porque cada corte del DENUE es una
        fecha discreta: da exactamente una partición por corte, que es la
        unidad de recuperación de `arquitectura.md` §2.4.
        """
        tabla = cls.__tablename__
        return (
            Index(f"ix_{tabla}_clee_resuelto", "clee_resuelto"),
            Index(f"ix_{tabla}_origen", "origen"),
            {"schema": SCHEMA, "postgresql_partition_by": "LIST (period)"},
        )


class CleeRadio(ResolucionCleeMixin, Base):
    """Resolución por radio geográfico + similitud difusa (BallTree).

    El método de referencia de la tesis: busca candidatos dentro de
    `CLEE_SEARCH_RADIUS_M` y elige el de score más alto.
    """

    __tablename__ = "clee_radio"

    def __repr__(self) -> str:
        return f"<CleeRadio({self.id}, {self.period}, {self.origen})>"


class CleeH3(ResolucionCleeMixin, Base):
    """Resolución por blocking H3 — pista de comparación contra `clee_radio`.

    Mismo contrato, otro mecanismo de generación de candidatos: vecinos de la
    celda H3 en vez de un radio métrico. Existe para medir si el blocking
    compra tiempo sin perder calidad, así que convive con la otra y nunca la
    sustituye.
    """

    __tablename__ = "clee_h3"

    def __repr__(self) -> str:
        return f"<CleeH3({self.id}, {self.period}, {self.origen})>"
