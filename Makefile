.PHONY: help setup extract transform load clee-resolution-radio clee-resolution-h3 h3-backfill reorganize-domain-ids pipeline clean db-migrate db-downgrade db-reset test
.DEFAULT_GOAL := help

# Variables
PYTHON := uv run python
SILVER_DB := data/silver/denue.duckdb
GOLD_DB := data/gold/denue.duckdb
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

# ─── ETL Pipeline ────────────────────────────────────────────────────────────

extract: ## Extract data from source (Bronze)
	$(PYTHON) -m src_etl.main extract $(ARGS)

transform: ## Transform raw data (Bronze -> Silver)
	$(PYTHON) -m src_etl.main transform $(ARGS)

extract-sesnsp: ## Extract SESNSP incidencia delictiva municipal (datos.gob.mx -> Bronze)
	$(PYTHON) -m src_etl.main extract --source sesnsp $(ARGS)

transform-sesnsp: ## Transform SESNSP CSV (Bronze -> Silver sesnsp_incidencia_municipal)
	$(PYTHON) -m src_etl.main transform --source sesnsp $(ARGS)

build-entidades: ## Build entidades_economicas dimension (run AFTER CLEE resolution)
	$(PYTHON) -m src_etl.main build-entidades

load: ## Load Silver -> Gold
	$(PYTHON) -m src_etl.main load $(ARGS)

clee-resolution-radio: ## Run CLEE Resolution (radio/BallTree -> clee_propagado_radio)
	$(PYTHON) -m src_etl.main transform --no-ingest --no-normalize-dates --no-h3-cells --clee-resolution $(ARGS)

clee-resolution-h3: ## Run CLEE Resolution (H3 blocking -> clee_propagado_h3)
	$(PYTHON) -m src_etl.main transform --no-ingest --no-normalize-dates --no-h3-cells --no-clee-resolution --clee-resolution-h3 $(ARGS)

h3-backfill: ## Backfill H3 cells (h3_cell_9/h3_cell_5) in Silver
	$(PYTHON) -m src_etl.main transform --no-ingest --no-normalize-dates --no-clee-resolution --h3-cells $(ARGS)

reorganize-domain-ids: ## ONE-TIME: rebuild establecimientos con FKs por id (correr tras db-migrate)
	$(PYTHON) scripts/reorganize_domain_ids.py

eval-masked-clee-extract: ## Fase 7: extrae muestra de Silver (read-only) a data/eval/
	$(PYTHON) scripts/evaluate_masked_clee.py extract $(ARGS)

eval-masked-clee: ## Fase 7: evalua cascada con CLEE enmascarado (default nearest = produccion; ARGS="--ref-order newest" compara el orden legado)
	$(PYTHON) scripts/evaluate_masked_clee.py evaluate $(ARGS)

eval-masked-clee-chain: ## Fase 7: experimento dos saltos (target via intermedio resuelto probabilisticamente)
	$(PYTHON) scripts/evaluate_masked_clee.py chain $(ARGS)

retry: ## Retry failed extractions
	$(PYTHON) -m src_etl.main retry

pipeline: ## Full pipeline (Extract -> Transform -> Load)
	$(PYTHON) -m src_etl.main pipeline $(ARGS)

pipeline-steps: extract transform load ## Pipeline step by step
	@echo "Pipeline completed"

pipeline-full: setup extract transform load ## Full pipeline with setup
	@echo "Full pipeline completed"

validate: ## Validate warehouse
	@echo "Warehouse validation (coming soon)"

# ─── Cleanup ─────────────────────────────────────────────────────────────────

clean: ## Clean cache
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true

clean-logs: ## Clean old logs
	@echo "Cleaning old logs..."
	@find logs/downloads -type d -name "run_*" -mtime +7 -exec rm -rf {} + 2>/dev/null || true
	@echo "Old logs cleaned (kept last 7 days)"

# ─── Metadata ────────────────────────────────────────────────────────────────

metadata-query: ## Query metadata database
	@echo "Recent job runs:"
	@$(PYTHON) -c "from src_metadata.tracker import MetadataTracker; from pathlib import Path; t = MetadataTracker(Path('$(METADATA_DB)')); import json; print(json.dumps(t.get_recent_runs(5), indent=2, default=str))"

metadata-stats: ## Show metadata statistics
	@echo "Pipeline Statistics:"
	@$(PYTHON) -c "from src_etl.transformers.denue import DenueTransformer; from pathlib import Path; t = DenueTransformer(Path('data')); import json; status = t.get_pipeline_status(); print(json.dumps(status, indent=2, default=str))"

logs: ## Show latest logs
	@echo "Latest download logs:"
	@ls -la logs/downloads/latest/ 2>/dev/null || echo "No logs found yet"
	@echo ""
	@echo "Last 20 log entries:"
	@tail -20 logs/downloads/latest/app.log 2>/dev/null || echo "No app.log found yet"

# ─── Database Migrations ─────────────────────────────────────────────────────

db-migrate: ## Run pending migrations
	@echo "Running database migrations..."
	$(ALEMBIC) upgrade head
	@echo "Migrations completed"

db-downgrade: ## Rollback last migration
	@echo "Rolling back last migration..."
	$(ALEMBIC) downgrade -1
	@echo "Downgrade completed"

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

db-status: ## Show migration status and index health
	@echo "Current database status:"
	@echo ""
	$(ALEMBIC) current --verbose
	@echo ""
	@echo "Migration history:"
	$(ALEMBIC) history
	@echo ""
	@echo "Indexes:"
	@$(PYTHON) -c "from src_etl.core.config import settings; \
from src_etl.utils.warehouse import connect_configured; \
from src_etl.db.indexes import missing_indexes, expected_indexes; \
conn = connect_configured(settings.SILVER_DUCKDB_PATH, read_only=True); \
m = missing_indexes(conn); \
print(f'  {len(expected_indexes()) - len(m)}/{len(expected_indexes())} presentes' + (f' | FALTAN: {m}' if m else ' (completos)')); \
print('  (faltan tras una corrida interrumpida: la siguiente los restituye)' if m else ''); \
conn.close()"

# ─── Development ─────────────────────────────────────────────────────────────

dev: setup ## Setup development
	@echo "Ready for development"

# ─── Testing ─────────────────────────────────────────────────────────────────

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
