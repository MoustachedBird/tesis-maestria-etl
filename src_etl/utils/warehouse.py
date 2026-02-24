from pathlib import Path

import duckdb


class BaseWarehouse:
    def __init__(self, warehouse_path: Path):
        self.warehouse_path = warehouse_path

    def _get_connection(self):
        """Retorna una conexión a la base de datos masiva."""
        return duckdb.connect(str(self.warehouse_path))
