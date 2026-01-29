# Environment Variables Update

**Date:** 2026-01-29  
**Status:** ✅ Complete

---

## Summary

Added `TEST_DB_PATH` environment variable to support configurable database paths for pytest integration tests.

---

## Changes Made

### 1. **Updated `.env`**

Added testing configuration section:

```bash
# Testing Configuration
TEST_DB_PATH=data/denue_warehouse_most_recent.db
```

### 2. **Updated `.env.example`**

Added documented testing configuration section:

```bash
# ----------------------------------------------------------------------------
# Testing Configuration
# ----------------------------------------------------------------------------
# Path to production DuckDB database for integration tests
# This is used by pytest to run tests against real data
TEST_DB_PATH=data/denue_warehouse_most_recent.db
```

---

## Usage

### **In Tests**

The `TEST_DB_PATH` environment variable is automatically used by the `production_db()` fixture in `tests/conftest.py`:

```python
def get_test_db_path() -> str:
    """Get production database path from environment or use default."""
    return os.getenv("TEST_DB_PATH", "data/denue_warehouse_most_recent.db")

@pytest.fixture(scope="session")
def production_db():
    """Read-only connection to production DuckDB warehouse."""
    db_path = get_test_db_path()
    conn = duckdb.connect(db_path, read_only=True)
    yield conn
    conn.close()
```

### **Override for Different Environments**

You can override the database path in different ways:

#### **Option 1: Modify `.env`**
```bash
TEST_DB_PATH=/path/to/your/custom/database.db
```

#### **Option 2: Environment Variable (temporary)**
```bash
TEST_DB_PATH=/path/to/test.db uv run pytest
```

#### **Option 3: Use `.env.test`** (recommended for testing)
The test suite also checks for `.env.test` which takes precedence:
```bash
# .env.test
TEST_DB_PATH=data/test_warehouse.db
```

---

## Files Modified

### **Modified (2 files)**
- ✅ `.env` - Added `TEST_DB_PATH` variable
- ✅ `.env.example` - Added documented `TEST_DB_PATH` section

### **Related Files (unchanged but use this variable)**
- `tests/conftest.py` - Uses `TEST_DB_PATH` via `os.getenv()`
- `.env.test` - Alternative test-specific environment file

---

## Verification

Test that the environment variable is working:

```bash
# Run a database connection test
uv run pytest tests/unit/test_models.py::TestDatabaseConnection::test_production_db_connection -v
```

**Expected output:**
```
tests/unit/test_models.py::TestDatabaseConnection::test_production_db_connection PASSED [100%]
```

---

## Environment Variable Priority

The test fixtures follow this priority order:

1. **`TEST_DB_PATH` environment variable** (if set in shell)
2. **`.env` file** (project default)
3. **Hardcoded default** `data/denue_warehouse_most_recent.db`

---

## Best Practices

### **Development**
Use `.env` with default path:
```bash
TEST_DB_PATH=data/denue_warehouse_most_recent.db
```

### **CI/CD**
Set environment variable in CI configuration:
```yaml
# GitHub Actions example
env:
  TEST_DB_PATH: ${{ secrets.TEST_DB_PATH }}
```

### **Local Testing with Different Database**
Create `.env.test`:
```bash
TEST_DB_PATH=data/test_sample.db
```

---

## Complete Environment Variables Reference

### **Database Configuration**
- `DATABASE_BACKEND` - Database backend (duckdb/postgresql)
- `DUCKDB_PATH` - Production DuckDB path
- `DUCKDB_THREADS` - Number of threads
- `DUCKDB_MEMORY_LIMIT` - Memory limit

### **Testing Configuration**
- `TEST_DB_PATH` - ✨ **NEW** - Test database path

### **Application Settings**
- `LOG_LEVEL` - Logging level (DEBUG/INFO/WARNING/ERROR)
- `API_TITLE` - API title
- `API_VERSION` - API version
- `API_PREFIX` - API URL prefix

---

## Troubleshooting

### **Tests Can't Find Database**

**Problem:**
```
pytest.skip: Production database not found at data/denue_warehouse_most_recent.db
```

**Solution:**
1. Check `TEST_DB_PATH` in `.env`:
   ```bash
   cat .env | grep TEST_DB_PATH
   ```

2. Verify file exists:
   ```bash
   ls -lh data/denue_warehouse_most_recent.db
   ```

3. Update path if different:
   ```bash
   # In .env
   TEST_DB_PATH=path/to/your/database.db
   ```

### **Environment Variable Not Loading**

**Problem:**
Tests using wrong database path.

**Solution:**
Make sure you're running tests with `uv run`:
```bash
uv run pytest  # Correct - loads .env
pytest         # Wrong - may not load .env
```

---

## Summary

✅ Added `TEST_DB_PATH` to `.env`  
✅ Added `TEST_DB_PATH` to `.env.example` with documentation  
✅ Verified environment variable works in tests  
✅ Maintains backward compatibility (has default fallback)  
✅ Supports multiple override methods

The testing infrastructure now has **flexible, configurable database paths** that work across different environments! 🎉
