# Seed Data Quick Reference

## TL;DR

Seed data is automatically loaded via Alembic migrations. Just run:

```bash
alembic upgrade head
```

or

```bash
make db-migrate
```

## Common Tasks

### Initial Setup (Fresh Database)
```bash
make db-migrate
```

### Reload Seeds (After Updating Seed Files)
```bash
make db-seed
```

### Check What's Loaded
```bash
make db-status
```

### Verify Data
```python
uv run python -c "
import duckdb
from src.core.config import settings

conn = duckdb.connect(str(settings.DUCKDB_PATH))
print('States:', conn.execute('SELECT COUNT(*) FROM denue_entidades').fetchone()[0])
print('Municipalities:', conn.execute('SELECT COUNT(*) FROM denue_municipios').fetchone()[0])
print('Localities:', conn.execute('SELECT COUNT(*) FROM denue_localidades').fetchone()[0])
print('Activities:', conn.execute('SELECT COUNT(*) FROM denue_actividades_economicas').fetchone()[0])
conn.close()
"
```

Expected output:
```
States: 32
Municipalities: 2478
Localities: 39140
Activities: 1127
```

## Seed Files Location

```
data/seeds/
├── seed_entidades.parquet     (32 rows)
├── seed_municipios.parquet    (2,478 rows)
├── seed_localidades.parquet   (39,140 rows)
└── seed_actividades.parquet   (1,127 rows)
```

## Key Files

- **Seed Loader**: `src/db/seed_loader.py`
- **Seed Migration**: `alembic/versions/dff9d0804542_seed_catalog_tables_with_initial_data.py`
- **Full Documentation**: `docs/SEEDING.md`

## Troubleshooting

**Problem**: Seeds not loading

```bash
# Check migration status
make db-status

# Force re-seed
make db-seed
```

**Problem**: Old data still present

```bash
# Seeds use truncate-and-reload strategy
# Just re-run:
make db-seed
```

## See Full Documentation

For complete details, see [docs/SEEDING.md](./SEEDING.md)
