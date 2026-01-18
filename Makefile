# Variables
PYTHON := python
MAIN := scripts/main.py
# Usamos export para que PYTHONPATH esté disponible en todos los comandos
export PYTHONPATH := .

.PHONY: help etl-download etl-process etl-process-force etl-status clean

help:
	@echo "Thesis ETL Pipeline - Available commands:"
	@echo "  make etl-download      - Step 1: Download RAW ZIPs"
	@echo "  make etl-process       - Step 2: Process new/failed ZIPs"
	@echo "  make etl-process-force - Step 2: Re-process ALL ZIPs"
	@echo "  make etl-status        - Show reports.db summary"

## --- STEP 1: DOWNLOAD ---
etl-download:
	@echo "🕷️  Running Inegi Downloader..."
	uv run $(PYTHON) $(MAIN) download-inegi --headless

## --- STEP 2: PROCESSING ---
etl-process:
	@echo "⚙️  Processing DENUE data (Normal Mode)..."
	uv run $(PYTHON) $(MAIN) process-denue

etl-process-force:
	@echo "⚠️  FORCE MODE: Re-processing all files..."
	uv run $(PYTHON) $(MAIN) process-denue --force

## --- UTILS ---
etl-status:
	@echo "📊 ETL Telemetry Summary:"
	@uv run $(PYTHON) -c "import duckdb, json; \
		conn = duckdb.connect('data/reports.db', read_only=True); \
		res = conn.execute('SELECT process_status, count(*) FROM denue_raw GROUP BY 1').fetchall(); \
		print('\n[STATUS SUMMARY]'); \
		[print(f'  {row[0]}: {row[1]} files') for row in res];"

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +