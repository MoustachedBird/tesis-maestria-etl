Procedimiento (archivo nuevo)

# 1. Respaldar el Silver actual (renombrar, no borrar — política del proyecto)
mv data/silver/denue.duckdb data/silver/denue_backup_pre_rebuild.duckdb

# 2. Crear esquema completo + seeds en el archivo nuevo (verificado en limpio)
make db-migrate

# 3. Ingest + normalize-dates + h3-cells de TODOS los periodos (sin CLEE aún)
make transform ARGS="--force --no-clee-resolution"

# 4. SESNSP (siembra sesnsp_delitos + hecho mensual)
make transform-sesnsp ARGS="--force"

# 5. Resolución CLEE — la etapa larga; ahora sí cubrirá los periodos 2015–2020
make clee-resolution-radio ARGS="--force"

# 6. Dimensión N3 (siempre después de CLEE)
make build-entidades

# 7. Opcional: Silver -> Gold
make load
