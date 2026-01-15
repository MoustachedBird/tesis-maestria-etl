# ==============================================================================
#  Tesis Backend Core - Makefile (Linux & macOS Compatible)
# ==============================================================================

.PHONY: help install api etl-download etl-retry docker-build docker-download clean check-os

# ------------------------------------------------------------------------------
#  Global Configuration
# ------------------------------------------------------------------------------
SHELL := /bin/bash

# PYTHONPATH=. ensures Python finds the 'scripts' module locally
PYTHON := PYTHONPATH=. uv run python
DOCKER_COMPOSE := docker compose

# Default arguments
HEADLESS ?= true
OUTPUT ?= data/raw/denue

# ------------------------------------------------------------------------------
#  1. Help & Utilities
# ------------------------------------------------------------------------------

help: ## Show this help message
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@grep -E '^[a-zA-Z0-9_-]+:.*## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

check-os: ## Print detected OS information
	@echo "🖥️  Detected OS: $$(uname -s)"
	@echo "🐚 Shell: $(SHELL)"

install: ## Install dependencies with uv (including ETL group)
	@echo "📦 Installing dependencies..."
	uv sync --all-groups

# ------------------------------------------------------------------------------
#  2. API Development
# ------------------------------------------------------------------------------

api: ## Run FastAPI server in development mode
	@echo "🚀 Starting FastAPI server..."
	uv run fastapi dev app/main.py

# ------------------------------------------------------------------------------
#  3. Local ETL Execution (Runs on your machine)
# ------------------------------------------------------------------------------

etl-download: ## Run ETL Download locally. Usage: make etl-download HEADLESS=false
	@echo "🕷️  Running Inegi Downloader locally..."
	$(PYTHON) scripts/main.py download-inegi $(if $(filter true,$(HEADLESS)),--headless,--no-headless) --output=$(OUTPUT)

etl-retry: ## Retry failed downloads from 'report_download.csv'. Usage: make etl-retry
	@echo "🔄 Retrying downloads from report in $(OUTPUT)..."
	$(PYTHON) scripts/main.py retry-inegi --output=$(OUTPUT)

# ------------------------------------------------------------------------------
#  4. Docker ETL Execution (The "Pro" Way)
# ------------------------------------------------------------------------------

docker-build: ## Build the Docker ETL image
	@echo "🔨 Building Docker image..."
	$(DOCKER_COMPOSE) build

docker-download: ## Run ETL inside Docker. Usage: make docker-download
	@echo "🐳 Running Downloader inside Docker..."
	$(DOCKER_COMPOSE) run --rm etl download-inegi

docker-debug: ## Run ETL inside Docker showing the browser (No-Headless)
	@echo "🐳 Running Downloader inside Docker (Visible Mode)..."
	@echo "⚠️  Note: This requires X11/Display configuration on macOS/Linux to be visible."
	$(DOCKER_COMPOSE) run --rm etl download-inegi --no-headless

# ------------------------------------------------------------------------------
#  5. Maintenance
# ------------------------------------------------------------------------------

clean: ## Clean cache files (.pyc, __pycache__) safely
	@echo "🧹 Cleaning up..."
	@find . -type d -name "__pycache__" -exec rm -rf {} +
	@find . -type f -name "*.pyc" -exec rm -f {} +
	@find . -type f -name "*.DS_Store" -exec rm -f {} +
	@echo "✅ Clean complete."