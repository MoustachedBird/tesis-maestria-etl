.PHONY: help setup extract transform load pipeline build-entidades clean db-migrate db-downgrade db-reset test
.DEFAULT_GOAL := help

# Variables
PYTHON := uv run python
SILVER_DB := data/silver/mipymes.duckdb
GOLD_DB := data/gold/mipymes.duckdb
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
	uv run fastapi dev src/api/main.py

# ─── ETL Pipeline ────────────────────────────────────────────────────────────

# Comandos globales: sin ARGS corren TODO; aisla secciones con flags --no-* (ver AGENTS.md).

extract: ## Extract ALL sources (DENUE + SESNSP -> Bronze); ARGS="--no-denue|--no-sesnsp|--force"
	$(PYTHON) -m src.etl.main extract $(ARGS)

transform: ## Transform ALL sections (Bronze -> Silver); aisla con ARGS="--no-* ..."
	$(PYTHON) -m src.etl.main transform $(ARGS)

load: ## Load Silver -> Gold; ARGS="--force"
	$(PYTHON) -m src.etl.main load $(ARGS)

pipeline: ## Full pipeline global (Transform -> Load; --extract para incluir descarga)
	$(PYTHON) -m src.etl.main pipeline $(ARGS)

build-entidades: ## Build entidades_economicas dimension (run AFTER CLEE resolution)
	$(PYTHON) -m src.etl.main build-entidades

eval-masked-clee-extract: ## Fase 7: extrae muestra de Silver (read-only) a data/eval/
	$(PYTHON) scripts/evaluate_masked_clee.py extract $(ARGS)

eval-masked-clee: ## Fase 7: evalua cascada con CLEE enmascarado (default nearest = produccion; ARGS="--ref-order newest" compara el orden legado)
	$(PYTHON) scripts/evaluate_masked_clee.py evaluate $(ARGS)

eval-masked-clee-chain: ## Fase 7: experimento dos saltos (target via intermedio resuelto probabilisticamente)
	$(PYTHON) scripts/evaluate_masked_clee.py chain $(ARGS)

propagation-stats: ## Cobertura/salud de clee_propagado_* sobre Silver (read-only); ARGS="--db ... --column ..."
	$(PYTHON) scripts/clee_propagation_stats.py $(ARGS)

retry: ## Retry failed extractions
	$(PYTHON) -m src.etl.main retry

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
	@$(PYTHON) -c "from src.meta.tracker import MetadataTracker; from pathlib import Path; t = MetadataTracker(Path('$(METADATA_DB)')); import json; print(json.dumps(t.get_recent_runs(5), indent=2, default=str))"

metadata-stats: ## Show metadata statistics
	@echo "Pipeline Statistics:"
	@$(PYTHON) -c "from src.etl.transformers.denue import DenuePipelineOrchestrator; from pathlib import Path; t = DenuePipelineOrchestrator(Path('data')); import json; status = t.get_pipeline_status(); print(json.dumps(status, indent=2, default=str))"

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
	@$(PYTHON) -c "from src.shared.config import settings; \
from src.etl.utils.warehouse import connect_configured; \
from src.etl.db.indexes import missing_indexes, expected_indexes; \
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
	uv run pytest --cov=src --cov-report=html --cov-report=term
	@echo "Coverage report at: htmlcov/index.html"

test-verbose: ## Tests in verbose mode
	uv run pytest -vv

test-fast: ## Fast tests (unit only, no slow)
	uv run pytest -m "unit and not slow"

lint: ## Run ruff linter
	uv run ruff check src/ tests/ scripts/ alembic/

lint-fix: ## Auto-fix lint issues
	uv run ruff check src/ tests/ scripts/ alembic/ --fix

format: ## Format code with ruff
	uv run ruff format src/ tests/ scripts/ alembic/
