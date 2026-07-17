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


class DuckDBWarehouse:
    def __init__(self, warehouse_path: Path):
        self.warehouse_path = warehouse_path

    def _get_connection(self):
        """Retorna una conexión a la base de datos masiva (limites de settings)."""
        return connect_configured(self.warehouse_path)
