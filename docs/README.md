# ETL Core Documentation

This directory contains all documentation for the DENUE ETL Core project.

## 📚 Documentation Index

### Getting Started

- **[AGENTS.md](./AGENTS.md)** - Development guide and team workflows
  - Project overview and architecture
  - Development environment setup
  - Common tasks and commands
  - Team collaboration guidelines

### Database & Migrations

- **[DATABASE_SETUP.md](./DATABASE_SETUP.md)** - Database configuration guide
  - DuckDB and PostgreSQL setup
  - Connection configuration
  - Environment variables

- **[DATABASE_MIGRATION_SUMMARY.md](./DATABASE_MIGRATION_SUMMARY.md)** - Migration system overview
  - Alembic migration workflow
  - Schema management
  - Migration best practices

- **[SEEDING.md](./SEEDING.md)** - Complete seed data guide
  - Seed loading architecture
  - Usage examples and troubleshooting
  - 42,777 catalog records management

- **[SEED_QUICKSTART.md](./SEED_QUICKSTART.md)** - Quick reference for seeding
  - Common commands
  - Expected outputs
  - Quick troubleshooting

- **[TRANSACTION_MANAGEMENT.md](./TRANSACTION_MANAGEMENT.md)** - Alembic transaction best practices
  - How Alembic handles transactions
  - Common pitfalls and solutions
  - Migration do's and don'ts

### Testing

- **[TESTING_SETUP.md](./TESTING_SETUP.md)** - Testing framework documentation
  - pytest configuration
  - Test markers and organization
  - Running tests
  - Coverage reports

### Configuration

- **[ENV_VARIABLES_UPDATE.md](./ENV_VARIABLES_UPDATE.md)** - Environment variables reference
  - Available settings
  - Configuration options
  - Environment-specific setup

- **[GITIGNORE_UPDATE.md](./GITIGNORE_UPDATE.md)** - Git ignore patterns
  - Excluded files and directories
  - Rationale for exclusions

## 🗺️ Documentation Map by Use Case

### "I'm new to the project"
1. Start with [AGENTS.md](./AGENTS.md) - Project overview
2. Read [DATABASE_SETUP.md](./DATABASE_SETUP.md) - Setup your database
3. Review [ENV_VARIABLES_UPDATE.md](./ENV_VARIABLES_UPDATE.md) - Configure environment
4. Check [SEED_QUICKSTART.md](./SEED_QUICKSTART.md) - Load initial data

### "I need to work with database migrations"
1. Read [DATABASE_MIGRATION_SUMMARY.md](./DATABASE_MIGRATION_SUMMARY.md) - Understand migrations
2. Study [TRANSACTION_MANAGEMENT.md](./TRANSACTION_MANAGEMENT.md) - Learn best practices
3. Reference [SEEDING.md](./SEEDING.md) - Understand seed data management

### "I'm writing tests"
1. Read [TESTING_SETUP.md](./TESTING_SETUP.md) - Test framework guide
2. Check [DATABASE_SETUP.md](./DATABASE_SETUP.md) - Test database setup

### "I need to load/update seed data"
1. Quick start: [SEED_QUICKSTART.md](./SEED_QUICKSTART.md)
2. Full guide: [SEEDING.md](./SEEDING.md)
3. Troubleshooting: [TRANSACTION_MANAGEMENT.md](./TRANSACTION_MANAGEMENT.md)

## 📊 Documentation Statistics

| Document | Lines | Purpose | Last Updated |
|----------|-------|---------|--------------|
| AGENTS.md | ~250 | Development guide | Jan 28, 2026 |
| DATABASE_SETUP.md | ~200 | Database config | Jan 28, 2026 |
| DATABASE_MIGRATION_SUMMARY.md | ~250 | Migration guide | Jan 28, 2026 |
| SEEDING.md | ~450 | Seed data system | Jan 29, 2026 |
| SEED_QUICKSTART.md | ~80 | Quick reference | Jan 29, 2026 |
| TRANSACTION_MANAGEMENT.md | ~300 | Transaction guide | Jan 29, 2026 |
| TESTING_SETUP.md | ~400 | Testing guide | Jan 29, 2026 |
| ENV_VARIABLES_UPDATE.md | ~150 | Environment vars | Jan 29, 2026 |
| GITIGNORE_UPDATE.md | ~250 | Git patterns | Jan 29, 2026 |

**Total**: ~2,330 lines of documentation

## 🔍 Quick Search

Looking for something specific? Here are some common topics:

- **Alembic**: See [DATABASE_MIGRATION_SUMMARY.md](./DATABASE_MIGRATION_SUMMARY.md), [SEEDING.md](./SEEDING.md), [TRANSACTION_MANAGEMENT.md](./TRANSACTION_MANAGEMENT.md)
- **PostgreSQL**: See [DATABASE_SETUP.md](./DATABASE_SETUP.md), [SEEDING.md](./SEEDING.md)
- **DuckDB**: See [DATABASE_SETUP.md](./DATABASE_SETUP.md), [AGENTS.md](./AGENTS.md)
- **Testing**: See [TESTING_SETUP.md](./TESTING_SETUP.md)
- **Seed Data**: See [SEEDING.md](./SEEDING.md), [SEED_QUICKSTART.md](./SEED_QUICKSTART.md)
- **Transactions**: See [TRANSACTION_MANAGEMENT.md](./TRANSACTION_MANAGEMENT.md)
- **Environment Setup**: See [ENV_VARIABLES_UPDATE.md](./ENV_VARIABLES_UPDATE.md)
- **Development**: See [AGENTS.md](./AGENTS.md)

## 🛠️ Contributing to Documentation

When adding or updating documentation:

1. **Follow the existing format** - Use the same structure as similar docs
2. **Add to this README** - Update the index and search sections
3. **Include examples** - Show, don't just tell
4. **Keep it updated** - Update "Last Updated" dates
5. **Cross-reference** - Link to related documentation

## 📝 Documentation Standards

All documentation should:

- ✅ Use clear, concise language
- ✅ Include code examples where relevant
- ✅ Provide troubleshooting sections
- ✅ Link to related documentation
- ✅ Include a table of contents for long docs
- ✅ Use consistent markdown formatting
- ✅ Include emoji for visual clarity (but don't overuse)

## 🔗 External Resources

- [Alembic Documentation](https://alembic.sqlalchemy.org/)
- [SQLAlchemy Documentation](https://docs.sqlalchemy.org/)
- [Polars Documentation](https://pola-rs.github.io/polars/)
- [pytest Documentation](https://docs.pytest.org/)
- [DuckDB Documentation](https://duckdb.org/docs/)
- [PostgreSQL Documentation](https://www.postgresql.org/docs/)

## 📧 Questions?

If you can't find what you're looking for:

1. Check the [AGENTS.md](./AGENTS.md) development guide
2. Search this documentation directory
3. Contact the development team

---

_Last updated: January 29, 2026_
