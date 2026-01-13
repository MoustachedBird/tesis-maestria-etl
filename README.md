# SME Survival Analysis - Backend Core

This repository contains the business logic, data engineering pipelines, and the REST API for the thesis project regarding the mortality and survival analysis of Small and Medium Enterprises (SMEs) in Mexico, utilizing microdata from INEGI.

## Architecture

The project follows a simplified Hexagonal Architecture (Src Layout) to decouple scientific logic from web infrastructure.

- **Language:** Python 3.12+
- **Package Manager:** uv
- **API Framework:** FastAPI
- **Data Engine:** DuckDB (OLAP SQL) + Polars (High-performance DataFrames)
- **Storage Format:** Apache Parquet (Partitioned by year)

## Project Structure

    src/tesis_core/
    ├── domain/           # Pydantic Models & Business Logic
    ├── infrastructure/   # Database connections & ML Model Loading
    └── interface/        # FastAPI Endpoints & CLI Commands

## Installation & Setup

### Prerequisites
- Python 3.12 or higher
- [uv](https://github.com/astral-sh/uv) (An extremely fast Python package installer and resolver)

### Steps

1. **Clone the repository:**

       git clone <your-gitea-url>/backend-core.git
       cd backend-core

2. **Install dependencies:**
   This project uses uv for dependency management.

       uv sync

3. **Environment Variables:**
   Create a .env file in the root directory based on the example:

       cp .env.example .env

## Data Management (Local Data Lake)

**IMPORTANT:** Raw INEGI microdata and processed .parquet files are **NOT** included in this repository due to size and privacy constraints.

The expected local structure is:
- /data/raw/: Place original CSV files from INEGI here.
- /data/processed/: Optimized Parquet files will be generated here.
- /data/models/: Serialized ML models (.pkl/.onnx).

To run the ETL pipeline (raw -> parquet conversion):

    uv run python -m src.tesis_core.interface.cli.etl --input data/raw --output data/processed

## Running the Server

To start the API in development mode:

    uv run fastapi dev src/tesis_core/interface/api/main.py

Interactive documentation will be available at: http://localhost:8000/docs

## Testing

To execute unit and integration tests:

    uv run pytest
