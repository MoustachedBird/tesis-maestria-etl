"""
Personnel strata (per_ocu) seed loader.

Seeds the `denue_estratos_personal` catalog from the `PersonalOcupado` enum.
The enum remains the single source of truth: rows are generated from
`get_range()` / `get_size_category()`, never duplicated by hand.
"""

import logging

from sqlalchemy import Connection, text

from src_etl.db.enums import PersonalOcupado

logger = logging.getLogger(__name__)

TABLE_NAME = "denue_estratos_personal"
DISPLAY_NAME = "personnel_strata"

TABLE_DDL = (
    "CREATE TABLE {table} ("
    "per_ocu VARCHAR PRIMARY KEY, "
    "empleados_min INTEGER NOT NULL, empleados_max INTEGER NOT NULL, "
    "categoria VARCHAR NOT NULL)"
)


def build_rows() -> list[dict]:
    """Generate catalog rows from the PersonalOcupado enum."""
    rows = []
    for estrato in PersonalOcupado:
        emp_min, emp_max = estrato.get_range()
        rows.append(
            {
                "per_ocu": estrato.value,
                "empleados_min": emp_min,
                "empleados_max": emp_max,
                "categoria": estrato.get_size_category(),
            }
        )
    return rows


def load_seeds_sqlalchemy(connection: Connection, truncate: bool = True) -> dict[str, int]:
    """
    Load personnel strata via SQLAlchemy connection (for Alembic migrations).

    Args:
        connection: SQLAlchemy connection
        truncate: If True, delete existing data first

    Returns:
        dict: Row count for the strata table
    """
    rows = build_rows()

    # The table may not exist yet when replaying migrations older than
    # bbe078d563ac (seed_catalog_tables also calls this loader from
    # dff9d0804542); creating it here keeps every replay point valid.
    table_exists = (
        connection.execute(
            text("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = :table"),
            {"table": TABLE_NAME},
        ).scalar()
        > 0
    )
    if not table_exists:
        connection.execute(text(TABLE_DDL.format(table=TABLE_NAME)))

    if truncate:
        connection.execute(text(f"DELETE FROM {TABLE_NAME}"))

    connection.execute(
        text(
            f"INSERT INTO {TABLE_NAME} (per_ocu, empleados_min, empleados_max, categoria) "
            "VALUES (:per_ocu, :empleados_min, :empleados_max, :categoria)"
        ),
        rows,
    )

    print(f"   Loaded {len(rows):,} personnel strata")
    return {DISPLAY_NAME: len(rows)}
