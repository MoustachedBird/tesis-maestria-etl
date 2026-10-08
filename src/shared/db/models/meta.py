"""Modelos ORM de la capa `meta` — pendiente de implementar.

El tracking de corridas existe hoy como 451 líneas de DDL en strings
(`src/meta/schema.py`), sin ORM ni migraciones: es el diagnóstico 1.3 de
`arquitectura.md`. Este módulo lo reemplaza con modelos reales en el schema
`meta` de la misma base, para que el historial pueda hacer JOIN con los
datos que describe.

Tablas que vendrán: `job_runs`, `download_tasks`, `process_tasks`,
`normalize_tasks`, `clee_resolution_tasks`, `data_lineage`, más las dos
nuevas del scheduler (`schedules`, `run_requests`, §2.5).
"""
