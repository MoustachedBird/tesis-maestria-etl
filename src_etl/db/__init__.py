"""Utilidades de base de datos propias del ETL.

Solo queda la gestión de índices (`indexes.py`), que es cosa de las
escrituras masivas y no la necesita ni el API ni el scheduler. El modelo
ORM, la sesión y la Base viven en `src.shared.db`.
"""
