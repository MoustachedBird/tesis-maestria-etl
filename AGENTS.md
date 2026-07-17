# AGENTS.md - Development Guide for ETL Core

## IMPORTANT: Destructive Operations Policy

**NEVER delete, truncate, or overwrite data files (databases, parquets, etc.) without explicit user confirmation first.** This includes:
- Removing `.duckdb`, `.db`, or `.parquet` files
- Running `rm -f` on any data file
- Dropping or truncating database tables with data
- Any operation that could cause data loss

Always ask the user before executing any destructive operation, even if it seems like a necessary intermediate step.

---

## Fixing Missing Localities (FK Violations)

When `make transform` fails with a foreign key constraint error like:
```
Constraint Error: Violates foreign key constraint because key
"cve_ent: XX, cve_mun: XXX, cve_loc: XXXX" does not exist in the referenced table
```

Follow this procedure:

1. **Find the locality name** from the CSV that failed:
   - Open the failing ZIP, read the data CSV (under `conjunto_de_datos/`)
   - Filter for the missing `(cve_ent, cve_mun, cve_loc)` combination
   - Extract the `localidad` column value

2. **Insert into Silver DuckDB** (do NOT delete/truncate anything):
   ```sql
   INSERT INTO denue_localidades (cve_ent, cve_mun, cve_loc, localidad)
   VALUES ('XX', 'XXX', 'XXXX', 'NOMBRE EN MAYÚSCULAS');
   ```
   If DuckDB accepts the INSERT (no PK violation), the locality is valid.

3. **Add to seed parquet** (`data/seeds/seed_localidades.parquet`):
   - Read the parquet with Polars
   - Append the new row (locality name in UPPERCASE)
   - Write back, sorted by `(cve_ent, cve_mun, cve_loc)`

4. **Re-run** `make transform` to continue processing.

**Important:** All locality names in the seed must be UPPERCASE. Always apply `.upper()` before inserting.

---

## Normalize-Dates Step

The transform pipeline has 3 ordered sub-steps: **ingest -> normalize-dates -> clee-resolution**.

The `normalize-dates` step parses `fecha_alta_str` (raw string from INEGI) into `fecha_alta_date` (DATE type, format `YYYY-MM-01`) directly in the Silver DuckDB via SQL UPDATE, one period at a time.

### 6 Known fecha_alta_str Formats

| Format | Example | Rows | Pattern |
|--------|---------|------|---------|
| YYYY-MM | "2010-07" | ~68M | Most common |
| YYYY-MM (trailing space) | "2011-03 " | 15,192 | Strip then parse |
| YYYY MM (space separator) | "2013 07" | 7,383 | Replace space with - |
| YYYY_MM (underscore) | "2022_11" | 10 | Replace _ with - |
| MES_ESPANOL ANNO | "JULIO 2010" | ~14.5M | Map Spanish month -> number |
| DD/MM/YYYY | "06/10/2023" | 12 | Extract year-month |

Plus 1 NULL row. Output format is always `DATE YYYY-MM-01`.

### Tracking

- Tracked per period (not per file) via `normalize_tasks` table in metadata DB
- Incremental by default: only processes periods not yet marked SUCCESS
- With `--force`, re-normalizes all periods
- If any `fecha_alta_date` remains NULL after UPDATE, the distinct unrecognized values are logged as errors

### CLI Usage

```bash
# Run only normalize-dates (skip ingest and clee-resolution)
uv run python -m src_etl.main transform --no-ingest --normalize-dates --no-clee-resolution

# Force re-normalize all periods
uv run python -m src_etl.main transform --no-ingest --normalize-dates --no-clee-resolution --force

# Full transform (ingest + normalize-dates + clee-resolution, all on by default)
uv run python -m src_etl.main transform
```

### Tips

- `SELECT DISTINCT column` is an efficient way to analyze patterns in columns with millions of rows. Use it to discover new fecha_alta_str formats if they appear in future INEGI releases.
- The Spanish month map is defined in `src_etl/transformers/date_normalizer.py` as `SPANISH_MONTHS`.
- DuckDB `regexp_matches()` + `CASE WHEN` handles all format detection in a single UPDATE statement per period.

---

## CLEE Resolution Step (Entity Resolution)

The transform pipeline has 3 ordered sub-steps: **ingest -> normalize-dates -> clee-resolution**. This section documents the CLEE resolution stage in detail.

### Purpose

CLEE resolution links establishments across temporal snapshots of DENUE. INEGI assigns CLEE codes starting in 2021, but older periods (2015-2020) lack them. This stage propagates CLEEs from recent periods backward through time using spatial + fuzzy matching, enabling longitudinal tracking of SME survival and mortality.

### Two-Phase Architecture

The resolver (`CLEEResolver.process_national()` in `clee_resolver.py:387`) runs two sequential phases:

**Phase 1 -- Deterministic Resolution** (`_resolve_deterministic_periods`, line 209):
- Identifies periods where INEGI assigned CLEE codes to 100% of rows (2021+ data).
- Performs a simple SQL `UPDATE establecimientos SET clee_propagado_radio = clee` for all rows with non-null `clee`.
- These periods become the initial **resolved set** -- the anchor references for Phase 2.
- No BallTree, no fuzzy matching -- purely deterministic copy.

**Phase 2 -- Probabilistic Resolution** (`_resolve_probabilistic_periods`, line 266):
- Processes older periods (2015-2020) that lack CLEE codes from INEGI.
- Uses a **sliding-window approach with an expanding reference set** (see below).
- After exhausting all references, unresolved rows get a synthetic ID: `NEW_{prefix}-{uuid_hex_12}`.
- Results are written to DuckDB immediately; the period then joins the resolved set.

### Sliding Window with Expanding Reference Set

This is the core mechanism. It is **not** a fixed-size window nor a monolithic KB.

```
resolved_periods = [deterministic periods from Phase 1]  # e.g., 2021-2024

FOR each target_period P in probabilistic_periods (newest-first):
    1. Load P from Silver DuckDB (all rows where clee_propagado_radio IS NULL)

    2. FOR each ref_period R in resolved_periods (newest-first):
        a. If P has 0 remaining unresolved rows -> BREAK early
        b. Load R from DuckDB
        c. Build ephemeral KB from R  (build_single_period_kb)
        d. Free R from RAM  (del + gc.collect)
        e. propagate_clees(target=P, ref_kb=KB_R, spatial_tol=500m)
           -> Partially fills clee_propagado_radio on P's matched rows
        f. Free KB_R from RAM  (del + gc.collect)

    3. assign_new_clees() for any rows still unresolved
    4. Write P's clee_propagado_radio values to DuckDB (50K-row batches via temp table)
    5. resolved_periods.append(P)  # P is now available as reference
    6. Free P from RAM
```

Key characteristics:
- **Direction**: future-to-past (newest periods resolved first, propagated backward)
- **Expanding reference**: after each target period resolves, it joins `resolved_periods`, so subsequent targets have more references
- **One reference in RAM at a time**: peak RAM capped at ~5-8 GB by loading/discarding each reference individually
- **Exhaustive comparison**: each target is compared against ALL resolved periods (not a fixed N-period window)
- **Early termination**: inner loop breaks when target has 0 pending rows (`clee_resolver.py:339`)

### Ephemeral KB Construction

The KB is built fresh from a **single reference period** each time -- never accumulated across periods:

`build_single_period_kb(df_ref)` (`entity_resolution.py:166`):
- Filters to rows with valid `clee_propagado_radio` (non-null, non-empty)
- Selects only 10 columns (`KB_COLUMNS`): `clee_propagado_radio`, `latitud`, `longitud`, `nom_estab`, `raz_social`, `nombre_act`, `tel_clean`, `email_clean`, `www_clean`, `cod_postal`
- Returns a lightweight Polars DataFrame -- no group-by, no aggregation
- Each row in the reference period is one row in the KB

### Cascade Matching Algorithm

`propagate_clees()` (`entity_resolution.py:326`) performs matching in three steps:

1. **Spatial pre-filter**: BallTree (haversine) queries target coords against reference KB within 500m radius
2. **Activity gate** (mandatory): SCIAN activity fuzzy score > 60 -- candidates below this are rejected
3. **Match paths** (one must pass):
   - **Path A**: Razon social (legal name) fuzzy > 80
   - **Path B**: Business name fuzzy > 75
   - **Path C**: 2+ combined weak signals from: partial name (>60), phone, email, web, postal code

Parallelization: datasets > 50K rows use `multiprocessing.Pool` with fork() + copy-on-write KB sharing. Below that threshold, sequential processing is used.

### Configuration Thresholds

Defined in `clee_resolution_config.py`. **All calibration/resource knobs are
env-configurable** (same name in `.env`, defaults in code, each documented
with a docstring and in `.env.example`):

| Variable (env) | Default | Purpose |
|----------|---------|---------|
| `SPATIAL_RADIUS_M` | 500 | Candidate radius in meters (BOTH methods: BallTree and H3 blocking) |
| `ACTIVITY_GATE_THRESH` | 60 | Mandatory SCIAN activity fuzzy threshold |
| `BUSINESS_NAME_MATCH_THRESH` | 75 | Direct match: business name fuzzy |
| `LEGAL_NAME_MATCH_THRESH` | 80 | Direct match: legal name (razon social) |
| `MIN_COMBINED_SIGNALS` | 2 | Combined match: minimum weak signals |
| `PARTIAL_NAME_THRESH` | 60 | Partial name signal threshold |
| `MATCH_WORKERS` | 8 | Multiprocessing worker count |
| `MATCH_PARALLEL_THRESHOLD` | 50,000 | Minimum rows to activate multiprocessing |
| `KB_MAX_ROWS_PER_PARTITION` | 1,500,000 | Max KB rows before partitioning (main RAM knob) |

Structural constants that are deliberately **NOT** env vars (changing them
requires migration + recompute; see their docstrings):

| Constant | Value | Purpose |
|----------|-------|---------|
| `H3_BLOCKING_RES` | 9 | Resolution materialized in `h3_cell_9` (~174m edge) |
| `H3_PARTITION_RES` | 5 | Resolution materialized in `h3_cell_5` (~8.5km edge); reserved for KB partition pruning, not yet consumed |
| `H3_GRID_DISK_K` | 3 | grid_disk rings for H3 candidates; coupled to `SPATIAL_RADIUS_M` (coverage guard at import) |
| `_EARTH_RADIUS_M` (`matching.py`) | 6,371,000 | Physical constant shared by both haversines |

### Two propagation methods (side-by-side columns)

H3 blocking does NOT replace the radius/BallTree method — it is a
complementary method filling its own column for thesis comparison:
`CLEEResolver(method="radio")` writes `clee_propagado_radio`
(`find_spatial_neighbors`, BallTree) and `CLEEResolver(method="h3")` writes
`clee_propagado_h3` (`find_h3_neighbors`, grid_disk + exact haversine
post-filter). Both share the sliding-window engine and matching cascade
(`_propagate_clees` core + `propagate_clees_radio`/`propagate_clees_h3`
wrappers). `h3_cell_9` feeds H3 blocking; `h3_cell_5` = parent(h3_cell_9),
stored for future KB partition pruning (currently unused, near-zero cost).

### Key Functions Reference

| Function | File:Line | Role |
|----------|-----------|------|
| `CLEEResolver.run()` | `clee_resolver.py:435` | Entry point. Creates metadata, invokes `process_national()` |
| `process_national()` | `clee_resolver.py:387` | Orchestrator. Phase 1 then Phase 2 |
| `_resolve_deterministic_periods()` | `clee_resolver.py:209` | Phase 1: `UPDATE clee_propagado_radio = clee` |
| `_resolve_probabilistic_periods()` | `clee_resolver.py:266` | Phase 2: sliding window loop |
| `_get_sorted_periods()` | `clee_resolver.py:93` | Returns all periods sorted newest-first |
| `_get_periods_with_clee_coverage()` | `clee_resolver.py:111` | Classifies periods as deterministic vs. probabilistic |
| `_load_period()` | `clee_resolver.py:132` | Loads one period from Silver DuckDB |
| `_write_period_results()` | `clee_resolver.py:166` | Writes clee_propagado_radio back to DuckDB in 50K batches |
| `build_single_period_kb()` | `entity_resolution.py:168` | Builds ephemeral KB from one reference period |
| `_propagate_single_kb()` | `entity_resolution.py:328` | Matching engine for one KB partition (BallTree + cascade) |
| `propagate_clees()` | `entity_resolution.py:429` | Orchestrator: partitions KB if needed, delegates to `_propagate_single_kb` |
| `assign_new_clees()` | `entity_resolution.py:542` | Assigns `NEW_{prefix}-{uuid12}` to unmatched rows |
| `find_spatial_neighbors()` | `matching.py:40` | BallTree haversine spatial search |
| `calculate_match_score()` | `matching.py:55` | Cascade matching: gate -> Path A/B/C |

### CLI Usage

```bash
# Run only CLEE resolution (skip ingest and normalize-dates)
uv run python -m src_etl.main transform --no-ingest --no-normalize-dates --clee-resolution

# Force re-resolve all periods (resets clee_propagado_radio to NULL first)
uv run python -m src_etl.main transform --no-ingest --no-normalize-dates --clee-resolution --force

# Or via Makefile
make clee-resolution

# Full transform (ingest + normalize-dates + clee-resolution, all on by default)
uv run python -m src_etl.main transform
```

### Tips

- The sliding window processes periods **newest-first** (`ORDER BY period DESC`). This maximizes the chance of matching against data that already has INEGI-assigned CLEEs.
- Peak RAM is controlled by loading only one reference period at a time (~5-8 GB). If RAM is a concern, monitor via `_count_unresolved()` progress logging.
- The `--force` flag resets ALL `clee_propagado_radio` to NULL and re-runs from scratch. Use only when the matching algorithm changes or seed data is corrected.
- Synthetic IDs (`NEW_*`) are stable within a single run but NOT across runs with `--force`. Do not use them as permanent foreign keys in downstream systems.
- The inner loop's early termination (`still_missing == 0`) significantly speeds up recent periods that have near-complete CLEE coverage from INEGI.

---

## Project Overview

**Name:** `tesis-core` (v0.1.0)
**Purpose:** ETL pipeline analyzing mortality and survival of SMEs in Mexico using INEGI's DENUE microdata.

- **Language:** Python 3.12+
- **Package Manager:** uv
- **Frameworks:** FastAPI (API), Typer (CLI), SQLAlchemy 2.0 (ORM), Alembic (migrations)
- **Data Engine:** DuckDB (OLAP) + Polars (DataFrames)
- **Storage:** DuckDB (Silver + Gold warehouses), Apache Parquet (seeds)
- **Architecture:** Hexagonal/Clean Architecture with medallion data lake (Bronze/Silver/Gold)
- **Design Decision:** Monolithic ETL -- YAGNI; single service doesn't justify microservices

---

## Directory Layout

```
etl-core/
├── src_etl/                       # ETL application code
│   ├── main.py                    # CLI entry point (Typer)
│   ├── core/                      # Configuration and infrastructure
│   │   ├── config.py              # App settings (pydantic-settings)
│   │   ├── download_config.py     # Download settings (retries, timeouts)
│   │   └── logging.py             # Structured logging (structlog)
│   ├── db/                        # Database layer
│   │   ├── models.py              # SQLAlchemy ORM models (5 tables, 40 cols)
│   │   ├── database.py            # Engine/session management
│   │   ├── enums.py               # DENUE enums (TipoVial, PersonalOcupado, etc.)
│   │   └── seed_loader.py         # Seed orchestrator (delegates to loaders/)
│   ├── extractors/                # Domain-specific data extraction
│   │   └── denue.py               # DENUE orchestrator (Selenium + parallel)
│   ├── transformers/              # Transformation layer
│   │   ├── denue.py               # Unified orchestrator (DenueTransformer)
│   │   ├── processor.py           # TransformStage: ZIP -> Silver DuckDB
│   │   ├── clee_resolver.py        # CLEEResolver: Entity resolution across time
│   │   └── _legacy/               # Archived old processor/clee_resolver
│   ├── loaders/                   # Data loading layer
│   │   ├── loader.py              # LoaderStage: Silver -> Gold (file copy)
│   │   ├── denue.py               # DataStoreManager (DuckDB upsert)
│   │   ├── geography.py           # Geography seeds (entidades, municipios, localidades)
│   │   └── scian.py               # SCIAN economic activity seeds
│   └── utils/                     # Shared utilities
│       ├── browser.py             # Selenium Chrome driver setup
│       ├── denue_schema.py        # DENUE column mappings, enums, aliases
│       ├── denue_scraping.py      # DENUE web scraping + filename sanitization
│       ├── downloader.py          # Generic HTTP downloader with retry
│       ├── entity_resolution.py   # Cascade matching algorithm engine
│       ├── file_validator.py      # SHA256, size, ZIP validation
│       ├── clee_resolution_config.py # CLEE resolution regions/schema config
│       ├── matching.py            # Spatial + fuzzy matching utilities
│       └── warehouse.py           # DuckDB BaseWarehouse class
│
├── src_api/                       # FastAPI REST API (separated)
│   ├── __init__.py
│   ├── main.py                    # FastAPI app with lifespan, CORS
│   └── schemas.py                 # Pydantic request/response models
│
├── src_metadata/                  # Metadata tracking service (separated)
│   ├── __init__.py
│   ├── models.py                  # Pydantic models (JobRun, tasks, lineage)
│   ├── schema.py                  # DDL for metadata.duckdb
│   └── tracker.py                 # MetadataTracker class
│
├── data/
│   ├── bronze/                    # Raw downloaded ZIPs (not committed)
│   ├── silver/                    # Processed intermediate (not committed)
│   ├── gold/                      # Final DuckDB warehouse (not committed)
│   ├── seeds/                     # Parquet seed files (committed)
│   │   ├── seed_entidades.parquet       # 32 states
│   │   ├── seed_municipios.parquet      # 2,478 municipalities
│   │   ├── seed_localidades.parquet     # 39,140 localities
│   │   └── seed_actividades.parquet     # 1,127 economic activities
│
├── alembic/                       # Database migrations
├── tests/
│   ├── conftest.py                # Fixtures (manual + real DB)
│   ├── unit/                      # Unit tests
│   └── integration/               # Integration tests
├── AGENTS.md                      # This file
├── Makefile                       # Task runner (30+ targets)
├── Dockerfile.etl                 # Container with Chrome for scraping
├── docker-compose.yml             # Docker services
├── pyproject.toml                 # Dependencies and config
└── uv.lock                        # Locked dependencies
```

---

## Build Commands

```bash
# Environment
uv sync                                    # Install all dependencies

# Run CLI
uv run python -m src_etl.main             # Main CLI entry point
uv run fastapi dev src_api/main.py        # Dev server (FastAPI)

# Linting
uv run ruff check src_etl/ src_metadata/ src_api/ tests/        # Lint
uv run ruff check src_etl/ src_metadata/ src_api/ tests/ --fix   # Auto-fix
uv run ruff format src_etl/ src_metadata/ src_api/ tests/        # Format

# Testing
uv run pytest                              # All tests
uv run pytest -m unit                      # Unit only
uv run pytest -m "not slow"                # Skip slow
uv run pytest -m "db and not slow"         # DB tests, skip slow
uv run pytest -vv                          # Verbose
```

---

## Makefile Targets

```bash
# ETL Pipeline
make extract                     # Extract data from INEGI (Bronze)
make extract ARGS="--force"      # Force re-download all files
make transform                   # Transform raw data (Bronze -> Silver)
make transform-force             # Force reprocess all
make load                        # Load Silver -> Gold
make clee-resolution             # CLEE Resolution (Entity Resolution)
make pipeline                    # Full: Extract -> Transform -> Load
make pipeline-force              # Full pipeline with force mode

# Database
make db-migrate                  # Run pending Alembic migrations
make db-downgrade                # Rollback last migration
make db-seed                     # Reload seed data (idempotent)
make db-reset                    # Full reset (destructive, asks confirmation)
make db-status                   # Show migration status

# Metadata
make metadata-query              # Query recent job runs
make metadata-stats              # Show pipeline statistics

# Testing
make test                        # All tests
make test-unit / test-integration / test-db / test-fast
make test-cov                    # Tests with coverage report
make lint / lint-fix / format    # Ruff linting and formatting
```

---

## Architecture

### ETL Pipeline Flow

```
┌──────────────────────────────────────────────────────────────────────┐
│                   DENUE TRANSFORMER (Orchestrator)                    │
│  ┌────────────────┬─────────────────┬──────────────┬──────────────┐ │
│  │ TransformStage │ CLEEResolver    │ LoaderStage  │ Metadata     │ │
│  │ (ZIP->Silver)  │ (Entity Resoln) │ (Silver->Gold│ Tracker      │ │
│  └────────────────┴─────────────────┴──────────────┴──────────────┘ │
└──────────────────────────────────────────────────────────────────────┘

Data Flow:
  EXTRACT   -> download_tasks  (lineage: WEB -> FILE)
  TRANSFORM -> process_tasks   (lineage: FILE -> TABLE in Silver)
  LOAD      -> process_tasks   (lineage: Silver -> Gold file copy)
  CLEE_RESOLUTION -> clee_resolution_tasks (lineage: Silver DuckDB in-place)
```

### Metadata Layer

```
┌─────────────────────────────────────────────────────────────┐
│                 data/metadata.duckdb                         │
│  ┌────────────┬────────────────┬──────────────┐             │
│  │ job_runs   │ download_tasks │ data_lineage │             │
│  │ (run-level)│ (file-level)   │ (provenance) │             │
│  ├────────────┼────────────────┼──────────────┤             │
│  │ process_   │ clee_          │              │             │
│  │ tasks      │ resolution_    │              │             │
│  │            │ tasks          │              │             │
│  └────────────┴────────────────┴──────────────┘             │
└─────────────────────────────────────────────────────────────┘
```

### Storage Layer

```
data/bronze/denue/{period}/*.zip      # Raw downloads
data/silver/denue.duckdb              # Silver DuckDB warehouse (incl. clee_propagado_radio)
data/gold/denue.duckdb                # Gold DuckDB warehouse (production)
```

### Logging Layer

```
logs/downloads/YYYY-MM-DD/run_TIMESTAMP/
├── structured.jsonl     (machine-readable)
├── application.log      (human-readable)
├── errors.log           (errors only)
└── summary.json         (run statistics)
```

---

## Database

### ORM Models (9 tables)

| Model | Table | Primary Key | Rows |
|-------|-------|------------|------|
| `Establecimiento` | `establecimientos` | `id` (hash) | 100M+ |
| `ActividadEconomica` | `denue_actividades_economicas` | `codigo_act` | 1,127 |
| `EstratoPersonal` | `denue_estratos_personal` | `per_ocu` | 8 |
| `TipoVialidad` | `denue_tipos_vialidad` | `tipo_vial` | 39 |
| `TipoAsentamiento` | `denue_tipos_asentamiento` | `tipo_asent` | 45 |
| `TipoUnidadEconomica` | `denue_tipos_unidad_economica` | `tipounieco` | 3 |
| `Entidad` | `denue_entidades` | `cve_ent` | 32 |
| `Municipio` | `denue_municipios` | `(cve_ent, cve_mun)` | 2,478 |
| `Localidad` | `denue_localidades` | `(cve_ent, cve_mun, cve_loc)` | 39,140 |

### Domain normalization (no DuckDB ENUMs, no domain FKs on the fact table)

The old DuckDB ENUM column types (`tipo_vial_type`, `tipo_asent_type`,
`tipounieco_type`, `per_ocu_type`) were normalized away (migration
`ae66f32a9bed`): those columns are plain VARCHAR and their domain lives in
the `denue_tipos_*` / `denue_estratos_personal` catalog tables, seeded from
the enums in `src_etl/db/enums.py` (single source of truth, loaders
`dominios.py`/`estratos.py`).

Linking strategy:
- **Physical FKs**: only the 3 geography FKs (created with the table).
  Deliberately NO FK from `establecimientos` to the domain/SCIAN catalogs:
  every FK in DuckDB creates an implicit ART index on the referencing
  columns (~77M rows -> massive writes measured ~1000x slower), and DuckDB
  cannot `ALTER TABLE ... ADD CONSTRAINT` anyway.
- **Integrity**: `EstablecimientoWriter.validate_integrity()` fail-fast
  checks every ingested batch against `DOMAIN_CATALOGS` (derived from the
  same enums that seed the catalogs).
- **Navigation**: viewonly ORM relationships on `Establecimiento`
  (`actividad`, `estrato`, `tipo_vialidad`, `tipo_asentamiento`,
  `tipo_unidad`, `entidad`, `municipio`, `localidad`) declare the joins
  without emitting FK DDL. In SQL, JOIN by the label columns directly.

### Establecimiento (40 columns)

| Category | Columns |
|----------|---------|
| PK | `id` |
| Identification | `clee`, `clee_propagado_radio`, `nom_estab`, `raz_social` |
| Economic | `codigo_act`, `tipounieco`, `per_ocu` |
| Address | `tipo_vial`, `nom_vial`, `numero_ext`, `letra_ext`, `numero_int`, `letra_int`, `edificio`, `edificio_e` |
| Cross Streets | `tipo_v_e_1/2/3`, `nom_v_e_1/2/3` |
| Settlement | `tipo_asent`, `nomb_asent`, `nom_cencom`, `num_local`, `cod_postal` |
| Geographic | `cve_ent`, `cve_mun`, `cve_loc`, `ageb`, `manzana` |
| Coordinates | `latitud`, `longitud` |
| Contact | `telefono`, `correoelec`, `www` |
| Temporal | `fecha_alta_str`, `fecha_alta_date`, `period` |

### clee_propagado_radio Column

The `clee_propagado_radio` column stores the resolved entity identifier assigned by the CLEE resolution stage. It enables tracking establishments across temporal snapshots. See the **CLEE Resolution Step** section above for the full two-phase algorithm, sliding window mechanism, and cascade matching details.

- **Source**: Populated by `CLEEResolver` directly in Silver DuckDB via UPDATE
- **Values**:
  - Original CLEE (copied from `clee` when available -- Phase 1 deterministic)
  - Propagated CLEE (matched from a resolved reference period -- Phase 2 probabilistic)
  - Synthetic CLEE: `NEW_{REGION_PREFIX}-{uuid_hex_12}` for new entities without match
- **Force mode** (`--force`): Resets all `clee_propagado_radio` to NULL, re-seeds from `clee`, then resolves remaining NULLs
- **Incremental mode** (default): Only processes rows where `clee_propagado_radio IS NULL`

### Performance Indexes (100M+ rows)

| Index | Columns | Speed Gain |
|-------|---------|-----------|
| `ix_estab_geo` | `cve_ent, cve_mun, cve_loc` | 100-600x |
| `ix_estab_activity_period` | `codigo_act, period` | 90-180x |
| `ix_estab_composite` | `cve_ent, codigo_act, per_ocu` | 300-600x |
| `ix_estab_postal` | `cod_postal` | 50-100x |
| `ix_estab_coords` | `latitud, longitud` | 80-200x |
| `ix_estab_clee` | `clee` | 200-500x |

### Backend Configuration

```bash
DATABASE_BACKEND=duckdb          # or postgresql
SILVER_DUCKDB_PATH=data/silver/denue.duckdb
GOLD_DUCKDB_PATH=data/gold/denue.duckdb
DUCKDB_THREADS=8
DUCKDB_MEMORY_LIMIT=16GB
```

| Backend | Migrations | Use Case |
|---------|-----------|----------|
| DuckDB | `init_db()` (create_all) | Development, Analytics |
| PostgreSQL | Alembic | Production, Multi-user |

**DuckDB limitation:** Does not support Alembic autogenerate or foreign keys in ETL patterns. Use application-level integrity.

### Seed Loading

Loading order (FK hierarchy): `entidades` -> `municipios` -> `localidades` -> `actividades_economicas`

```bash
make db-migrate          # Initial setup (creates schema + loads seeds)
make db-seed             # Reload seeds (truncate-and-reload, idempotent)
```

Verification:
```bash
uv run python -c "
import duckdb
from src_etl.core.config import settings
conn = duckdb.connect(str(settings.GOLD_DUCKDB_PATH))
for t in ['denue_entidades','denue_municipios','denue_localidades','denue_actividades_economicas']:
    print(f'{t}: {conn.execute(f\"SELECT COUNT(*) FROM {t}\").fetchone()[0]}')
conn.close()
"
# Expected: 32, 2478, 39140, 1127
```

### API Endpoints

```
GET /health              # Basic health check
GET /api/v1/health/db    # Database health check
GET /api/v1/info         # API information
GET /api/v1/docs         # Swagger UI
GET /api/v1/redoc        # ReDoc
```

---

## Environment Variables

```bash
# Database
DATABASE_BACKEND=duckdb
SILVER_DUCKDB_PATH=data/silver/denue.duckdb
GOLD_DUCKDB_PATH=data/gold/denue.duckdb
DUCKDB_THREADS=8
DUCKDB_MEMORY_LIMIT=16GB
METADATA_DUCKDB_PATH=data/metadata.duckdb

# PostgreSQL (when DATABASE_BACKEND=postgresql)
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=denue
POSTGRES_USER=postgres
POSTGRES_PASSWORD=your_password

# SQLAlchemy
SQLALCHEMY_ECHO=false
SQLALCHEMY_POOL_SIZE=5
SQLALCHEMY_MAX_OVERFLOW=10
SQLALCHEMY_POOL_PRE_PING=true

# Application
LOG_LEVEL=INFO
API_TITLE=DENUE ETL API
API_VERSION=0.1.0
API_PREFIX=/api/v1

# Testing
TEST_DB_PATH=data/denue_warehouse_most_recent.db
```

---

## Metadata Tracking (src_metadata)

### MetadataTracker Usage

```python
from src_metadata.tracker import MetadataTracker
from src_metadata.models import JobStatus, TaskStatus

tracker = MetadataTracker(Path("data/metadata.duckdb"))

# Job run lifecycle
run_id = tracker.create_run("denue_downloader", config={...})
tracker.complete_run(run_id, JobStatus.SUCCESS)

# Download tasks
task_id = tracker.create_task(run_id, "download_comercio_2024_11", url, path)
tracker.start_task(task_id)
tracker.complete_task(task_id, TaskStatus.SUCCESS, result)

# Process tasks
task_id = tracker.create_process_task(run_id, task_name, source_path, period)
tracker.complete_process_task(task_id, status, rows_loaded=1000)

# CLEE Resolution tasks
task_id = tracker.create_clee_resolution_task(run_id, task_name, region, states)
tracker.complete_clee_resolution_task(task_id, status, entities_matched=8000)

# Queries
failed = tracker.get_failed_tasks(retryable_only=True)
summary = tracker.get_run_summary(run_id)
metrics = tracker.get_process_metrics()
```

### Monitoring Queries

```sql
-- Failed retryable downloads
SELECT * FROM download_tasks
WHERE status = 'FAILED' AND is_retryable = TRUE AND attempt_number < max_retries;

-- Performance by period
SELECT period, COUNT(*), AVG(file_size_bytes)/1024/1024 as avg_mb,
       AVG(download_speed_mbps), AVG(duration_seconds)
FROM download_tasks WHERE status = 'SUCCESS' GROUP BY period;

-- Job success rate
SELECT DATE(run_date), COUNT(*),
  ROUND(100.0 * SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END) / COUNT(*), 2) as pct
FROM job_runs GROUP BY DATE(run_date) ORDER BY date DESC;

-- Data lineage audit
SELECT dl.*, dt.status, jr.job_name
FROM data_lineage dl
JOIN download_tasks dt ON dl.task_id = dt.task_id
JOIN job_runs jr ON dl.run_id = jr.run_id
ORDER BY dl.created_at;
```

### Error Classification

| Error | Retryable | Description |
|-------|-----------|-------------|
| NETWORK | Yes | Connection issues |
| HTTP_500 | Yes | Server errors |
| TIMEOUT | Yes | Request timeout |
| HTTP_404 | No | Not found (permanent) |
| VALIDATION | No | Corrupt file (permanent) |
| ZIP_CORRUPT / HASH_MISMATCH / SIZE_MISMATCH | No | File integrity failure |

---

## Testing

### Markers

```python
@pytest.mark.unit          # Fast, isolated
@pytest.mark.integration   # Real database
@pytest.mark.db            # Database-related
@pytest.mark.slow          # 100M+ row queries
@pytest.mark.catalog       # Catalog table tests
@pytest.mark.quality       # Data quality validation
```

### Fixtures (conftest.py)

**Manual:** `sample_establecimiento()`, `sample_entidad()`, `sample_municipio()`, `sample_localidad()`
**Real DB:** `production_db()` (read-only DuckDB), `real_sample_establecimiento()`, `db_schema_columns()`

---

## Code Style

### Principles
- **Polars** for DataFrames (never pandas)
- **Type hints** on all public functions
- **pydantic-settings** for configuration (never hardcode paths)
- **Structured logging** (never `print()`)
- **Context managers** for database connections

### Import Organization

```python
# Standard library
from pathlib import Path
from typing import Optional

# Third-party
import polars as pl
from pydantic import BaseModel

# Local - ETL
from src_etl.core.config import settings
from src_etl.transformers.denue import DenuePipelineOrchestrator

# Local - Metadata
from src_metadata.tracker import MetadataTracker
from src_metadata.models import JobStatus
```

---

## Alembic Migrations

**Golden rule:** NEVER call `commit()` or `rollback()` in migration code. Let Alembic manage transactions.

```
Alembic Transaction Lifecycle:
  1. BEGIN TRANSACTION
  2. Execute migration upgrade()
  3. Update alembic_version table
  4. COMMIT (or ROLLBACK on error)
```

Anti-patterns:
- Calling `connection.commit()` inside migrations
- Not re-raising exceptions in helper functions
- Mixing transaction levels between migrations and helpers

Verify cycle:
```bash
alembic upgrade head      # Apply all
alembic downgrade -1      # Rollback seeds (data: 0 rows)
alembic upgrade head      # Reapply (data: 42,777 rows)
```

---

## Git Rules

**Committed:** `data/seeds/*.parquet`, `uv.lock`, `*.py`, `*.md`, config files
**Ignored:** `data/bronze/*`, `data/silver/*`, `data/gold/*`, `*.duckdb`, `*.db`, `.env`, `logs/`, `.ruff_cache/`, `data/archive/`

---

## Docker

```bash
# Build
docker build -f Dockerfile.etl -t tesis-etl:local .

# Run (downloads data into local data/ volume)
docker compose run etl extract --force

# Run pipeline
docker compose run etl pipeline
```

---

## Quick Reference

```bash
# Setup from scratch
uv sync && make db-migrate

# Daily development
make test                        # Run tests (180 should pass)
make lint                        # Check code quality

# Run ETL
make extract                     # Step 1: Download from INEGI
make transform                   # Step 2: ZIP -> Silver DuckDB
make load                        # Step 3: Silver -> Gold DuckDB
# OR
make pipeline                    # All three steps

# Check status
make metadata-stats              # Pipeline statistics
make db-status                   # Migration status
duckdb data/metadata.duckdb "SHOW TABLES"
```
