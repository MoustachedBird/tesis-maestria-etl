# Database Setup Guide

This document explains the database architecture and setup for the ETL Core project.

## Architecture Overview

The project uses **SQLAlchemy 2.0** with **Alembic** for migrations and supports two database backends:

- **DuckDB** (default): File-based OLAP database for analytics
- **PostgreSQL**: Traditional RDBMS for production deployments

The database layer is fully abstracted, allowing you to switch backends by changing a single environment variable.

## Project Structure

```
src/
├── core/
│   └── config.py          # Settings with database configuration
├── db/
│   ├── __init__.py        # Database module exports
│   ├── base.py            # SQLAlchemy declarative base
│   ├── database.py        # Engine and session management
│   ├── models.py          # ORM models
│   └── dependencies.py    # FastAPI dependencies
└── api/
    ├── main.py            # FastAPI application
    └── schemas.py         # Pydantic schemas

alembic/
├── env.py                 # Alembic environment configuration
└── versions/              # Migration files
```

## Configuration

### Environment Variables

Copy `.env.example` to `.env` and configure:

```bash
# Database backend: "duckdb" or "postgresql"
DATABASE_BACKEND=duckdb

# DuckDB Configuration (when DATABASE_BACKEND=duckdb)
DUCKDB_PATH=data/gold/denue.duckdb
DUCKDB_THREADS=8
DUCKDB_MEMORY_LIMIT=16GB

# PostgreSQL Configuration (when DATABASE_BACKEND=postgresql)
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=denue
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_password

# SQLAlchemy Configuration
SQLALCHEMY_ECHO=false
SQLALCHEMY_POOL_SIZE=5
SQLALCHEMY_MAX_OVERFLOW=10
SQLALCHEMY_POOL_PRE_PING=true
```

### Switching Databases

To switch from DuckDB to PostgreSQL:

1. Update `.env`:
   ```bash
   DATABASE_BACKEND=postgresql
   POSTGRES_USER=your_user
   POSTGRES_PASSWORD=your_password
   ```

2. Run migrations:
   ```bash
   uv run alembic upgrade head
   ```

## Database Models

The project includes the following ORM models:

### Core Models

1. **Establecimiento** - Business establishments
   - Primary key: `clave` (establishment unique ID)
   - Includes: name, activity code, size, location, coordinates
   - Indexes on: activity+year, location

2. **ActividadEconomica** - SCIAN economic activity catalog
   - Composite primary key: `(codigo_act, nombre_act)`

3. **Entidad** - Mexican states catalog
   - Primary key: `cve_ent` (state code)

4. **Municipio** - Municipalities catalog
   - Composite primary key: `(cve_ent, cve_mun)`

5. **Localidad** - Localities catalog
   - Composite primary key: `(cve_ent, cve_mun, cve_loc)`

## Migrations with Alembic

### Initial Setup

```bash
# Run initial migration to create tables
uv run alembic upgrade head
```

### Creating New Migrations

**Note:** DuckDB doesn't support Alembic's autogenerate feature. For DuckDB, create migrations manually.

For PostgreSQL:
```bash
# Ensure DATABASE_BACKEND=postgresql in .env
uv run alembic revision --autogenerate -m "Description of changes"
```

For DuckDB:
```bash
# Create empty migration
uv run alembic revision -m "Description of changes"

# Edit the migration file manually
# See alembic/versions/ for examples
```

### Applying Migrations

```bash
# Upgrade to latest
uv run alembic upgrade head

# Upgrade/downgrade to specific revision
uv run alembic upgrade <revision>
uv run alembic downgrade <revision>

# Show current revision
uv run alembic current

# Show migration history
uv run alembic history
```

## Using the Database in Code

### In FastAPI Endpoints

```python
from fastapi import APIRouter
from src.db.dependencies import DbSession
from src.db.models import Establecimiento

router = APIRouter()

@router.get("/establecimientos")
def list_establecimientos(db: DbSession, skip: int = 0, limit: int = 100):
    """List establishments with pagination."""
    return db.query(Establecimiento).offset(skip).limit(limit).all()
```

### In Scripts/ETL

```python
from src.db import get_session_factory, Establecimiento

SessionLocal = get_session_factory()

with SessionLocal() as session:
    # Query data
    establecimientos = session.query(Establecimiento).filter_by(year_carga=2024).all()
    
    # Insert data
    new_est = Establecimiento(
        clave="123456",
        nombre_establecimiento="Example Corp",
        codigo_actividad="46111",
        # ... other fields
    )
    session.add(new_est)
    session.commit()
```

### Direct SQL Queries

```python
from src.db import get_engine

engine = get_engine()

with engine.connect() as conn:
    result = conn.execute(text("SELECT COUNT(*) FROM establecimientos"))
    count = result.scalar()
    print(f"Total establishments: {count}")
```

## Running the API

```bash
# Development mode with auto-reload
uv run fastapi dev src/api/main.py

# Production mode
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000
```

### API Endpoints

- `GET /health` - Basic health check
- `GET /api/v1/health/db` - Database health check
- `GET /api/v1/info` - API information
- `GET /api/v1/docs` - Swagger UI documentation
- `GET /api/v1/redoc` - ReDoc documentation

## Database Administration

### DuckDB

```bash
# Open DuckDB CLI
duckdb data/gold/denue.duckdb

# Run queries
D SELECT COUNT(*) FROM establecimientos;
D .tables
D .schema establecimientos
```

### PostgreSQL

```bash
# Connect with psql
psql -h localhost -U postgres -d denue

# Run queries
\dt                           -- List tables
\d establecimientos          -- Describe table
SELECT COUNT(*) FROM establecimientos;
```

## Best Practices

1. **Always use migrations** - Don't modify database schema directly
2. **Use the ORM** - Prefer SQLAlchemy models over raw SQL when possible
3. **Connection management** - Always use context managers or FastAPI dependencies
4. **Transactions** - Use `session.commit()` to persist changes
5. **Testing** - Use separate test database or in-memory DuckDB for tests

## Troubleshooting

### DuckDB file locked
- Ensure no other processes are using the database file
- Close all connections properly using context managers

### PostgreSQL connection refused
- Check if PostgreSQL is running: `pg_isready`
- Verify connection parameters in `.env`
- Check firewall/network settings

### Alembic migration errors
- For DuckDB: Create migrations manually (autogenerate not supported)
- For PostgreSQL: Ensure database is running before generating migrations
- Check `alembic/env.py` for proper configuration

## Additional Resources

- [SQLAlchemy 2.0 Documentation](https://docs.sqlalchemy.org/en/20/)
- [Alembic Documentation](https://alembic.sqlalchemy.org/)
- [FastAPI Database Tutorial](https://fastapi.tiangolo.com/tutorial/sql-databases/)
- [DuckDB Documentation](https://duckdb.org/docs/)
- [PostgreSQL Documentation](https://www.postgresql.org/docs/)
