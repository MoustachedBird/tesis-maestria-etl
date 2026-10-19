.PHONY: help setup extract transform load pipeline build-entidades clean test \
	db-up db-down db-logs psql db-config db-destroy db-migrate db-downgrade db-reset db-status
.DEFAULT_GOAL := help

# ─── Runner: nativo o contenedor ─────────────────────────────────────────────
# En la workstation Linux NADA corre nativo: no hay uv ni venv en el host, todo
# pasa por Docker. En la Mac de desarrollo sí se corre nativo.
#
#   make test              -> uv run pytest            (Mac)
#   DOCKER=1 make test     -> docker compose run tools pytest  (Linux)
#
# Para no escribir DOCKER=1 en cada comando, en la Linux conviene exportarlo
# una vez por sesión:  export DOCKER=1
ifdef DOCKER
RUN := docker compose run --rm tools
else
RUN := uv run
endif

# Variables
PYTHON := $(RUN) python
SILVER_DB := data/silver/mipymes.duckdb
GOLD_DB := data/gold/mipymes.duckdb
METADATA_DB := data/metadata.duckdb
ALEMBIC := $(RUN) alembic
PYTEST := $(RUN) pytest
RUFF := $(RUN) ruff

help: ## Show help
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@echo "DENUE ETL Pipeline"
	@echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'
	@echo ""

setup: ## Install dependencies (nativo); con DOCKER=1 construye las imagenes
ifdef DOCKER
	docker compose build etl tools
else
	uv sync
endif

docker-build: ## Construye/reconstruye las imagenes del ETL y de herramientas
	docker compose build etl tools

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

# ─── Postgres (contenedor) ───────────────────────────────────────────────────

# El servicio `etl` esta bajo el profile `manual`, asi que `up` solo levanta
# la base y lo que sea daemon. Los limites de recursos salen del .env.

db-up: ## Levanta Postgres y espera a que este listo
	docker compose up -d postgres
	@echo "Esperando a que Postgres acepte conexiones..."
	@until docker compose exec -T postgres pg_isready -q 2>/dev/null; do sleep 1; done
	@echo "Postgres listo en localhost:$${POSTGRES_PORT:-5432}"

db-down: ## Detiene Postgres (conserva el volumen de datos)
	docker compose down

db-logs: ## Sigue los logs de Postgres
	docker compose logs -f postgres

psql: ## Abre una sesion psql en la base
	docker compose exec postgres psql -U $${POSTGRES_USER:-etl_rw} -d $${POSTGRES_DB:-mipymes}

db-create-test: ## Crea la base de pruebas (<POSTGRES_DB>_test), idempotente
	@docker compose exec -T postgres psql -U $${POSTGRES_USER:-etl_rw} -d $${POSTGRES_DB:-mipymes} \
		-tc "SELECT 1 FROM pg_database WHERE datname = '$${POSTGRES_DB:-mipymes}_test'" \
		| grep -q 1 \
		&& echo "La base $${POSTGRES_DB:-mipymes}_test ya existe." \
		|| docker compose exec -T postgres createdb -U $${POSTGRES_USER:-etl_rw} \
			"$${POSTGRES_DB:-mipymes}_test" && echo "Base de pruebas lista."

db-config: ## Muestra el tuning efectivo de Postgres (lo que quedo aplicado)
	@docker compose exec -T postgres psql -U $${POSTGRES_USER:-etl_rw} -d $${POSTGRES_DB:-mipymes} \
		-c "SELECT name, setting, unit FROM pg_settings WHERE name IN \
		('shared_buffers','effective_cache_size','maintenance_work_mem','work_mem', \
		 'max_parallel_workers','max_parallel_workers_per_gather','random_page_cost', \
		 'effective_io_concurrency','max_wal_size','max_locks_per_transaction') ORDER BY name;"

db-destroy: ## BORRA el volumen de datos de Postgres (pide confirmacion)
	@echo "ATENCION: esto borra TODOS los datos de Postgres (volumen etl-pgdata)."
	@read -p "Escribe 'borrar' para confirmar: " ans; \
	if [ "$$ans" = "borrar" ]; then \
		docker compose down -v; \
		echo "Volumen eliminado."; \
	else \
		echo "Cancelado."; \
	fi

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
	$(PYTEST)

test-postgres: ## Solo los tests marcados `postgres` (requieren servidor)
	$(PYTEST) -m postgres

smoke-fase3: ## Valida la Fase 3 contra Postgres (particiones, COPY, staging, indices)
	$(PYTHON) -m scripts.smoke_fase3

test-unit: ## Unit tests (fast)
	$(PYTEST) -m unit

test-integration: ## Integration tests (with DB)
	$(PYTEST) -m integration

test-db: ## Database tests
	$(PYTEST) -m db

test-cov: ## Tests with coverage
	$(PYTEST) --cov=src --cov-report=html --cov-report=term
	@echo "Coverage report at: htmlcov/index.html"

test-verbose: ## Tests in verbose mode
	$(PYTEST) -vv

test-fast: ## Fast tests (unit only, no slow)
	$(PYTEST) -m "unit and not slow"

lint: ## Run ruff linter
	$(RUFF) check src/ tests/ scripts/ alembic/

lint-fix: ## Auto-fix lint issues
	$(RUFF) check src/ tests/ scripts/ alembic/ --fix

format: ## Format code with ruff
	$(RUFF) format src/ tests/ scripts/ alembic/
