# Database Seeding Guide

This document explains how seed data is managed in the ETL Core project for DENUE catalog tables.

## Overview

The project uses **Alembic migrations** to load seed data from Parquet files into catalog tables. This approach ensures:

- ✅ **Version Control**: Seeds are tracked as part of migration history
- ✅ **Reproducibility**: All environments get identical catalog data
- ✅ **Atomicity**: Seeds load completely or not at all (transaction safety)
- ✅ **Automation**: CI/CD pipelines can run `alembic upgrade head`
- ✅ **Idempotency**: Re-running seed migrations produces consistent results

## Seed Files

Seed data is stored as Parquet files in `data/seeds/`:

| File | Table | Rows | Description |
|------|-------|------|-------------|
| `seed_entidades.parquet` | `denue_entidades` | 32 | Mexican states (entidades federativas) |
| `seed_municipios.parquet` | `denue_municipios` | 2,478 | Municipalities across all states |
| `seed_localidades.parquet` | `denue_localidades` | 39,140 | Localities within municipalities |
| `seed_actividades.parquet` | `denue_actividades_economicas` | 1,127 | SCIAN economic activity codes |

**Total**: 42,777 catalog records

## Architecture

### Components

1. **Seed Loader Module** (`src/db/seed_loader.py`)
   - Generic Parquet-to-database loading utilities
   - Handles both DuckDB and PostgreSQL backends
   - Provides validation and error handling
   - Uses Polars for efficient data reading

2. **Seed Migration** (`alembic/versions/dff9d0804542_seed_catalog_tables_with_initial_data.py`)
   - Alembic data migration that loads all seed files
   - Truncates existing data before loading (idempotent)
   - Loads seeds in correct dependency order
   - Includes detailed logging and progress tracking

### Loading Order

Seeds are loaded in hierarchical order to respect foreign key relationships:

```
1. denue_entidades (states)
   ↓
2. denue_municipios (municipalities)
   ↓
3. denue_localidades (localities)

4. denue_actividades_economicas (activities) - independent
```

### Truncate-and-Reload Strategy

The seed migration uses a **truncate-and-reload** strategy:

1. **Validate** seed files exist and are readable
2. **Truncate** all catalog tables (in reverse dependency order)
3. **Load** seed data from Parquet files
4. **Commit** transaction (or rollback on error)

This ensures:
- Idempotency: Running migration multiple times produces same result
- Clean state: No duplicate or stale data
- Atomicity: Either all seeds load or none do

## Usage

### Initial Setup

When setting up a fresh database:

```bash
# Run all migrations (creates schema + loads seeds)
alembic upgrade head

# OR use Makefile command
make db-migrate
```

### Re-seeding Data

To reload seed data (e.g., after seed files are updated):

```bash
# Using Makefile (recommended)
make db-seed

# OR manually
alembic downgrade cbc954c59583  # Remove seed data
alembic upgrade head             # Reload seed data
```

### Checking Status

```bash
# Show current migration state
make db-status

# OR
alembic current --verbose
```

### Rolling Back

```bash
# Remove seed data (keeps schema)
alembic downgrade cbc954c59583

# OR rollback last migration
make db-downgrade
```

### Complete Reset

```bash
# Drop everything and rebuild (DESTRUCTIVE)
make db-reset

# This will:
# 1. Drop all tables
# 2. Recreate schema
# 3. Load seed data
```

## Makefile Commands

The project includes convenient Makefile commands for database operations:

| Command | Description |
|---------|-------------|
| `make db-migrate` | Run pending migrations |
| `make db-downgrade` | Rollback last migration |
| `make db-seed` | Reload seed data (idempotent) |
| `make db-reset` | Full database reset (requires confirmation) |
| `make db-status` | Show migration status and history |

## Migration Details

### Upgrade Process

When running `alembic upgrade head`, the seed migration:

1. **Validates** all seed files:
   ```
   ✅ seed_entidades.parquet: 32 rows, 2 columns
   ✅ seed_municipios.parquet: 2,478 rows, 3 columns
   ✅ seed_localidades.parquet: 39,140 rows, 4 columns
   ✅ seed_actividades.parquet: 1,127 rows, 2 columns
   ```

2. **Clears** existing catalog data:
   ```
   ✅ Cleared denue_localidades
   ✅ Cleared denue_municipios
   ✅ Cleared denue_entidades
   ✅ Cleared denue_actividades_economicas
   ```

3. **Loads** seed data:
   ```
   🔄 Loading states...
      ✅ Loaded 32 rows
   
   🔄 Loading municipalities...
      ✅ Loaded 2,478 rows
   
   🔄 Loading localities...
      ✅ Loaded 39,140 rows
   
   🔄 Loading economic activities...
      ✅ Loaded 1,127 rows
   ```

4. **Reports** summary:
   ```
   📊 Summary:
      • States: 32 rows
      • Municipalities: 2,478 rows
      • Localities: 39,140 rows
      • Economic Activities: 1,127 rows
      • Total: 42,777 rows
   ```

### Downgrade Process

When running `alembic downgrade`, the seed migration:

1. **Clears** all catalog tables (in reverse dependency order)
2. **Preserves** table structures (schema remains intact)
3. **Does NOT affect** the `establecimientos` table

## Error Handling

The seed migration is designed to fail gracefully:

### Missing Seed Files

```
❌ Seed Loading Failed
   Error: Seed file not found: seed_entidades.parquet
   Rolling back migration...
```

### Corrupted Parquet Files

```
❌ Seed Loading Failed
   Error: Failed to read Parquet file seed_municipios.parquet
   Rolling back migration...
```

### Database Errors

```
❌ Seed Loading Failed
   Error: Failed to insert data into denue_entidades
   Rolling back migration...
```

All errors trigger a **rollback**, ensuring the database remains in a consistent state.

## Validation

The seed loader includes validation checks:

### Pre-Load Validation

- ✅ Seeds directory exists
- ✅ All required seed files present
- ✅ Parquet files are readable
- ✅ Files are not empty
- ✅ Files have expected columns

### Post-Load Validation

- ✅ Row counts match expectations
- ✅ No duplicate primary keys
- ✅ Transaction committed successfully

## Programmatic Access

You can use the seed loader programmatically in Python:

```python
from pathlib import Path
from sqlalchemy import create_engine
from src.db.seed_loader import seed_catalog_tables, validate_seed_data
from src.core.config import settings

# Validate seed files
validate_seed_data(settings.SEEDS_DIR)

# Load seeds
engine = create_engine(settings.DATABASE_URL)
with engine.begin() as connection:
    stats = seed_catalog_tables(connection, settings.SEEDS_DIR)
    print(f"Loaded {sum(stats.values())} total rows")
```

## Updating Seed Data

To update seed files:

1. **Generate new Parquet files** with updated data:
   ```python
   import polars as pl
   
   # Create updated seed data
   df = pl.DataFrame({
       "cve_ent": ["01", "02", ...],
       "entidad": ["Aguascalientes", "Baja California", ...]
   })
   
   # Save as Parquet
   df.write_parquet("data/seeds/seed_entidades.parquet")
   ```

2. **Re-run seed migration**:
   ```bash
   make db-seed
   ```

3. **Verify data loaded correctly**:
   ```bash
   make db-status
   ```

## CI/CD Integration

For continuous integration pipelines:

```yaml
# Example GitHub Actions workflow
- name: Setup Database
  run: |
    uv run alembic upgrade head

- name: Verify Seeds Loaded
  run: |
    uv run python -c "
    from src.db.database import get_engine
    engine = get_engine()
    with engine.connect() as conn:
        count = conn.execute('SELECT COUNT(*) FROM denue_entidades').scalar()
        assert count == 32, f'Expected 32 states, got {count}'
    "
```

## Best Practices

1. **Keep seeds small**: Catalog data only (not transactional data)
2. **Use Parquet format**: Efficient, schema-preserving, language-agnostic
3. **Version control seeds**: Commit seed files to Git
4. **Test migrations**: Verify upgrade/downgrade cycle works
5. **Document changes**: Update this file when modifying seed structure
6. **No manual transactions**: Let Alembic manage commits/rollbacks (see [Transaction Management](./TRANSACTION_MANAGEMENT.md))

## Troubleshooting

### "Module 'src' not found" error

This happens if you try to run `alembic history` before the environment is set up. The imports in the migration file are lazy-loaded inside functions, so this only affects the `history` command.

**Solution**: Run migrations normally with `alembic upgrade head` (imports work at runtime).

### Seed data missing after upgrade

Check the alembic version table matches:

```bash
make db-status
```

If version is wrong, the migration may not have completed. Re-run:

```bash
make db-seed
```

### Parquet file corruption

If seed files are corrupted:

1. Regenerate from source data
2. Replace files in `data/seeds/`
3. Re-run seed migration

## Performance

Loading 42,777 catalog records:

- **Read time**: ~50ms (Polars)
- **Insert time**: ~200ms (batch insert)
- **Total time**: ~250ms

The seed migration is very fast and suitable for frequent re-runs.

## Related Files

- `src/db/seed_loader.py` - Core seeding logic
- `alembic/versions/dff9d0804542_*.py` - Seed migration
- `src/core/config.py` - Settings (SEEDS_DIR path)
- `Makefile` - Convenient commands
- `data/seeds/*.parquet` - Seed data files

## See Also

- [Transaction Management](./TRANSACTION_MANAGEMENT.md) - How Alembic handles transactions
- [Seed Quick Start](./SEED_QUICKSTART.md) - Quick reference guide
- [Alembic Documentation](https://alembic.sqlalchemy.org/)
- [Polars Documentation](https://pola-rs.github.io/polars/)
- [AGENTS.md](../AGENTS.md) - Development guide
