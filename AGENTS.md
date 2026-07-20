# AGENTS.md - Development Guide for ETL Core

## IMPORTANT: Destructive Operations Policy

**NEVER delete, truncate, or overwrite data files (databases, parquets, etc.) without explicit user confirmation first.** This includes:
- Removing `.duckdb`, `.db`, or `.parquet` files
- Running `rm -f` on any data file
- Dropping or truncating database tables with data
- Any operation that could cause data loss

Always ask the user before executing any destructive operation, even if it seems like a necessary intermediate step.

---

## Reglas permanentes del proyecto

Directrices de trabajo definidas en `todo.md` (2026-07) y vigentes para
TODO cambio futuro; se replican aquí porque AGENTS.md es la guía de
desarrollo canónica.

### Regla de oro — migraciones y esquema

**No correr migraciones ni `ALTER TABLE` sobre los `.duckdb` de `data/`
sin confirmación explícita del usuario.** Los cambios de esquema se
preparan en código (migración Alembic + modelo ORM + DDL en el
loader/writer correspondiente) y se aplican en un paso controlado
(`make db-migrate`) que ejecuta el usuario. Esto aplica también a
operaciones "solo aditivas" (CREATE TABLE, INSERT en catálogos): el paso
de aplicación sobre las bases reales lo decide el usuario.

### Regla de configuración

**Toda variable importante de configuración o calibración se lee del
entorno (`.env`) con default en código, y se documenta en TRES lugares:
`.env` (entrada explícita), `.env.example` y AGENTS.md.** Las constantes
que NO deben ser configurables (físicas como el radio de la Tierra, o
estructurales acopladas a datos materializados como las resoluciones H3)
se quedan en su archivo con un docstring que explique su significado y
POR QUÉ no son `.env`. Además: **toda variable global y toda función
deben tener docstring**.

### Premisa de optimización

**Toda mejora debe optimizar tiempo de ejecución Y uso de RAM a la vez,
aprovechando la CPU con paralelización correcta** — el pipeline corre en
una Mac M4 Pro/Max (límite DuckDB en `.env`), no en un servidor. Nada de
caches sin cota, KBs monolíticas en RAM ni matrices gigantes
precomputadas; preferir procesamiento columnar/streaming out-of-core,
caches acotados o en disco, y liberar memoria entre pasadas. Para CPU:
preferir el paralelismo interno de los motores (DuckDB threads, Polars,
rapidfuzz workers) antes que multiprocessing manual; cuando se use
multiprocessing, fork+COW sin copiar datos grandes. Los datos DENUE
provienen de encuestas: cualquier campo puede venir vacío o faltar en un
periodo completo — todo código de matching debe tolerarlo
(`fill_null("")`, columnas ausentes → `""`).

### Reglas operativas (de las auditorías 2026-07-17)

- **Conexiones**: toda conexión nueva a Silver/Gold usa
  `utils/warehouse.py:connect_configured()` (aplica
  `DUCKDB_MEMORY_LIMIT`/`DUCKDB_THREADS`), nunca `duckdb.connect()`
  directo.
- **Logging**: los stages nuevos usan
  `core/logging.py:get_stage_logger(name, log_file)` — jamás
  `logging.basicConfig()` a nivel de módulo ni `print()`.
- **Observabilidad**: todo job nuevo (extract/transform/load) se registra
  en metadata (`job_runs` + tasks + lineage) vía `MetadataTracker`, mismo
  contrato que los jobs existentes.
- **Reutilizar, no reinventar**: `Downloader` para descargas HTTP, patrón
  de seeds parquet committed (catálogos append-only), patrón kb_cache
  para derivados regenerables.

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

The transform pipeline has 4 ordered sub-steps: **ingest -> normalize-dates -> h3-cells -> clee-resolution**.

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

## H3 Cells: `h3_cell_9` and `h3_cell_5`

Two BIGINT columns on `establecimientos` (Silver; Gold inherits on `make load`)
hold precomputed H3 cell indexes derived from `latitud`/`longitud`. Valid H3
indexes always fit in a signed BIGINT (high bit is 0).

| Column | Resolution | Derivation | Consumed by | Indexed |
|--------|-----------|------------|-------------|---------|
| `h3_cell_9` | 9 (~174m edge) | `h3_latlng_to_cell(latitud, longitud, 9)` | H3 candidate blocking in `find_h3_neighbors()` (`matching.py`) — the ONLY consumer today | `ix_estab_h3_9` |
| `h3_cell_5` | 5 (~8.5km edge) | `h3_cell_to_parent(h3_cell_9, 5)` — NEVER directly from the point | Nothing yet — precomputed enabler for KB partition pruning (see subsection below) | No (deliberate) |

### Why `h3_cell_5 = parent(h3_cell_9)` instead of the point's own res-5 cell

The H3 hierarchy does not nest perfectly: ~6.5% of points near cell edges
fall in a res-5 cell that is NOT the parent of their res-9 cell (measured on
the real Silver, 2026-07-17). The future KB partition pruning needs every row
of one res-9 blocking cell to live in the same res-5 partition — only
`h3_cell_to_parent` guarantees that co-partitioning. Never compute it as
`h3_latlng_to_cell(lat, lon, 5)`.

### Intended use of `h3_cell_5`: KB partition pruning (not yet implemented)

`h3_cell_5` has zero consumers today. It exists as the precomputed enabler
for ONE specific optimization, tracked as the optional Fase 4 item in
`todo.md` (and reused by Fase 5 if that lands):

- **The problem it will solve.** When a reference KB exceeds
  `KB_MAX_ROWS_PER_PARTITION` (1.5M rows), `propagate_clees()` splits it
  into contiguous row slices and matches EVERY pending target row against
  EVERY slice. Correct but wasteful: a slice can consist entirely of rows
  thousands of km away from a target, yet it is still built, shared with
  the workers (fork/COW pages) and scanned.
- **How pruning will work.** Partition the KB by `h3_cell_5` instead of by
  arbitrary slices. Since `SPATIAL_RADIUS_M` (500m) is far smaller than a
  res-5 cell (~8.5km edge), every possible candidate for a target lies in
  the target's own res-5 cell or one of its immediate neighbors
  (`grid_disk(h3_cell_5, k=1)`). Any KB partition whose res-5 cells do not
  intersect that disk can be skipped entirely: fewer rows scanned per pass
  AND a lower RAM peak (partitions become geographically bounded), with
  zero correctness impact — pruned partitions cannot contain candidates by
  construction.
- **Why this requires the parent derivation.** Pruning decides at res 5,
  but blocking happens at res 9. `h3_cell_5 = parent(h3_cell_9)`
  guarantees every row of one res-9 blocking cell lives in exactly one
  res-5 partition, so a skipped partition can never hide a candidate the
  res-9 `grid_disk` would have found. Deriving res 5 from the point
  directly would leak ~6.5% of rows across partition boundaries (measured;
  see previous subsection).
- **Secondary planned consumer.** The Fase 5 consolidated-KB experiment
  (candidate generation as out-of-core DuckDB joins) would use `h3_cell_5`
  as its partition/pruning key for the same reason.
- **Cost and exit criteria.** Keeping the column costs ~0: derived in the
  same INSERT/UPDATE pass, no index, and DuckDB columnar compression
  handles the low-cardinality BIGINT well (Mexico spans on the order of
  thousands of distinct res-5 cells). It is derivable from `h3_cell_9` at
  any time, so if
  the pruning optimization is eventually rejected, the column can simply
  be dropped; until then it stays materialized so the optimization can be
  tested without another 77M-row pass.

### How the columns get populated (two paths, same expressions)

1. **At ingest (primary path)**: `EstablecimientoWriter.upsert()`
   (`loaders/denue.py`) computes both cells inside the INSERT SELECT using
   the DuckDB `h3` community extension (vectorized, out-of-core). Guard:
   coords outside [-90,90]/[-180,180] (or missing lat/lon columns) → NULL
   cells. New periods are born with cells populated.
2. **h3-cells stage (incremental safety net)**: `H3CellBackfiller`
   (`transformers/h3_backfill.py`) runs a per-period SQL UPDATE for rows
   where `h3_cell_9 IS NULL` and coords are valid. This did the original
   76.7M-row backfill and catches any row that reached Silver without
   cells. Tracked in metadata (job `denue_h3_backfill`, one process_task
   per period).

Both paths load the extension via `load_h3_extension()`
(`utils/warehouse.py`). Where Python-side cells are needed, the h3-py
`h3.api.basic_int` API is used — verified to produce cells identical to the
DuckDB extension.

### How `h3_cell_9` drives matching

`find_h3_neighbors(ref_df, target_df, radius)` (`matching.py`) is the
candidate generator of the H3 propagation method (fills `clee_propagado_h3`):

1. `grid_disk(h3_cell_9, k=H3_GRID_DISK_K)` over the DISTINCT target cells
   only (pairs accumulated in preallocated numpy arrays, 16 bytes/pair —
   never Python tuples).
2. Polars hash joins: target → neighbor cells → KB rows by `h3_cell_9`
   (multithreaded, streaming engine).
3. Exact haversine post-filter `<= SPATIAL_RADIUS_M` restores the exact
   radius semantics of the BallTree; results sorted by distance.

Parity with the BallTree generator is verified: identical neighbors, 0
differences (`tests/unit/test_h3_candidates.py`). Rows with NULL
`h3_cell_9` (no coords) get 0 candidates, same as the baseline. `KB_COLUMNS`
includes `h3_cell_9`, so the KB cache (`data/silver/kb_cache/{radio|h3}/`)
carries it; the BallTree generator simply ignores the column.

### Structural constants (deliberately NOT .env)

Defined in `clee_resolution_config.py` with full docstrings:

- `H3_BLOCKING_RES = 9` / `H3_PARTITION_RES = 5`: materialized in ~77M rows
  of Silver — changing either requires an Alembic migration + full
  re-backfill with `--force`, so they must not drift via environment.
- `H3_GRID_DISK_K = 3`: k=3 rings at res 9 cover >= 500m worst case. Coupled
  to `SPATIAL_RADIUS_M`: an import-time guard raises if
  `SPATIAL_RADIUS_M > k × 174m`. Raising the radius means raising k AND
  re-verifying H3-vs-BallTree parity.

### Index / write-performance rules

- `ix_estab_h3_9` is the only index the H3 method needs. There is
  deliberately NO index on `h3_cell_5` (unused column) nor on
  `clee_propagado_h3`: each ART index on the 77M-row table makes bulk
  writes ~1000x slower (measured).
- Full recompute with `--force`: `DROP INDEX ix_estab_h3_9` → backfill →
  `CREATE INDEX` is an order of magnitude faster (the UPDATE cost is row
  rewrite + per-row ART maintenance, not the h3 computation). Not needed
  for the normal incremental flow (new periods are ~1-4M rows).

### CLI Usage

```bash
# h3-cells stage only (incremental: rows with NULL h3_cell_9)
make h3-backfill
# equivalent:
uv run python -m src_etl.main transform --no-ingest --no-normalize-dates --no-clee-resolution --h3-cells

# Recompute ALL rows with valid coords (drop ix_estab_h3_9 first, see above)
make h3-backfill ARGS="--force"
```

---

## CLEE Resolution Step (Entity Resolution)

The transform pipeline has 4 ordered sub-steps: **ingest -> normalize-dates -> h3-cells -> clee-resolution**. This section documents the CLEE resolution stage in detail.

### Purpose

CLEE resolution links establishments across temporal snapshots of DENUE. INEGI assigns CLEE codes starting in 2021, but older periods (2015-2020) lack them. This stage propagates CLEEs from recent periods backward through time using spatial + fuzzy matching, enabling longitudinal tracking of SME survival and mortality.

### Cómo funciona, explicado desde cero (for dummies)

**El problema.** El DENUE es una "foto" periódica de todos los negocios de
México. A partir de 2021 INEGI asigna a cada negocio un identificador
estable (CLEE), pero en las fotos viejas (2015-2020) el mismo negocio
aparece SIN ningún ID que lo conecte entre fotos. Para estudiar
supervivencia/mortalidad necesitamos saber que la "TAQUERÍA EL GÜERO" de
la foto 2019 es EL MISMO negocio que la de la foto 2022 (que sí tiene
CLEE). La propagación busca cada negocio "huérfano" en las fotos que sí
tienen identidad y le hereda (propaga) el CLEE hacia atrás en el tiempo.

**La intuición.** Un negocio casi nunca se mueve. Si en 2022 hay una
taquería con CLEE en cierta esquina, y en 2019 había una taquería con
nombre casi idéntico a metros de esa esquina, es razonable concluir que
son el mismo negocio. La regla completa es: (1) busca candidatos CERCA
(≤500m), (2) exige que el giro coincida, (3) compara nombres y señales de
contacto, (4) si pasa, copia el CLEE. Los dos métodos (radio y H3) solo
difieren en el paso 1 — los pasos 2-4 (la "cascada") son idénticos.

**Paso 1A — candidatos por RADIO (método baseline, `clee_propagado_radio`).**
Literalmente: dibujar un círculo de 500m alrededor del negocio target y
preguntar qué negocios del periodo de referencia caen adentro. Para no
comparar contra los ~4M de negocios de la referencia uno por uno, las
coordenadas de referencia se indexan en un **BallTree** (un árbol de
búsqueda espacial) con distancia **haversine** (la distancia real en
metros sobre la esfera terrestre). Es exacto, pero cada consulta recorre
el árbol de forma secuencial (un solo core).

**Paso 1B — candidatos por H3 (método nuevo, `clee_propagado_h3`).**
H3 (sistema de Uber) divide TODA la superficie de la Tierra en una malla
de hexágonos, disponible en 16 tamaños ("resoluciones"). Cada hexágono
tiene un ID numérico único. Lo que representa cada celda en este proyecto:

- **`h3_cell_9`** (resolución 9, lado ~174m): "la manzana". El hexágono de
  ~2-3 cuadras donde cae el negocio. Es la unidad de **blocking**: dos
  negocios solo se comparan si sus hexágonos están cerca.
- **`h3_cell_5`** (resolución 5, lado ~8.5km): "la zona de la ciudad".
  Hoy no se usa; está reservada para partir la KB en pedazos geográficos
  (ver sección H3 Cells arriba).

Como cada negocio ya trae precomputado su `h3_cell_9`, encontrar vecinos
deja de ser geometría y se vuelve un **join de tablas**: se toma el
hexágono del target más 3 anillos de hexágonos alrededor (`grid_disk k=3`
= 37 hexágonos, que garantizan cubrir ≥500m incluso si el negocio está en
la orilla de su hexágono), y se juntan por ID de hexágono todos los
negocios de referencia que caen en esos 37. Después un post-filtro calcula
la distancia haversine EXACTA y descarta los que quedaron a >500m (los
hexágonos sobran en las esquinas del disco). El resultado son EXACTAMENTE
los mismos vecinos que el círculo del BallTree — pero obtenido con joins
columnares de Polars que usan todos los cores.

```
      grid_disk(celda_del_target, k=3) = 37 hexágonos ≈ 700m a la redonda
                 3 3 3 3
                3 2 2 2 3          X = hexágono del target (h3_cell_9)
               3 2 1 1 2 3         1 = anillo 1 (pegados, ~174m)
              3 2 1 X 1 2 3        2 = anillo 2 (~350m)
               3 2 1 1 2 3         3 = anillo 3 (~520-700m)
                3 2 2 2 3
                 3 3 3 3           post-filtro: solo lo que esté ≤500m
```

**Ejemplo completo, paso a paso (números ilustrativos).**

Target: "TACOS EL GUERO", lat 20.6597, lon -103.3496 (centro de
Guadalajara), periodo 2019-11, sin CLEE. Referencia: periodo 2022-11 (ya
resuelto, todos con CLEE).

1. El target vive en el hexágono `h3_cell_9 = 617700169958293503`.
2. `grid_disk(617700169958293503, 3)` → 37 IDs de hexágonos.
3. Join contra la KB de 2022-11 por `h3_cell_9`: 84 negocios de referencia
   caen en esos 37 hexágonos → 84 candidatos crudos.
4. Distancia haversine exacta: 61 de los 84 están a ≤500m → candidatos
   finales, ordenados del más cercano al más lejano.
   (El método radio habría producido estos MISMOS 61, con el círculo.)
5. Cascada sobre cada candidato, empezando por el más cercano:
   - Candidato a 22m: "TACOS EL GUERO SA DE CV", actividad "Restaurantes
     de tacos y tortas" vs "Restaurantes de tacos y tortas" → gate de
     actividad = 100 ✓ (obligatorio, umbral 60). Nombre comercial fuzzy
     = 95 > 75 → **MATCH DIRECTO** (Path B). Se detiene la búsqueda.
   - (Si el fuzzy de nombre hubiera dado 68, se habrían contado señales
     débiles: ¿mismo teléfono? ¿mismo CP? 2+ señales → match combinado,
     que solo gana si ningún candidato más lejano logra match directo.)
6. El target 2019 hereda el CLEE del candidato:
   `clee_propagado_h3 = '14TAC12345'` (el mismo valor que ya tenía la fila
   de 2022). Ahora ambas filas comparten identidad → el negocio "existía
   al menos desde 2019".
7. Un negocio de 2019 que tras comparar contra TODAS las referencias no
   matchea con nadie recibe un ID sintético `NEW_REG_1234H3-a1b2c3d4e5f6`
   (nuevo/desaparecido; el sufijo H3 evita colisionar con los sintéticos
   del método radio).

**La ventana deslizante (contexto temporal).** Los periodos CON CLEE de
INEGI (2021+) se resuelven primero trivialmente (`columna = clee`). Luego
los periodos huérfanos se procesan del más nuevo al más viejo, y cada
periodo resuelto SE VUELVE referencia para los siguientes: 2019 se compara
contra 2022, 2021...; cuando 2019 queda resuelto, 2018 se compara contra
2022, 2021, ..., Y 2019. Así la identidad fluye del futuro al pasado.

**¿Por qué dos métodos si dan el mismo resultado?** Porque por
construcción DEBEN dar asignaciones idénticas (mismo radio de 500m, misma
cascada) — verificado en paridad end-to-end. Lo que cambia es el CÓMO:
BallTree = consultas geométricas secuenciales; H3 = joins columnares
paralelos sobre celdas precomputadas. La comparación entre ambos es de
**rendimiento** (tiempo/RAM), material del capítulo metodológico. La
comparación de **precisión** requiere un método con otra regla de decisión
y ground truth (CLEE enmascarado) — eso es la Fase 7 del `todo.md`.

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
| `H3_PARTITION_RES` | 5 | Resolution of `h3_cell_5` (~8.5km edge), derived as `parent(h3_cell_9)`; reserved for KB partition pruning, not yet consumed |
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
stored for future KB partition pruning (currently unused, near-zero cost) —
full details in the **H3 Cells** section above. Same run-order semantics per
method: force resets ONLY that method's column; the KB cache is split per
method (`kb_cache/radio/` vs `kb_cache/h3/`); NEW_* synthetic IDs from the
H3 method carry a distinct prefix suffix so they never collide with radio's.

```bash
make clee-resolution-radio                   # radio/BallTree -> clee_propagado_radio
make clee-resolution-h3                      # H3 blocking   -> clee_propagado_h3 (default OFF in transform)
make clee-resolution-radio ARGS="--force"    # reset ONLY clee_propagado_radio (+ its KB cache) and re-resolve
make clee-resolution-h3 ARGS="--force"       # reset ONLY clee_propagado_h3 (+ its KB cache) and re-resolve
```

`--force` is per-method: it NULLs only the active method's column and clears
only that method's KB cache (`kb_cache/h3/` or `kb_cache/radio/`) — the
other method's column is never touched.

**Progress logging — one file per method**: each propagation method logs
to its own file (and stdout): `data/etl_clee_resolution_radio.log` and
`data/etl_clee_resolution_h3.log`. The file name derives from the
`CLEEResolver.METHODS` registry key (`etl_clee_resolution_{method}.log`),
so registering a new method automatically gets it a log file — no logging
code to touch. The shared matching engine (`entity_resolution.py`) logs
through the active method's logger (injected via `set_matching_logger()`),
so per-period progress, KB cache hits, worker chunk completions and the
heartbeat every `PROGRESS_LOG_INTERVAL` rows all land in the right file.
`tail -f data/etl_clee_resolution_h3.log` to watch a run.

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
make clee-resolution-radio

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

## SESNSP: Incidencia Delictiva Municipal (segunda fuente)

Segunda fuente de datos del proyecto (además de DENUE): cifras de
incidencia delictiva del fuero común a nivel municipal del SESNSP
(carpetas de investigación por municipio × delito × mes, 2015+), para
cruzar la supervivencia/mortalidad de negocios con la evolución del
crimen por municipio.

### Origen y descarga automática

- **Fuente oficial usada**: dataset "Incidencia delictiva" en datos.gob.mx
  (CC-BY-4.0). El extractor (`extractors/sesnsp.py`) consulta la **API
  CKAN** (`SESNSP_CKAN_PACKAGE_URL`) para descubrir la URL del CSV del
  corte vigente — el nombre del archivo cambia con cada corte mensual
  (`IDM_NM_dic25.csv` → `IDM_NM_jun26.csv`...), por eso nunca se fija en
  código. `SESNSP_DOWNLOAD_URL` (env) permite forzar una URL directa.
- **Por qué no el SharePoint del SESNSP**: el enlace de OneDrive exige
  login Microsoft (verificado 2026-07-19) y gob.mx tiene challenge
  anti-bot. El espejo de datos.gob.mx (`repodatos.atdt.gob.mx`) baja sin
  autenticación, aunque puede ir un corte atrás del SharePoint; un CSV
  bajado a mano del SharePoint a `data/bronze/sesnsp/` también se procesa
  (el transform toma el CSV **más reciente por mtime** de esa carpeta).
- **TLS**: datos.gob.mx sirve cadena TLS incompleta; la llamada CKAN usa
  `truststore` (certificados del sistema, como curl). La descarga del CSV
  no lo necesita.

### Modelo en Silver (2 tablas nuevas, migración f1a2b3c4d5e6)

- **`sesnsp_delitos`** (~98 filas): catálogo jerárquico bien jurídico →
  tipo → subtipo → modalidad. Ids surrogate **append-only** materializados
  en `data/seeds/seed_sesnsp_delitos.parquet` (committed); combinaciones
  nuevas de cortes futuros se agregan con `max(id)+1` sin renumerar (los
  hechos las referencian por FK).
- **`sesnsp_incidencia_municipal`** (~30.7M filas): hechos en formato
  largo — `cve_ent`, `cve_mun`, `delito_id` (FK), `fecha` (DATE
  `YYYY-MM-01`, misma convención que `fecha_alta_date`/`period`),
  `carpetas`. PK compuesta + FKs reales a `denue_entidades` y
  `denue_municipios` (misma política de integridad física que
  `establecimientos`).
- **Municipios sintéticos**: el SESNSP usa claves `998` ("NO
  ESPECIFICADO") y `999` ("OTROS MUNICIPIOS") en 6 estados; esas 8 filas
  viven en `seed_municipios.parquet` (2,486 filas ahora) como filas
  normales del catálogo para satisfacer la FK compuesta.
- **Rebuild, no upsert**: cada corte del SESNSP REVISA cifras históricas,
  así que el transform reconstruye la tabla de hechos completa
  (DELETE + INSERT en una transacción, ~50s los 30.7M en M4). Incremental
  a nivel archivo: un corte ya procesado con SUCCESS se omite (tracking
  en `process_tasks`, job `sesnsp_transform`); `--force` reprocesa.
- Claves geográficas zero-padded compatibles con DENUE: el CSV trae
  `Cve. Municipio` = ent*1000+mun (p. ej. `1001`) → `cve_ent='01'`,
  `cve_mun='001'`. Encoding del CSV autodetectado (latin-1 o utf-8).

### CLI / Makefile

```bash
make extract-sesnsp                    # CKAN discovery + descarga a data/bronze/sesnsp/
make transform-sesnsp                  # CSV mas reciente -> Silver (catalogo + hechos)
make transform-sesnsp ARGS="--force"   # reprocesar el corte ya cargado
# equivalentes: uv run python -m src_etl.main extract|transform --source sesnsp
```

`make load` no necesita nada especial: copia el archivo Silver completo a
Gold, tablas SESNSP incluidas. Log del stage: `data/etl_sesnsp.log`.

### Cruce negocios ↔ crimen (ejemplo)

```sql
-- Establecimientos y homicidios dolosos por municipio-año
SELECT n.anio, n.establecimientos, c.carpetas
FROM (SELECT year(period) anio, COUNT(DISTINCT id) establecimientos
      FROM establecimientos WHERE cve_ent='23' AND cve_mun='005' GROUP BY 1) n
JOIN (SELECT year(fecha) anio, SUM(carpetas) carpetas
      FROM sesnsp_incidencia_municipal i
      JOIN sesnsp_delitos d ON d.id = i.delito_id
      WHERE i.cve_ent='23' AND i.cve_mun='005'
        AND d.subtipo='Homicidio doloso' GROUP BY 1) c USING (anio)
ORDER BY 1;
```

Granularidad temporal: DENUE son fotos (1-2 cortes/año) y SESNSP es flujo
mensual — el cruce se resuelve agregando en la consulta (p. ej. sumar
carpetas del año o del semestre entre cortes DENUE), no en el esquema.

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
│   │   ├── denue.py               # DENUE orchestrator (Selenium + parallel)
│   │   └── sesnsp.py              # SESNSP crime data (CKAN API discovery + HTTP)
│   ├── transformers/              # Transformation layer
│   │   ├── denue.py               # Unified orchestrator (DenueTransformer)
│   │   ├── processor.py           # TransformStage: ZIP -> Silver DuckDB
│   │   ├── date_normalizer.py     # normalize-dates stage (fecha_alta_str -> DATE)
│   │   ├── h3_backfill.py         # h3-cells stage (h3_cell_9/h3_cell_5 backfill)
│   │   ├── clee_resolver.py       # CLEEResolver: Entity resolution across time
│   │   └── sesnsp.py              # SESNSP CSV -> Silver (unpivot + catalogo delitos)
│   ├── loaders/                   # Data loading layer
│   │   ├── loader.py              # LoaderStage: Silver -> Gold (file copy)
│   │   ├── denue.py               # EstablecimientoWriter (DuckDB upsert, H3 in INSERT)
│   │   ├── geography.py           # Geography seeds (entidades, municipios, localidades)
│   │   ├── scian.py               # SCIAN economic activity seeds
│   │   ├── estratos.py            # denue_estratos_personal seed
│   │   ├── dominios.py            # Domain catalogs seeds (vialidad/asentamiento/unidad)
│   │   └── sesnsp.py              # SESNSP DDL + seed catalogo delitos (append-only)
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
│   │   ├── seed_municipios.parquet      # 2,486 municipalities (incl. 8 SESNSP 998/999)
│   │   ├── seed_localidades.parquet     # 39,140 localities
│   │   ├── seed_actividades.parquet     # 1,127 economic activities
│   │   └── seed_sesnsp_delitos.parquet  # 98 SESNSP crime hierarchy (append-only ids)
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
make extract-sesnsp              # Extract SESNSP crime data (datos.gob.mx -> Bronze)
make transform-sesnsp            # Transform SESNSP CSV -> Silver
make transform                   # Transform raw data (Bronze -> Silver)
make transform-force             # Force reprocess all
make load                        # Load Silver -> Gold
make h3-backfill                 # Backfill h3_cell_9/h3_cell_5 in Silver (incremental)
make clee-resolution-radio       # CLEE Resolution radio/BallTree -> clee_propagado_radio
make clee-resolution-h3          # CLEE Resolution H3 blocking -> clee_propagado_h3
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
  H3_CELLS  -> process_tasks   (job denue_h3_backfill, Silver in-place UPDATE)
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
data/bronze/sesnsp/*.csv              # SESNSP crime CSV (one file per monthly cut)
data/silver/denue.duckdb              # Silver DuckDB warehouse (incl. clee_propagado_radio/_h3, h3_cell_9/5, sesnsp_*)
data/silver/kb_cache/{radio|h3}/      # Per-method KB cache (parquet, regenerable)
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

### ORM Models (11 tables)

| Model | Table | Primary Key | Rows |
|-------|-------|------------|------|
| `Establecimiento` | `establecimientos` | `id` (hash) | 100M+ |
| `SesnspDelito` | `sesnsp_delitos` | `id` (surrogate, seed order) | 98 |
| `SesnspIncidenciaMunicipal` | `sesnsp_incidencia_municipal` | `(cve_ent, cve_mun, delito_id, fecha)` | ~30.7M |
| `ActividadEconomica` | `denue_actividades_economicas` | `codigo_act` | 1,127 |
| `EstratoPersonal` | `denue_estratos_personal` | `id` (surrogate) | 8 |
| `TipoVialidad` | `denue_tipos_vialidad` | `id` (surrogate) | 39 |
| `TipoAsentamiento` | `denue_tipos_asentamiento` | `id` (surrogate) | 45 |
| `TipoUnidadEconomica` | `denue_tipos_unidad_economica` | `id` (surrogate) | 3 |
| `Entidad` | `denue_entidades` | `cve_ent` | 32 |
| `Municipio` | `denue_municipios` | `(cve_ent, cve_mun)` | 2,478 |
| `Localidad` | `denue_localidades` | `(cve_ent, cve_mun, cve_loc)` | 39,140 |

### Relational schema: real PK/FK links by surrogate ids

The old DuckDB ENUM column types (`tipo_vial_type`, etc.) were fully
normalized (migrations `ae66f32a9bed` + `c3a26327aec3`, user decision
2026-07-17): the domain catalogs carry `id INTEGER PRIMARY KEY` (the value's
1-based position in its enum — see `enums.domain_id()`; enums are
**append-only** because millions of rows reference these ids), the label as
UNIQUE, and a `descripcion` column with the meaning of each value.
Seeds are materialized as parquet files in `data/seeds/`
(`seed_tipos_*.parquet`, `seed_estratos_personal.parquet`, same pattern as
geography/SCIAN, tracked in git); the enums + descriptions in
`loaders/dominios.py` GENERATE those files and regenerate them on drift —
loaders always load the DB from the parquet.

`establecimientos` links with **real FOREIGN KEYs (8 total)**:
`tipo_vial_id`, `tipo_asent_id`, `tipounieco_id`, `per_ocu_id` →
`denue_tipos_*`/`denue_estratos_personal(id)`; `codigo_act` → SCIAN
catalog; plus the 3 composite geography FKs. Canonical DDL:
`loaders.denue.ESTABLECIMIENTOS_DDL` (same schema as the ORM model).

Key implications:
- **Ingest**: CSVs bring labels; `EstablecimientoWriter.upsert()` converts
  label → id via LEFT JOIN with the catalogs (`DOMAIN_ID_COLUMNS`).
  `validate_integrity()` fail-fasts out-of-domain labels BEFORE insert, and
  `_validate_codigo_act()` catches new SCIAN codes with an actionable error
  (update the catalog seed) instead of a cryptic FK violation.
- **Reads**: labels come via JOIN (e.g. `SILVER_READ_QUERY` resolves
  `per_ocu`/`tipo_asent` under their old column names, so the CLEE matching
  pipeline is unchanged). ORM relationships (`actividad`, `estrato`,
  `tipo_vialidad`, `tipo_asentamiento`, `tipo_unidad`, `entidad`,
  `municipio`, `localidad`) are FK-driven.
- **Cost accepted**: FKs imply per-row lookups/index maintenance on massive
  DuckDB writes; the user chose physical referential integrity.
- **DuckDB cannot `ALTER TABLE ... ADD CONSTRAINT`**: FKs only exist on
  tables created with them. New DBs are born with the final DDL; converting
  a populated Silver is the ONE-TIME script
  `scripts/reorganize_domain_ids.py` (`make reorganize-domain-ids`), which
  rebuilds the fact table (label→id joins, parity checks, swap, index
  recreation). Run order on the real Silver: `make db-migrate` → 
  `make reorganize-domain-ids`.

### Establecimiento (43 columns)

| Category | Columns |
|----------|---------|
| PK | `id` |
| Identification | `clee`, `clee_propagado_radio`, `clee_propagado_h3`, `nom_estab`, `raz_social` |
| Economic (FKs) | `codigo_act`, `tipounieco_id`, `per_ocu_id` |
| Address | `tipo_vial_id` (FK), `nom_vial`, `numero_ext`, `letra_ext`, `numero_int`, `letra_int`, `edificio`, `edificio_e` |
| Cross Streets | `tipo_v_e_1/2/3`, `nom_v_e_1/2/3` |
| Settlement | `tipo_asent_id` (FK), `nomb_asent`, `nom_cencom`, `num_local`, `cod_postal` |
| Geographic | `cve_ent`, `cve_mun`, `cve_loc`, `ageb`, `manzana` |
| Coordinates | `latitud`, `longitud` |
| H3 cells | `h3_cell_9` (res 9, blocking key), `h3_cell_5` (res 5 = parent of `h3_cell_9`, reserved) — see **H3 Cells** section |
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
| `ix_estab_h3_9` | `h3_cell_9` | H3 blocking (the one index the CLEE H3 method needs) |
| `ix_estab_geo` | `cve_ent, cve_mun, cve_loc` | 100-600x |
| `ix_estab_activity_period` | `codigo_act, period` | 90-180x |
| `ix_estab_composite` | `cve_ent, codigo_act, per_ocu_id` | 300-600x |
| `ix_estab_postal` | `cod_postal` | 50-100x |
| `ix_estab_coords` | `latitud, longitud` | 80-200x |
| `ix_estab_clee` | `clee` | 200-500x |

**State on the real Silver (2026-07-18):** only `ix_estab_h3_9` currently
exists — the analytical indexes were lost during the rename migration and
are pending recreation (tens of minutes each on 77M rows; recreate them only
when analysis queries need them — see todo.md). Deliberately unindexed:
`h3_cell_5`, `clee_propagado_radio`, `clee_propagado_h3` (each ART index
makes bulk writes ~1000x slower, measured).

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

# SESNSP (crime incidence, ver seccion SESNSP arriba)
SESNSP_CKAN_PACKAGE_URL=https://www.datos.gob.mx/api/3/action/package_show?id=incidencia_delictiva
SESNSP_RESOURCE_NAME=Incidencia delictiva municipal
# SESNSP_DOWNLOAD_URL=            # override manual, salta el descubrimiento CKAN

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
