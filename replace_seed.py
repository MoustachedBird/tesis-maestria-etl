import polars as pl

# 1. Cargar la semilla actual
seed_path = "data/seeds/seed_entidades.parquet"
df = pl.read_parquet(seed_path)

# 2. Reemplazar MEXICO por ESTADO DE MÉXICO
df = df.with_columns(
    pl.col("entidad").replace("MÉXICO", "ESTADO DE MÉXICO")
)

# 3. Guardar los cambios
df.write_parquet(seed_path)
print("✅ seed_entidades.parquet actualizado correctamente.")