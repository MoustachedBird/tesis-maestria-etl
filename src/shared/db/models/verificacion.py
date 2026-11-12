"""Lo que un agente observa FUERA de los datos: el estado real de un negocio.

`gold.negocios_clee_oficial.cerrado` es una hipótesis --"dejó de aparecer en
el DENUE, luego quebró"-- y este schema es donde se guarda la prueba: una
muestra de esos negocios, buscada en línea uno por uno, con el veredicto del
agente y la evidencia en que se apoyó. Cruzar `cerrado` contra
`veredictos.estado` es lo que dice si la regla de cierre se sostiene
(`verificacion_cierre.md`).

**Por qué es un schema y no tablas en `gold`.** Todo lo de `gold` es
derivable: se trunca con `--force` y se reconstruye desde Silver en unas
horas. Esto no. Cada veredicto costó búsquedas y tiempo de agente, y
rehacerlo no daría lo mismo --la web cambia--, así que tiene que sobrevivir
a cualquier reconstrucción de `gold`. Separarlo en su schema hace esa
frontera estructural: ningún `TRUNCATE gold.*` lo alcanza, y la
**escritura** es de otro actor (el agente, no el ETL), igual que
`silver.clee_<metodo>` separa lo que escribe cada método.

Y por lo mismo **no hay FK hacia `gold`**: una FK impediría truncar la
cohorte mientras exista un veredicto, que es justo lo contrario de lo que se
quiere. El join es lógico, por `clee`, y lo que la muestra necesita de la
fila de `gold` lo copia al momento de muestrear.

Cuatro tablas, del diseño al detalle:

    muestras            una tirada: cuántos, con qué semilla, bajo qué reglas
    muestra_negocios    los negocios que salieron, con la HIPÓTESIS congelada
    veredictos          lo que el agente concluyó de cada uno, y por qué
    evidencias          cada pieza en que se apoyó, con su URL y hacia dónde
                        apunta

**La hipótesis se congela en `muestra_negocios`**, y no contradice
`b0007`/`b0010` ("las tablas derivadas no guardan snapshots"): aquí el valor
que estaba en `gold` al muestrear ES el objeto de estudio. Si `gold` se
reconstruye con otro umbral, el veredicto del agente sigue siendo sobre el
negocio tal como se le presentó, y la comparación tiene que poder rehacerse
contra lo que se le presentó.
"""

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Double,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.db.base import Base
from src.shared.enums import Confianza, EstadoVerificado, Layer, SenalEvidencia

SCHEMA = Layer.VERIFICACION.value
"""Schema propio: lo escribe el agente, lo lee el análisis y el API."""


def _en(valores: type) -> str:
    """Lista SQL de los valores de un `StrEnum`, para un `CHECK (... IN (...))`.

    El `CHECK` sale del enum y no de una cadena escrita a mano para que no
    puedan divergir: agregar un valor allá lo admite aquí en la siguiente
    migración, y uno que no esté allá falla el `INSERT`.
    """
    return ", ".join(f"'{v.value}'" for v in valores)


class Muestra(Base):
    """Una tirada de la muestra: qué se le pidió al azar y bajo qué reglas.

    Existe para que la muestra sea **reproducible y citable**: con la semilla
    y el mismo `gold` se vuelve a sacar idéntica, y con `version_reglas`,
    `ventana_cortes` y `cierre_meses_ausente` queda dicho contra qué versión
    de la hipótesis se verificó. Dos tiradas con reglas distintas son dos
    filas, nunca una fila actualizada.
    """

    __tablename__ = "muestras"
    __table_args__ = (
        UniqueConstraint("nombre"),
        {"schema": SCHEMA},
    )

    muestra_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, comment="UUID de la tirada"
    )
    nombre: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Nombre con que se cita la muestra (p. ej. 'piloto-2026-11')",
    )
    creada_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    semilla: Mapped[float] = mapped_column(
        Double,
        nullable=False,
        comment=(
            "Argumento de setseed(), en [-1, 1]. Con ella y el mismo gold la muestra "
            "se vuelve a sacar identica, que es lo que la hace reproducible"
        ),
    )
    tamano_pedido: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Cuantos negocios se pidieron en total"
    )
    n_negocios: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment=(
            "Cuantos salieron de verdad. Menor que el pedido cuando un estrato tiene "
            "menos negocios que su cuota"
        ),
    )
    estratos: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        comment=(
            "Diseno del muestreo: {estrato: cuota}. Los estratos son los de "
            "verificacion_cierre.md (cerrado x intermitente x local reocupado); se "
            "guardan porque la muestra NO es aleatoria simple y las tasas se "
            "reponderan por estrato"
        ),
    )
    filtro: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment=(
            "El WHERE aplicado sobre gold.negocios_clee_oficial antes de muestrear "
            "(p. ej. excluir cluster_sospechoso y sin nombre). Es parte de la "
            "poblacion a la que se puede extrapolar"
        ),
    )
    version_reglas: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="gold.version_reglas al momento de muestrear"
    )
    ventana_cortes: Mapped[list[date]] = mapped_column(
        ARRAY(Date), nullable=False, comment="gold.ventana_cortes al momento de muestrear"
    )
    cierre_meses_ausente: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Umbral CIERRE_MESES_AUSENTE con que se calculo el `cerrado` muestreado",
    )

    def __repr__(self) -> str:
        return f"<Muestra({self.nombre}, n={self.n_negocios})>"


class MuestraNegocio(Base):
    """Un negocio que salió en la muestra, con la hipótesis tal como estaba.

    `cerrado_gold`, `meses_sin_aparecer_gold` y `huecos_gold` se copian de
    `gold` al muestrear y **no se actualizan**: son lo que se le presentó al
    agente y contra lo que se compara su veredicto. Si `gold` cambia después,
    la comparación sigue siendo sobre esto; la nueva versión de la regla se
    verifica con una muestra nueva.
    """

    __tablename__ = "muestra_negocios"
    __table_args__ = (
        # El analisis agrupa por estrato dentro de la muestra.
        Index("ix_muestra_negocios_estrato", "muestra_id", "estrato"),
        {"schema": SCHEMA},
    )

    muestra_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(f"{SCHEMA}.muestras.muestra_id", ondelete="CASCADE"),
        primary_key=True,
    )
    clee: Mapped[str] = mapped_column(
        String(50),
        primary_key=True,
        comment=(
            "El negocio, por su CLEE oficial. Join LOGICO con "
            "gold.negocios_clee_oficial.clee: sin FK, para que gold se pueda truncar"
        ),
    )
    orden: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Posicion en la muestra: el agente la recorre en este orden y se reanuda por aqui",
    )
    estrato: Mapped[str] = mapped_column(
        String(40), nullable=False, comment="Estrato del que salio, con el nombre de `estratos`"
    )
    cerrado_gold: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="La HIPOTESIS que se verifica: gold.cerrado al muestrear"
    )
    meses_sin_aparecer_gold: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="gold.meses_sin_aparecer al muestrear"
    )
    huecos_gold: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="gold.huecos al muestrear: >0 es un negocio que ya desaparecio y volvio",
    )

    def __repr__(self) -> str:
        return f"<MuestraNegocio({self.clee}, {self.estrato})>"


class Veredicto(Base):
    """Lo que el agente concluyó de UN negocio, y el razonamiento que lo sostiene.

    Puede haber más de un veredicto por negocio: otro agente, otra fecha, una
    segunda pasada con mejores fuentes. Por eso la llave es propia y no
    `(muestra_id, clee)`; cuál se usa en el análisis es una decisión de
    consulta (el más reciente, el de mayor confianza, el acuerdo entre dos).

    `razonamiento` es obligatorio: un `estado` sin el porqué no se puede
    auditar, y auditar al agente es la mitad del valor de esta tabla. Las
    piezas concretas en que se apoyó van en `evidencias`.
    """

    __tablename__ = "veredictos"
    __table_args__ = (
        ForeignKeyConstraint(
            ["muestra_id", "clee"],
            [f"{SCHEMA}.muestra_negocios.muestra_id", f"{SCHEMA}.muestra_negocios.clee"],
            ondelete="CASCADE",
        ),
        CheckConstraint(f"estado IN ({_en(EstadoVerificado)})", name="estado"),
        CheckConstraint(f"confianza IN ({_en(Confianza)})", name="confianza"),
        # El cruce con la hipotesis entra por aqui.
        Index("ix_veredictos_muestra_clee", "muestra_id", "clee"),
        Index("ix_veredictos_estado", "estado"),
        {"schema": SCHEMA},
    )

    veredicto_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    muestra_id: Mapped[str] = mapped_column(String(36), nullable=False)
    clee: Mapped[str] = mapped_column(String(50), nullable=False)
    estado: Mapped[str] = mapped_column(
        String(15),
        nullable=False,
        comment=(
            "activo | cerrado | indeterminado. INDETERMINADO es un resultado legitimo "
            "(sin huella en linea, o huella que no decide); forzarlo a uno de los otros "
            "dos contaminaria la medida de la regla con adivinanzas"
        ),
    )
    confianza: Mapped[str] = mapped_column(
        String(5),
        nullable=False,
        comment="alta | media | baja. Ordinal a proposito: un 0.83 de un LLM no mide nada",
    )
    fecha_cierre_estimada: Mapped[date | None] = mapped_column(
        Date,
        comment=(
            "Si la evidencia dice CUANDO cerro (ultima resena, aviso de cierre). Se "
            "compara con gold.ultimo_periodo: dice si el DENUE lo dejo de listar "
            "antes o despues de que cerrara"
        ),
    )
    razonamiento: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Por que el agente llego a ese estado, en prosa: lo que vuelve auditable la fila",
    )
    agente: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Quien verifico: modelo y version del agente (p. ej. 'claude-opus-5-5/v1')",
    )
    verificado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    n_busquedas: Mapped[int | None] = mapped_column(
        Integer, comment="Cuantas consultas hizo antes de concluir: el costo del veredicto"
    )
    duracion_s: Mapped[float | None] = mapped_column(Double, comment="Segundos que tardo")

    def __repr__(self) -> str:
        return f"<Veredicto({self.clee}, {self.estado}, {self.confianza})>"


class Evidencia(Base):
    """Una pieza concreta en que el agente se apoyó: de dónde, qué decía, hacia dónde apunta.

    Una fila por pieza y no un JSON en el veredicto, porque la evidencia es
    lo que se cuenta: "¿cuántos veredictos de cierre dependen solo de Google
    Maps?", "¿qué fuente contradice más seguido al DENUE?" son `GROUP BY`
    sobre esta tabla, y dentro de un JSON serían un desfile de operadores.

    `senal` se guarda por pieza porque la evidencia suele ser contradictoria
    --una ficha "cerrado permanentemente" y una publicación del mes pasado--
    y lo que vuelve auditable al agente es ver cómo pesó cada una.
    """

    __tablename__ = "evidencias"
    __table_args__ = (
        CheckConstraint(f"senal IN ({_en(SenalEvidencia)})", name="senal"),
        Index("ix_evidencias_veredicto", "veredicto_id"),
        Index("ix_evidencias_fuente", "fuente"),
        {"schema": SCHEMA},
    )

    evidencia_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    veredicto_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(f"{SCHEMA}.veredictos.veredicto_id", ondelete="CASCADE"),
        nullable=False,
    )
    fuente: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        comment=(
            "Tipo de fuente en minusculas: google_maps, facebook, instagram, sitio_web, "
            "directorio, noticia, marketplace, otro. Sin CHECK porque las fuentes "
            "crecen; el analisis las agrupa por este valor"
        ),
    )
    url: Mapped[str | None] = mapped_column(Text, comment="Donde se vio; NULL si no es citable")
    titulo: Mapped[str | None] = mapped_column(Text, comment="Titulo de la pagina o de la ficha")
    fecha_publicacion: Mapped[date | None] = mapped_column(
        Date,
        comment=(
            "Fecha de la publicacion, resena o aviso, si la pieza la trae. Es lo que "
            "permite situar la evidencia respecto a la ventana del DENUE"
        ),
    )
    extracto: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="El fragmento textual que sostiene la senal, citado y no parafraseado",
    )
    senal: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        comment="cerrado | activo | neutral: hacia donde apunta ESTA pieza por si sola",
    )
    capturada_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<Evidencia({self.fuente}, {self.senal})>"
