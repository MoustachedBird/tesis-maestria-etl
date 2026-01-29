"""Initial migration with DENUE models

Revision ID: cd78919bad45
Revises:
Create Date: 2026-01-28 22:55:39.368615

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "cd78919bad45"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create initial DENUE database schema."""

    # Create Entidades (States) table
    op.create_table(
        "entidades",
        sa.Column("cve_ent", sa.String(length=2), nullable=False, comment="State code"),
        sa.Column(
            "entidad", sa.String(length=100), nullable=False, comment="State name"
        ),
        sa.PrimaryKeyConstraint("cve_ent", name=op.f("pk_entidades")),
    )

    # Create Actividades Economicas table
    op.create_table(
        "actividades_economicas",
        sa.Column(
            "codigo_act",
            sa.String(length=10),
            nullable=False,
            comment="SCIAN activity code",
        ),
        sa.Column(
            "nombre_act",
            sa.String(length=500),
            nullable=False,
            comment="Activity description",
        ),
        sa.PrimaryKeyConstraint(
            "codigo_act", "nombre_act", name=op.f("pk_actividades_economicas")
        ),
    )

    # Create Municipios table
    op.create_table(
        "municipios",
        sa.Column("cve_ent", sa.String(length=2), nullable=False, comment="State code"),
        sa.Column(
            "cve_mun", sa.String(length=3), nullable=False, comment="Municipality code"
        ),
        sa.Column(
            "municipio",
            sa.String(length=100),
            nullable=False,
            comment="Municipality name",
        ),
        sa.PrimaryKeyConstraint("cve_ent", "cve_mun", name=op.f("pk_municipios")),
    )
    op.create_index(
        op.f("ix_municipio_nombre"), "municipios", ["municipio"], unique=False
    )

    # Create Localidades table
    op.create_table(
        "localidades",
        sa.Column("cve_ent", sa.String(length=2), nullable=False, comment="State code"),
        sa.Column(
            "cve_mun", sa.String(length=3), nullable=False, comment="Municipality code"
        ),
        sa.Column(
            "cve_loc", sa.String(length=4), nullable=False, comment="Locality code"
        ),
        sa.Column(
            "localidad", sa.String(length=100), nullable=False, comment="Locality name"
        ),
        sa.PrimaryKeyConstraint(
            "cve_ent", "cve_mun", "cve_loc", name=op.f("pk_localidades")
        ),
    )
    op.create_index(
        op.f("ix_localidad_nombre"), "localidades", ["localidad"], unique=False
    )

    # Create Establecimientos table
    op.create_table(
        "establecimientos",
        sa.Column(
            "clave",
            sa.String(length=50),
            nullable=False,
            comment="Unique establishment key",
        ),
        sa.Column(
            "nombre_establecimiento",
            sa.String(length=500),
            nullable=False,
            comment="Establishment name",
        ),
        sa.Column(
            "codigo_actividad",
            sa.String(length=10),
            nullable=False,
            comment="SCIAN economic activity code",
        ),
        sa.Column(
            "personal_ocupado",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Number of employees",
        ),
        sa.Column(
            "tamano_empresa",
            sa.String(length=20),
            nullable=False,
            comment="Company size category",
        ),
        sa.Column(
            "cve_entidad",
            sa.String(length=2),
            nullable=False,
            comment="State code (entidad federativa)",
        ),
        sa.Column(
            "entidad", sa.String(length=100), nullable=False, comment="State name"
        ),
        sa.Column(
            "municipio",
            sa.String(length=100),
            nullable=True,
            comment="Municipality name",
        ),
        sa.Column("latitud", sa.Float(), nullable=True, comment="Latitude"),
        sa.Column("longitud", sa.Float(), nullable=True, comment="Longitude"),
        sa.Column(
            "coords_validas",
            sa.Boolean(),
            nullable=False,
            server_default="false",
            comment="Coordinates validation flag",
        ),
        sa.Column(
            "year_carga", sa.Integer(), nullable=False, comment="Year of data load"
        ),
        sa.Column(
            "fecha_procesamiento",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Processing timestamp",
        ),
        sa.CheckConstraint(
            "personal_ocupado >= 0",
            name=op.f("ck_establecimientos_personal_ocupado_positive"),
        ),
        sa.PrimaryKeyConstraint("clave", name=op.f("pk_establecimientos")),
    )

    # Create indexes for common queries on Establecimientos
    op.create_index(
        op.f("ix_establecimiento_activity_year"),
        "establecimientos",
        ["codigo_actividad", "year_carga"],
        unique=False,
    )
    op.create_index(
        op.f("ix_establecimiento_location"),
        "establecimientos",
        ["cve_entidad", "municipio"],
        unique=False,
    )


def downgrade() -> None:
    """Drop all DENUE tables."""

    # Drop tables in reverse order
    op.drop_index(op.f("ix_establecimiento_location"), table_name="establecimientos")
    op.drop_index(
        op.f("ix_establecimiento_activity_year"), table_name="establecimientos"
    )
    op.drop_table("establecimientos")

    op.drop_index(op.f("ix_localidad_nombre"), table_name="localidades")
    op.drop_table("localidades")

    op.drop_index(op.f("ix_municipio_nombre"), table_name="municipios")
    op.drop_table("municipios")

    op.drop_table("actividades_economicas")
    op.drop_table("entidades")
