.PHONY: help setup download retry process normalize harmonize validate clean db-migrate db-downgrade db-seed db-reset test-phase2 test-phase3
.DEFAULT_GOAL := help

# Variables
PYTHON := uv run python
WAREHOUSE := data/denue_warehouse.db
METADATA_DB := data/metadata.duckdb
ALEMBIC := uv run alembic

help: ## 📚 Muestra esta ayuda
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo "🚀 DENUE ETL Pipeline"
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'
	@echo ""

setup: ## 🔧 Instala dependencias
	uv sync

download: ## ⬇️  Descarga datos INEGI (Phase 2 system)
	$(PYTHON) -m src.main download-inegi $(ARGS)

retry: ## 🔄 Reintenta descargas fallidas
	$(PYTHON) -m src.main retry-inegi

process: ## ⚙️  Procesa archivos RAW → Warehouse
	$(PYTHON) -m src.main process-denue $(ARGS)

process-force: ## ⚙️  Procesa TODOS los archivos (ignora estado)
	$(PYTHON) -m src.main process-denue --force

harmonize: ## 🔗 Ejecuta harmonización (Identity Resolution)
	$(PYTHON) -m src.main harmonize

validate: ## ✅ Valida warehouse
	@echo "📊 Warehouse validation (coming soon)"

clean: ## 🧹 Limpia cache
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true

clean-logs: ## 🧹 Limpia logs antiguos
	@echo "🧹 Cleaning old logs..."
	@find logs/downloads -type d -name "run_*" -mtime +7 -exec rm -rf {} + 2>/dev/null || true
	@echo "✅ Old logs cleaned (kept last 7 days)"

# Metadata commands
metadata-query: ## 📊 Query metadata database
	@echo "📊 Recent job runs:"
	@$(PYTHON) -c "from src.metadata.tracker import MetadataTracker; from pathlib import Path; t = MetadataTracker(Path('$(METADATA_DB)')); import json; print(json.dumps(t.get_recent_runs(5), indent=2, default=str))"

metadata-stats: ## 📈 Show metadata statistics
	@echo "📈 Pipeline Statistics:"
	@$(PYTHON) -c "from src.transformers.denue_transformer import DenueTransformer; from pathlib import Path; t = DenueTransformer(Path('data')); import json; status = t.get_pipeline_status(); print(json.dumps(status, indent=2, default=str))"

logs: ## 📝 Show latest logs
	@echo "📝 Latest download logs:"
	@ls -la logs/downloads/latest/ 2>/dev/null || echo "No logs found yet"
	@echo ""
	@echo "📄 Last 20 log entries:"
	@tail -20 logs/downloads/latest/app.log 2>/dev/null || echo "No app.log found yet"

# Database migrations
db-migrate: ## 🗄️  Ejecuta migraciones pendientes
	@echo "🔄 Running database migrations..."
	$(ALEMBIC) upgrade head
	@echo "✅ Migrations completed"

db-downgrade: ## ⏮️  Rollback última migración
	@echo "⏮️  Rolling back last migration..."
	$(ALEMBIC) downgrade -1
	@echo "✅ Downgrade completed"

db-seed: ## 🌱 Carga seeds (catalog data)
	@echo "🌱 Loading seed data..."
	@echo "   This will re-run the seed migration (idempotent)"
	$(ALEMBIC) downgrade cbc954c59583
	$(ALEMBIC) upgrade head
	@echo "✅ Seed data loaded"

db-reset: ## 🔄 Reset completo de BD (schema + seeds)
	@echo "🔄 Resetting database..."
	@echo "⚠️  This will drop all tables and data"
	@read -p "Are you sure? [y/N] " -n 1 -r; \
	echo; \
	if [[ $$REPLY =~ ^[Yy]$$ ]]; then \
		$(ALEMBIC) downgrade base; \
		$(ALEMBIC) upgrade head; \
		echo "✅ Database reset completed"; \
	else \
		echo "❌ Reset cancelled"; \
	fi

db-status: ## 📊 Muestra estado de migraciones
	@echo "📊 Current database status:"
	@echo ""
	$(ALEMBIC) current --verbose
	@echo ""
	@echo "📜 Migration history:"
	$(ALEMBIC) history

# Pipeline completo
pipeline: ## 🚀 Pipeline completo (Download → Process → Harmonize)
	$(PYTHON) -m src.main pipeline

pipeline-force: ## 🚀 Pipeline completo con force mode
	$(PYTHON) -m src.main pipeline --force

pipeline-steps: download process harmonize ## 🚀 Pipeline paso a paso
	@echo "✨ Pipeline completado"

pipeline-full: setup download process harmonize validate ## 🎯 Pipeline completo con setup
	@echo "✨ Pipeline completo con todas las etapas"

# Desarrollo
dev: setup ## 🛠️  Setup desarrollo
	@echo "✅ Listo para desarrollo"

# Testing
test: ## 🧪 Ejecuta todos los tests
	uv run pytest

test-unit: ## ⚡ Tests unitarios (rápidos)
	uv run pytest -m unit

test-integration: ## 🔗 Tests de integración (con BD)
	uv run pytest -m integration

test-db: ## 💾 Tests de base de datos
	uv run pytest -m db

test-cov: ## 📊 Tests con cobertura
	uv run pytest --cov=src/db --cov-report=html --cov-report=term
	@echo "📈 Reporte de cobertura en: htmlcov/index.html"

test-verbose: ## 🔍 Tests en modo verbose
	uv run pytest -vv

test-fast: ## 🚀 Tests rápidos (solo unit, sin slow)
	uv run pytest -m "unit and not slow"

# Phase 2 & 3 Testing
test-phase2: ## 🧪 Test Phase 2 components
	@echo "🧪 Testing Phase 2 Components..."
	$(PYTHON) test_phase2.py

test-phase3: ## 🔗 Test Phase 3 integration
	@echo "🔗 Testing Phase 3 Integration..."
	$(PYTHON) test_phase3_integration.py

test-all-phases: ## 🎯 Test all phases
	@echo "🎯 Running all phase tests..."
	$(PYTHON) test_phase2.py
	$(PYTHON) test_phase3_integration.py
	@echo "✅ All phase tests completed"
