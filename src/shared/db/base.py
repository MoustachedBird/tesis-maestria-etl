"""
SQLAlchemy Base y configuración del MetaData.

Provee la base declarativa de todos los modelos ORM. El `MetaData` lleva el
schema por defecto y la convención de nombres de constraints, que son las dos
cosas que deben ser idénticas en toda la base.
"""

from sqlalchemy import DDL, MetaData, event
from sqlalchemy.orm import DeclarativeBase

from src.shared.enums import Layer

# Consistent naming convention for constraints
# This ensures that constraint names are predictable and follow best practices
naming_convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

DEFAULT_SCHEMA = Layer.SILVER.value
"""Schema por defecto de los modelos que no declaran uno.

`silver` porque es donde vive la mayoría de las tablas (hechos y catálogos).
Las de `gold` y `meta` lo declaran explícitamente con
`__table_args__ = {"schema": ...}`, que gana sobre este default.

Ponerlo en el MetaData y no en cada modelo tiene una consecuencia práctica
que conviene conocer: las `ForeignKeyConstraint` escritas sin calificar
(p. ej. `["denue_entidades.cve_ent"]`) se resuelven dentro de este schema,
así que no hay que reescribir las ~20 FKs de silver. Las FKs que cruzan de
un schema a otro SÍ deben calificarse.

Efecto secundario a tener presente: las claves de `Base.metadata.tables`
quedan calificadas — `"silver.establecimientos"`, no `"establecimientos"`.
"""

SCHEMAS = tuple(layer.value for layer in Layer)
"""Los tres schemas de la base, en el orden en que se crean.

Derivado de `Layer` para que no puedan divergir: añadir una capa allá la
crea aquí.
"""


class Base(DeclarativeBase):
    """
    Base class for all SQLAlchemy ORM models.

    This uses SQLAlchemy 2.0 declarative style with proper metadata configuration.
    All models should inherit from this class.

    Example:
        class MyModel(Base):
            __tablename__ = "my_table"
            id = Column(Integer, primary_key=True)
    """

    metadata = MetaData(schema=DEFAULT_SCHEMA, naming_convention=naming_convention)

    # Type annotation for proper type checking
    __tablename__: str


def tables_for(schema: str) -> list:
    """
    Objetos `Table` de una capa, en el orden en que los declara el modelo.

    Para `create_all(tables=...)` cuando se quiere UNA capa y no todas:
    los tests del esquema de datos montan solo `silver`, sin arrastrar las
    tablas de control. Que la lista salga del metadata y no esté escrita a
    mano evita que diverja del modelo.

    Args:
        schema: Nombre del schema (usar `Layer.SILVER.value` y similares).

    Returns:
        Lista de `Table` de ese schema, en orden de declaración.
    """
    return [t for t in Base.metadata.sorted_tables if t.schema == schema]


def get_table(name: str, schema: str = DEFAULT_SCHEMA):
    """
    Objeto `Table` del metadata, aceptando el nombre sin calificar.

    Existe porque las claves de `Base.metadata.tables` llevan el schema
    (`"silver.establecimientos"`), mientras el código que las consulta
    maneja nombres de negocio (`"establecimientos"`, los seeds, las tablas
    gestionadas por `indexes.py`). Centralizar la calificación aquí evita
    que cada call site concatene el schema a mano y se equivoque de capa.

    Args:
        name: Nombre de la tabla, con o sin schema.
        schema: Schema donde buscarla si `name` viene sin calificar.

    Returns:
        El objeto `Table` correspondiente.

    Raises:
        KeyError: Si la tabla no está registrada en el metadata, con la
            lista de las disponibles — el error por defecto solo dice la
            clave que falta, que es poco útil cuando el problema es un
            modelo que no se importó.
    """
    key = name if "." in name else f"{schema}.{name}"
    try:
        return Base.metadata.tables[key]
    except KeyError:
        available = ", ".join(sorted(Base.metadata.tables))
        raise KeyError(
            f"tabla '{key}' no esta en el metadata del ORM. Disponibles: {available}"
        ) from None


# -----------------------------------------------------------------------------
# Creación automática de los schemas en `create_all`
# -----------------------------------------------------------------------------
# SQLAlchemy emite `CREATE TABLE silver.establecimientos` pero NO crea el
# schema: sin esto, cualquier `Base.metadata.create_all()` falla con "schema
# does not exist". En producción los schemas los crea la migración baseline
# de Alembic; este listener cubre los tests y el uso en desarrollo, donde se
# arma un esquema desechable con `create_all` sobre una base vacía.
#
for _schema in SCHEMAS:
    event.listen(
        Base.metadata,
        "before_create",
        DDL(f"CREATE SCHEMA IF NOT EXISTS {_schema}"),
    )
