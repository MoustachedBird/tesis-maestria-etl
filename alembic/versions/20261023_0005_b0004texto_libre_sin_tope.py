"""Quita el tope de largo al texto libre de INEGI

El primer ingest contra el esquema Postgres fallo en los 20 cortes con
`value too long for type character varying(10)`. INEGI publica frases
enteras en campos que su diccionario describe como numero y letra:

    letra_ext:      "DOMICILIO CONOCIDO", "LOTE 01 Y 02 MANZANA",
                    "KILOMETRO 304+800", "MZ 259 LT 2503"
    letra_int:      "MANZANA 135 LOTE 2", "222 C CRUJIA 203 B"
    fecha_alta_str: "DICIEMBRE 2014"   (uno de los seis formatos que
                    `transformers/enrich.py` documenta, ~14.5M filas)

Un `varchar(n)` sobre un dato que no controlamos no valida nada: convierte
un valor sucio en el fallo de un COPY de 6M filas. Y en PostgreSQL `text` y
`varchar(n)` son identicos en almacenamiento y rendimiento, asi que el tope
no compraba nada a cambio.

Se ensanchan solo los campos de origen libre. El tope se conserva donde es
validacion real: los codigos que cruzan con los catalogos de geografia
(`cve_ent`, `cve_mun`, `cve_loc`) y `codigo_act`, que es FK al catalogo
SCIAN.

Revision ID: b0004texto_libre_sin_tope
Revises: b0003saca_clee_del_hecho
Create Date: 2026-10-23

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b0004texto_libre_sin_tope"
down_revision: str | Sequence[str] | None = "b0003saca_clee_del_hecho"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNAS = (
    "numero_ext",
    "letra_ext",
    "numero_int",
    "letra_int",
    "num_local",
    "cod_postal",
    "ageb",
    "manzana",
    "fecha_alta_str",
)
"""Campos que llegan como texto libre desde los CSV de INEGI."""


def upgrade() -> None:
    """Pasa las columnas a `text`.

    `ALTER TYPE` de `varchar(n)` a `text` no reescribe la tabla --es el
    mismo formato en disco-- asi que es instantaneo incluso con la tabla
    llena. En una tabla particionada hay que aplicarlo al padre: PostgreSQL
    lo propaga a las particiones.

    El import de `src` va aqui adentro: al correr el CLI de alembic el
    `sys.path` lo arma `env.py`.
    """
    from src.shared.enums import Layer

    tabla = f"{Layer.SILVER.value}.establecimientos"
    for columna in COLUMNAS:
        op.execute(f"ALTER TABLE {tabla} ALTER COLUMN {columna} TYPE text")


def downgrade() -> None:
    """No se estrecha de vuelta.

    Volver a `varchar(10)` requeriria truncar datos reales de INEGI, que es
    exactamente lo que esta revision existe para no hacer.
    """
    raise NotImplementedError("irreversible: estrechar truncaria datos de INEGI")
