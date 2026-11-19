"""El expediente, los informes de los especialistas y la auditoria del juez

El agente pasa de uno que busca con herramientas a un EQUIPO: por cada
fuente (Maps, buscador, redes, sitio propio) una recoleccion base igual para
todos los negocios y un especialista que la lee; un decisor que integra los
informes; y un juez que audita el veredicto contra el expediente crudo.
Hacen falta tres tablas y una columna:

    verificacion.consultas          el EXPEDIENTE: cada pieza que se trajo,
                                    citada o no, incluidas las que fallaron
    verificacion.informes           lo que el especialista de cada fuente
                                    concluyo de ella
    verificacion.auditorias         el juez: su estado a ciegas y si las citas
                                    del decisor sostienen el veredicto
    verificacion.evidencias.pieza_id   que pieza del expediente cita la
                                    evidencia (NULL en los veredictos v1-v3,
                                    que no guardaban lo visto)

Igual que `b0010`, NO tira antes de crear: lo que hay en `verificacion` no
es derivable. `checkfirst=True` y `ADD COLUMN IF NOT EXISTS` vuelven la
revision idempotente.

Los GRANT de `api_ro` no hacen falta: `b0010` dejo DEFAULT PRIVILEGES en el
schema, asi que las tablas nuevas nacen legibles.

Revision ID: b0011expediente_y_juez
Revises: b0010verificacion_agente
Create Date: 2026-11-18

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b0011expediente_y_juez"
down_revision: str | Sequence[str] | None = "b0010verificacion_agente"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLAS_NUEVAS = ("consultas", "informes", "auditorias")
"""Las que crea esta revision; el resto del schema ya existe desde `b0010`."""

COMENTARIOS = {
    "consultas": (
        "El expediente de un veredicto: cada pieza que el recolector trajo (Maps, buscador, "
        "redes, sitio propio), citada o no, con el texto exacto que vio el modelo"
    ),
    "informes": (
        "Lo que el especialista de una fuente concluyo de ella: si es este negocio, que "
        "indica y con que piezas. Uno por veredicto y fuente"
    ),
    "auditorias": (
        "Lo que el juez dijo de un veredicto: su propio estado sin verlo, y si las piezas "
        "citadas lo sostienen. Una por veredicto y juez"
    ),
}
"""El grano de cada tabla nueva, en el COMMENT."""


def upgrade() -> None:
    """Crea `consultas`, `informes` y `auditorias`, y agrega `evidencias.pieza_id`."""
    from src.shared.db.base import Base
    from src.shared.db.models import verificacion  # noqa: F401  (puebla el metadata)
    from src.shared.enums import Layer

    schema = Layer.VERIFICACION.value
    conn = op.get_bind()
    tablas = [Base.metadata.tables[f"{schema}.{nombre}"] for nombre in TABLAS_NUEVAS]
    Base.metadata.create_all(bind=conn, tables=tablas, checkfirst=True)
    for tabla, comentario in COMENTARIOS.items():
        op.execute(f"COMMENT ON TABLE {schema}.{tabla} IS '{comentario}'")

    # La columna va despues de crear `consultas`, que es a donde apunta. La FK
    # lleva el nombre que le da la convencion de `base.py`, el mismo que tendria
    # en una base creada desde cero por `b0010` (que ya ve la columna en el modelo).
    op.execute(f"ALTER TABLE {schema}.evidencias ADD COLUMN IF NOT EXISTS pieza_id VARCHAR(36)")
    op.execute(f"""
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_evidencias_pieza_id_consultas'
            ) THEN
                ALTER TABLE {schema}.evidencias
                    ADD CONSTRAINT fk_evidencias_pieza_id_consultas
                    FOREIGN KEY (pieza_id) REFERENCES {schema}.consultas (pieza_id)
                    ON DELETE CASCADE;
            END IF;
        END $$
    """)
    print(f"   {schema}: consultas, informes y auditorias creadas; evidencias.pieza_id agregada")


def downgrade() -> None:
    """Quita la columna y las tres tablas.

    Lo que tengan dentro no es derivable (un expediente es lo que la web
    decia ese dia), asi que bajar de aqui se hace a sabiendas.
    """
    from src.shared.enums import Layer

    schema = Layer.VERIFICACION.value
    op.execute(f"ALTER TABLE {schema}.evidencias DROP COLUMN IF EXISTS pieza_id")
    op.execute(f"DROP TABLE IF EXISTS {schema}.auditorias")
    op.execute(f"DROP TABLE IF EXISTS {schema}.informes")
    op.execute(f"DROP TABLE IF EXISTS {schema}.consultas")
