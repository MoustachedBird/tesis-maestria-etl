.PHONY: help setup download retry process normalize harmonize validate clean db-migrate db-downgrade db-seed db-reset test
.DEFAULT_GOAL := help

# Variables
PYTHON := uv run python
WAREHOUSE := data/denue_warehouse.db
METADATA_DB := data/metadata.duckdb
ALEMBIC := uv run alembic

help: ## Show help
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo "DENUE ETL Pipeline"
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'
	@echo ""

setup: ## Install dependencies
	uv sync

api: ## Run FastAPI dev server
	uv run fastapi dev src_api/main.py

download: ## Download INEGI data
	$(PYTHON) -m src_etl.main download-inegi $(ARGS)

retry: ## Retry failed downloads
	$(PYTHON) -m src_etl.main retry-inegi

process: ## Process RAW -> Warehouse
	$(PYTHON) -m src_etl.main process-denue $(ARGS)

process-force: ## Process ALL files (ignore state)
	$(PYTHON) -m src_etl.main process-denue --force

harmonize: ## Run harmonization (Identity Resolution)
	$(PYTHON) -m src_etl.main harmonize

validate: ## Validate warehouse
	@echo "Warehouse validation (coming soon)"

clean: ## Clean cache
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true

clean-logs: ## Clean old logs
	@echo "Cleaning old logs..."
	@find logs/downloads -type d -name "run_*" -mtime +7 -exec rm -rf {} + 2>/dev/null || true
	@echo "Old logs cleaned (kept last 7 days)"

# Metadata commands
metadata-query: ## Query metadata database
	@echo "Recent job runs:"
	@$(PYTHON) -c "from src_metadata.tracker import MetadataTracker; from pathlib import Path; t = MetadataTracker(Path('$(METADATA_DB)')); import json; print(json.dumps(t.get_recent_runs(5), indent=2, default=str))"

metadata-stats: ## Show metadata statistics
	@echo "Pipeline Statistics:"
	@$(PYTHON) -c "from src_etl.transformers.denue_transformer import DenueTransformer; from pathlib import Path; t = DenueTransformer(Path('data')); import json; status = t.get_pipeline_status(); print(json.dumps(status, indent=2, default=str))"

logs: ## Show latest logs
	@echo "Latest download logs:"
	@ls -la logs/downloads/latest/ 2>/dev/null || echo "No logs found yet"
	@echo ""
	@echo "Last 20 log entries:"
	@tail -20 logs/downloads/latest/app.log 2>/dev/null || echo "No app.log found yet"

# Database migrations
db-migrate: ## Run pending migrations
	@echo "Running database migrations..."
	$(ALEMBIC) upgrade head
	@echo "Migrations completed"

db-downgrade: ## Rollback last migration
	@echo "Rolling back last migration..."
	$(ALEMBIC) downgrade -1
	@echo "Downgrade completed"

db-seed: ## Load seeds (catalog data)
	@echo "Loading seed data..."
	$(ALEMBIC) downgrade cbc954c59583
	$(ALEMBIC) upgrade head
	@echo "Seed data loaded"

db-reset: ## Full database reset (schema + seeds)
	@echo "Resetting database..."
	@echo "WARNING: This will drop all tables and data"
	@read -p "Are you sure? [y/N] " -n 1 -r; \
	echo; \
	if [[ $$REPLY =~ ^[Yy]$$ ]]; then \
		$(ALEMBIC) downgrade base; \
		$(ALEMBIC) upgrade head; \
		echo "Database reset completed"; \
	else \
		echo "Reset cancelled"; \
	fi

db-status: ## Show migration status
	@echo "Current database status:"
	@echo ""
	$(ALEMBIC) current --verbose
	@echo ""
	@echo "Migration history:"
	$(ALEMBIC) history

# Pipeline
pipeline: ## Full pipeline (Download -> Process -> Harmonize)
	$(PYTHON) -m src_etl.main pipeline

pipeline-force: ## Full pipeline with force mode
	$(PYTHON) -m src_etl.main pipeline --force

pipeline-steps: download process harmonize ## Pipeline step by step
	@echo "Pipeline completed"

pipeline-full: setup download process harmonize validate ## Full pipeline with setup
	@echo "Full pipeline completed"

# Development
dev: setup ## Setup development
	@echo "Ready for development"

# Testing
test: ## Run all tests
	uv run pytest

test-unit: ## Unit tests (fast)
	uv run pytest -m unit

test-integration: ## Integration tests (with DB)
	uv run pytest -m integration

test-db: ## Database tests
	uv run pytest -m db

test-cov: ## Tests with coverage
	uv run pytest --cov=src_etl --cov=src_metadata --cov-report=html --cov-report=term
	@echo "Coverage report at: htmlcov/index.html"

test-verbose: ## Tests in verbose mode
	uv run pytest -vv

test-fast: ## Fast tests (unit only, no slow)
	uv run pytest -m "unit and not slow"

lint: ## Run ruff linter
	uv run ruff check src_etl/ src_metadata/ src_api/ tests/

lint-fix: ## Auto-fix lint issues
	uv run ruff check src_etl/ src_metadata/ src_api/ tests/ --fix

format: ## Format code with ruff
	uv run ruff format src_etl/ src_metadata/ src_api/ tests/
