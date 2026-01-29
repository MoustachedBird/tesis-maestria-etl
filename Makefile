.PHONY: help setup download process normalize harmonize validate clean db-migrate db-downgrade db-seed db-reset
.DEFAULT_GOAL := help

# Variables
PYTHON := uv run python
WAREHOUSE := data/denue_warehouse.db
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

download: ## ⬇️  Descarga datos INEGI
	$(PYTHON) -m scripts.main download-inegi

process: ## ⚙️  Procesa archivos RAW → Warehouse
	$(PYTHON) -m scripts.main process

process-parallel: ## ⚡ Procesa en paralelo
	$(PYTHON) -m scripts.main process --parallel

normalize: ## 📅 Normaliza fechas
	$(PYTHON) -m scripts.main normalize-dates

harmonize: ## 🔗 Ejecuta harmonización
	$(PYTHON) -m scripts.main harmonize

validate: ## ✅ Valida warehouse
	$(PYTHON) -m scripts.main stats

clean: ## 🧹 Limpia cache
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true

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
pipeline: download process normalize harmonize validate ## 🚀 Pipeline completo
	@echo "✨ Pipeline completado"

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
