# Transaction Management in Alembic Migrations

## Overview

This document explains how transaction management works in our seed loading migrations and why we follow Alembic's transaction model.

## The Problem (Before Fix)

Previously, our migration code contained explicit `commit()` and `rollback()` calls:

```python
# ❌ INCORRECT - Don't do this
def upgrade():
    connection = op.get_bind()
    
    connection.execute(text("DELETE FROM table"))
    connection.commit()  # ❌ Commits transaction early
    
    # Load data...
    connection.commit()  # ❌ Another commit
    
    # Alembic tries to update version → ❌ FAILS!
```

**Issues:**
- ❌ Alembic version doesn't update correctly
- ❌ State mismatch (data changes but version doesn't)
- ❌ No atomic operations (partial state possible)
- ❌ Rollback doesn't work properly

## The Solution (After Fix)

Remove all explicit transaction management and let Alembic handle it:

```python
# ✅ CORRECT - Let Alembic manage transactions
def upgrade():
    connection = op.get_bind()
    
    connection.execute(text("DELETE FROM table"))
    # ✅ No commit - stays in Alembic's transaction
    
    # Load data...
    # ✅ No commit - stays in Alembic's transaction
    
# Alembic commits entire transaction + updates version → ✅ Works!
```

**Benefits:**
- ✅ Version tracking works correctly
- ✅ Atomic operations (all-or-nothing)
- ✅ Proper rollback on errors
- ✅ Database state always matches version

## How Alembic Transaction Management Works

### Transaction Lifecycle

```
┌─────────────────────────────────────────────────────────────┐
│ Alembic Starts Transaction                                  │
├─────────────────────────────────────────────────────────────┤
│ 1. BEGIN TRANSACTION                                         │
│ 2. Execute migration upgrade() function                      │
│    - DELETE operations                                       │
│    - INSERT operations                                       │
│    - All data modifications                                  │
│ 3. Update alembic_version table                             │
│ 4. COMMIT TRANSACTION (or ROLLBACK on error)                │
└─────────────────────────────────────────────────────────────┘
```

### Key Points

1. **Single Transaction**: Alembic wraps the entire migration in ONE transaction
2. **Version Update**: `alembic_version` is updated AFTER migration succeeds
3. **Atomic Commit**: Data changes + version update happen together
4. **Auto Rollback**: If migration fails, everything rolls back (including version)

## Implementation Guidelines

### DO ✅

```python
def upgrade():
    connection = op.get_bind()
    
    # Execute SQL operations
    connection.execute(text("DELETE FROM table"))
    connection.execute(text("INSERT INTO table VALUES (...)"))
    
    # Let Alembic handle commit
```

```python
def downgrade():
    connection = op.get_bind()
    
    # Execute SQL operations
    connection.execute(text("DELETE FROM table"))
    
    # Let Alembic handle commit
```

```python
def upgrade():
    try:
        # Migration logic
        pass
    except Exception as e:
        # Log error, then re-raise
        print(f"Error: {e}")
        raise  # ✅ Let Alembic handle rollback
```

### DON'T ❌

```python
def upgrade():
    connection = op.get_bind()
    
    connection.execute(text("DELETE FROM table"))
    connection.commit()  # ❌ Don't commit manually
```

```python
def upgrade():
    connection = op.get_bind()
    
    try:
        # Migration logic
    except Exception as e:
        connection.rollback()  # ❌ Don't rollback manually
        raise
```

```python
def upgrade():
    connection = op.get_bind()
    
    # Partial commits
    connection.execute(text("DELETE FROM table1"))
    connection.commit()  # ❌ Creates partial state
    
    connection.execute(text("DELETE FROM table2"))
    connection.commit()  # ❌ Version update may fail
```

## Why This Matters

### Scenario: Migration Fails Halfway

**With Manual Commits (BROKEN):**
```
1. Delete data from table1 → commit() ✓
2. Delete data from table2 → commit() ✓
3. Insert new data → FAILS ❌
4. Alembic tries to update version → FAILS ❌

Result: 
- Data partially modified
- Version not updated
- Database in inconsistent state 💥
```

**With Alembic Transaction Management (CORRECT):**
```
1. BEGIN TRANSACTION
2. Delete data from table1 ✓
3. Delete data from table2 ✓
4. Insert new data → FAILS ❌
5. ROLLBACK entire transaction

Result:
- No data modified
- Version not updated
- Database remains consistent ✅
```

## Database Compatibility

### DuckDB

```python
# Works with Alembic transaction management ✅
connection.execute(text("DELETE FROM table"))
connection.execute(text("INSERT INTO table VALUES (...)"))
# Alembic commits at end
```

### PostgreSQL

```python
# Works with Alembic transaction management ✅
connection.execute(text("DELETE FROM table"))
connection.execute(text("INSERT INTO table VALUES (...)"))
# Alembic commits at end
```

**Both databases work identically** - no special handling needed.

## Error Handling

### Pattern 1: Re-raise Exceptions (Recommended)

```python
def upgrade():
    try:
        # Migration logic
        load_seed_data(connection, path)
    except SeedLoadError as e:
        print(f"Seed loading failed: {e}")
        raise  # ✅ Alembic handles rollback
```

### Pattern 2: Custom Error Messages

```python
def upgrade():
    try:
        # Migration logic
        load_seed_data(connection, path)
    except Exception as e:
        print("=" * 70)
        print("❌ Migration Failed")
        print("=" * 70)
        print(f"Error: {e}")
        print("Alembic will rollback automatically...")
        raise  # ✅ Let Alembic rollback
```

### Anti-Pattern: Manual Rollback ❌

```python
def upgrade():
    try:
        # Migration logic
        load_seed_data(connection, path)
    except Exception as e:
        connection.rollback()  # ❌ DON'T DO THIS
        raise
```

**Why?** Alembic's transaction is already broken by explicit rollback, causing version tracking issues.

## Testing Transaction Behavior

### Test 1: Successful Migration

```bash
$ alembic upgrade head

# Expected:
# - Migration executes successfully
# - Data is loaded
# - Version updates to target revision
# - All in single transaction
```

### Test 2: Failed Migration

```bash
$ alembic upgrade head
# (Migration fails midway)

# Expected:
# - Transaction rolls back automatically
# - No data changes persisted
# - Version remains at previous revision
# - Database state consistent
```

### Test 3: Upgrade/Downgrade Cycle

```bash
# Start at base
$ alembic upgrade head
# Version: dff9d0804542, Data: 42,777 rows ✅

$ alembic downgrade -1
# Version: cbc954c59583, Data: 0 rows ✅

$ alembic upgrade head
# Version: dff9d0804542, Data: 42,777 rows ✅
```

All three commands should update version correctly and maintain data consistency.

## Common Pitfalls

### Pitfall 1: Mixing Transaction Levels

```python
# ❌ WRONG
def upgrade():
    connection.execute(text("DELETE FROM table"))
    connection.commit()  # Manual commit
    
    load_seed_data(connection, path)
    # load_seed_data also has commits inside!
    
    # Alembic tries to update version → FAILS
```

**Fix:** Remove ALL commits from migration AND helper functions.

### Pitfall 2: Not Re-raising Exceptions

```python
# ❌ WRONG
def upgrade():
    try:
        load_seed_data(connection, path)
    except Exception as e:
        print(f"Error: {e}")
        # Missing raise - migration appears successful!
```

**Fix:** Always re-raise exceptions after logging.

### Pitfall 3: Committing in Helper Functions

```python
# ❌ WRONG
def load_seed_data(connection, path):
    connection.execute(text("INSERT ..."))
    connection.commit()  # Breaks Alembic transaction!
```

**Fix:** Remove commits from helper functions called during migrations.

## References

- [Alembic Transaction Management](https://alembic.sqlalchemy.org/en/latest/tutorial.html#running-our-first-migration)
- [SQLAlchemy Transactions](https://docs.sqlalchemy.org/en/20/core/connections.html#using-transactions)
- [Migration Best Practices](https://alembic.sqlalchemy.org/en/latest/cookbook.html)

## Summary

**Golden Rule:** 
> Let Alembic manage transactions. Never call `commit()` or `rollback()` in migration code.

This ensures:
- ✅ Correct version tracking
- ✅ Atomic operations
- ✅ Proper error handling
- ✅ Database consistency
- ✅ PostgreSQL compatibility
