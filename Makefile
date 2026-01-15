# ==============================================================================
#  Tesis Backend Core - Makefile (Linux & macOS Compatible)
# ==============================================================================

.PHONY: help install api etl-download etl-retry etl-process docker-build docker-download docker-process clean check-os

# ------------------------------------------------------------------------------
#  Global Configuration
# ------------------------------------------------------------------------------
SHELL := /bin/bash

# PYTHONPATH=. ensures Python finds the 'scripts' module locally
PYTHON := PYTHONPATH=. uv run python
DOCKER_COMPOSE := docker compose

# Default arguments
HEADLESS ?= true
RAW_DIR ?= data/raw/denue
PROCESSED_DIR ?= data/processed/denue

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

install: ## Install dependencies with uv
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

etl-download: ## Step 1: Download Raw Data. Usage: make etl-download HEADLESS=false
	@echo "🕷️  Running Inegi Downloader..."
	$(PYTHON) scripts/main.py download-inegi $(if $(filter true,$(HEADLESS)),--headless,--no-headless) --output=$(RAW_DIR)

etl-retry: ## Step 1.5: Retry failed downloads. Usage: make etl-retry
	@echo "🔄 Retrying downloads in $(RAW_DIR)..."
	$(PYTHON) scripts/main.py retry-inegi --output=$(RAW_DIR)

etl-process: ## Step 2: Process ZIPs to Parquet (Polars). Usage: make etl-process
	@echo "⚙️  Processing DENUE data (High Performance Mode)..."
	$(PYTHON) scripts/main.py process-denue --input-dir=$(RAW_DIR) --output-dir=$(PROCESSED_DIR)

# ------------------------------------------------------------------------------
#  4. Docker ETL Execution (The "Pro" Way)
# ------------------------------------------------------------------------------

docker-build: ## Re-build the Docker ETL image (Run this after adding libs)
	@echo "🔨 Building Docker image..."
	$(DOCKER_COMPOSE) build

docker-download: ## Run Downloader inside Docker
	@echo "🐳 Running Downloader in Docker..."
	$(DOCKER_COMPOSE) run --rm etl download-inegi

docker-process: ## Run Processor inside Docker
	@echo "🐳 Running Processor in Docker..."
	$(DOCKER_COMPOSE) run --rm etl process-denue

# ------------------------------------------------------------------------------
#  5. Maintenance
# ------------------------------------------------------------------------------

clean: ## Clean cache files (.pyc, __pycache__) safely
	@echo "🧹 Cleaning up..."
	@find . -type d -name "__pycache__" -exec rm -rf {} +
	@find . -type f -name "*.pyc" -exec rm -f {} +
	@find . -type f -name "*.DS_Store" -exec rm -f {} +
	@echo "✅ Clean complete."