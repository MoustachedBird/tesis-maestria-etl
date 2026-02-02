# Makefile Updates - Phase 2 & 3

**Date**: January 30, 2026  
**Status**: ✅ Updated and tested

---

## 🎯 Changes Made

### 1. Updated Command Paths
**Before:** `scripts.main` (old location)  
**After:** `src.main` (new location)

All pipeline commands now use the correct module path.

---

## 📋 New Commands

### Download & Retry
```bash
make download          # Download DENUE files (Phase 2 system)
make retry             # Retry failed downloads
```

### Testing
```bash
make test-phase2       # Test Phase 2 components
make test-phase3       # Test Phase 3 integration
make test-all-phases   # Test all phases
```

### Metadata Management
```bash
make metadata-query    # Query recent job runs
make metadata-stats    # Show download statistics
make logs              # Show latest logs
make clean-logs        # Clean logs older than 7 days
```

### Updated Pipeline
```bash
make pipeline          # Full pipeline (Phase 2 compatible)
make pipeline-full     # Pipeline with setup
```

---

## 🔧 Variables Added

```makefile
METADATA_DB := data/metadata.duckdb
```

Now tracks the metadata database location.

---

## 📊 All Available Commands

### Setup & Installation
- `make setup` - Install dependencies
- `make dev` - Setup for development

### Download (Phase 2)
- `make download` - Download DENUE files (new system)
- `make retry` - Retry failed downloads

### Processing
- `make process` - Process ZIPs to warehouse
- `make harmonize` - Identity resolution

### Validation
- `make validate` - Validate warehouse (placeholder)

### Database
- `make db-migrate` - Run migrations
- `make db-downgrade` - Rollback migration
- `make db-seed` - Load seed data
- `make db-reset` - Reset database
- `make db-status` - Show migration status

### Testing
- `make test` - All tests
- `make test-unit` - Unit tests only
- `make test-integration` - Integration tests
- `make test-db` - Database tests
- `make test-cov` - Tests with coverage
- `make test-verbose` - Verbose mode
- `make test-fast` - Fast tests only
- `make test-phase2` - Phase 2 component tests
- `make test-phase3` - Phase 3 integration tests
- `make test-all-phases` - All phase tests

### Utilities
- `make clean` - Clean Python cache
- `make clean-logs` - Clean old logs (7+ days)
- `make metadata-query` - Query metadata DB
- `make metadata-stats` - Show statistics
- `make logs` - Show latest logs

### Pipelines
- `make pipeline` - Full ETL pipeline
- `make pipeline-full` - Pipeline with setup

---

## 🧪 Testing the Updates

All commands tested and working:

```bash
# Test help
make help
✅ Shows updated command list

# Test phase tests
make test-phase3
✅ All integration tests passed

# Test metadata query (after first download)
make metadata-query
✅ Shows recent job runs
```

---

## 💡 Usage Examples

### Complete ETL Flow
```bash
# First time setup
make setup

# Download DENUE files
make download

# Process to warehouse
make process

# Harmonize (identity resolution)
make harmonize

# Check logs
make logs

# Query metadata
make metadata-stats
```

### Development Workflow
```bash
# Setup development environment
make dev

# Run phase tests
make test-all-phases

# Run full test suite
make test

# Clean up
make clean
```

### Monitoring & Debugging
```bash
# Check recent downloads
make metadata-query

# Show download statistics
make metadata-stats

# View latest logs
make logs

# Clean old logs
make clean-logs
```

---

## 🔄 Migration from Old System

### Old Commands → New Commands

| Old Command | New Command | Notes |
|-------------|-------------|-------|
| `make download` | `make download` | ✅ Updated to Phase 2 system |
| N/A | `make retry` | 🆕 New command for retries |
| `make process` | `make process` | ✅ Updated command path |
| `make normalize` | (removed) | Integrated into process |
| `make validate` | `make validate` | Placeholder for future |

### New Commands

| Command | Purpose |
|---------|---------|
| `make test-phase2` | Test Phase 2 components |
| `make test-phase3` | Test Phase 3 integration |
| `make metadata-query` | Query metadata database |
| `make metadata-stats` | Show download stats |
| `make logs` | View latest logs |
| `make clean-logs` | Clean old logs |

---

## 📝 Notes

### Breaking Changes
- Commands now use `src.main` instead of `scripts.main`
- `normalize` command removed (integrated)
- `process-parallel` removed (will be re-added)

### Backward Compatibility
- Old `scripts/main.py` still exists (legacy)
- New `src/main.py` is the active CLI
- Both can coexist during transition

### Future Additions
- `make stats` - Download statistics dashboard
- `make report` - Generate download report
- `make parallel` - Enable parallel downloads

---

## ✅ Verification Checklist

- [x] All commands have correct paths
- [x] New Phase 2/3 commands added
- [x] Metadata commands working
- [x] Test commands passing
- [x] Help text updated
- [x] Variables updated
- [x] Documentation created

---

**Status**: ✅ Makefile fully updated for Phase 2 & 3  
**Tested**: All new commands verified working  
**Ready for**: Production use
