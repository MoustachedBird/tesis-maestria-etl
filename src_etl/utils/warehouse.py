from datetime import date
from pathlib import Path

import duckdb

from src_etl.core.config import settings


def connect_configured(
    db_path: Path | str,
    read_only: bool = False,
) -> duckdb.DuckDBPyConnection:
    """
    Conexion DuckDB con los limites de recursos de settings aplicados.

    Aplica DUCKDB_MEMORY_LIMIT y DUCKDB_THREADS (del .env) como settings de
    sesion. Sin esto, DuckDB usa sus defaults: todos los cores (bien) pero
    ~80% de la RAM del sistema, lo que viola el presupuesto de memoria en
    los UPDATEs paralelos sobre la tabla de ~77M filas (premisa: tiempo Y
    RAM). Usar este helper en TODA conexion a Silver/Gold en vez de
    duckdb.connect() directo.

    Args:
        db_path: Ruta al archivo .duckdb.
        read_only: Abrir en modo solo lectura.

    Returns:
        Conexion con memory_limit y threads configurados.
    """
    conn = duckdb.connect(str(db_path), read_only=read_only)
    conn.execute(f"SET memory_limit='{settings.DUCKDB_MEMORY_LIMIT}'")
    conn.execute(f"SET threads={settings.DUCKDB_THREADS}")
    return conn


def load_h3_extension(conn: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    """
    Carga la extension comunitaria h3 en una conexion (instala si falta).

    Punto unico para el patron LOAD/INSTALL: lo usan el backfill de celdas
    (h3_backfill) y el INSERT del ingest (EstablecimientoWriter).
    """
    try:
        conn.execute("LOAD h3")
    except duckdb.Error:
        conn.execute("INSTALL h3 FROM community; LOAD h3")
    return conn


def get_distinct_periods(
    conn: duckdb.DuckDBPyConnection,
    descending: bool = False,
) -> list[date]:
    """
    Periodos distintos de establecimientos, ordenados cronologicamente.

    Query unica compartida por los stages (antes existian 3 variantes:
    clee_resolver, date_normalizer y h3_backfill implementaban cada uno
    la suya).

    Args:
        conn: Conexion DuckDB abierta al Silver.
        descending: True = mas reciente primero (orden del resolver CLEE).

    Returns:
        Lista de dates de periodo.
    """
    order = "DESC" if descending else "ASC"
    rows = conn.execute(f"""
        SELECT DISTINCT period FROM establecimientos
        WHERE period IS NOT NULL
        ORDER BY period {order}
    """).fetchall()
    return [r[0] for r in rows]


class DuckDBWarehouse:
    def __init__(self, warehouse_path: Path):
        self.warehouse_path = warehouse_path

    def _get_connection(self):
        """Retorna una conexión a la base de datos masiva (limites de settings)."""
        return connect_configured(self.warehouse_path)
