.PHONY: help setup download process normalize harmonize validate clean
.DEFAULT_GOAL := help

# Variables
PYTHON := uv run python
WAREHOUSE := data/denue_warehouse.db

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

# Pipeline completo
pipeline: download process normalize harmonize validate ## 🚀 Pipeline completo
	@echo "✨ Pipeline completado"

# Desarrollo
dev: setup ## 🛠️  Setup desarrollo
	@echo "✅ Listo para desarrollo"

pipeline: download process normalize generate-seeds load-seeds harmonize validate ## 🚀 Pipeline completo
