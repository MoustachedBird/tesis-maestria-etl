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

### Regla de corridas largas — SIEMPRE `caffeinate`

**Todo comando del pipeline con tiempo de ejecución largo (ingest masivo,
normalize-dates, resolución CLEE, build-entidades, load) se lanza con
`caffeinate -dims` antepuesto:**

```bash
caffeinate -dims make transform ARGS="..."
caffeinate -dims make pipeline ARGS="..."
```

Razón: las corridas duran horas/días y un reposo a medio camino las
interrumpe. `caffeinate -dims` impide el reposo del sistema y del disco
mientras el comando vive; alternativa: "Evitar reposo automático" en
Ajustes → Batería.

**Los índices no se tocan a mano.** El ingest y la resolución CLEE los
suspenden y restituyen solos (ver "Índices" en Database); no hay comando
manual precisamente para que la base no dependa de que alguien lo recuerde.

**Memoria en ingest masivo (incidente 2026-07-23):** el rebuild murió dos
veces con `SIGBUS` (heap over-read dentro de DuckDB 1.4.3, mismo stack).
El disparador NO fue el reposo: con `establecimientos` en ~28GB, el
índice PK (ART sobre `id` VARCHAR) + buffers de commit ya no caben en
`DUCKDB_MEMORY_LIMIT=8GB` — todos los INSERT fallaban con "could not
allocate block (7.4/7.4 GiB)" y bajo esa presión sostenida DuckDB acaba
en la ruta con el bug. Mitigación: subir `DUCKDB_MEMORY_LIMIT` en `.env`
(12GB+) mientras la tabla es grande; si reaparecen OOMs, subir más o
bajar `DUCKDB_THREADS`. Recuperación tras crash: DuckDB commitea por
archivo — reanudar incremental (SIN `--force`, que dropearía la tabla) y
verificar en metadata que los pendientes no tengan SUCCESS viejos que el
incremental saltaría.

### Respaldo automático antes de escribir (2026-07-25)

**Cada comando que modifica el warehouse deja primero un respaldo
rotativo** (`src_etl/utils/backup.py`, enganchado en ingest,
normalize-dates, h3-cells, ambas resoluciones CLEE, sesnsp, load y
build-entidades). No hay paso manual que recordar.

- Un archivo por base: `data/silver/mipymes.backup.duckdb`. Cada comando
  **sobreescribe** el anterior — es "el estado justo antes de esta
  corrida", no un historial.
- **Uno por comando, no por etapa**: `make transform` corre varias etapas
  y el respaldo se hace una sola vez, antes de la primera escritura, para
  que revertir devuelva al estado previo al comando completo.
- En APFS es un **clon copy-on-write**: medido sobre el Silver real de
  52GB → **0.07s y 0 bytes** de disco (crece solo conforme los archivos
  divergen). Fuera de APFS cae a copia normal.
- **Nunca pisa un respaldo bueno con una base ilegible**: antes de copiar
  verifica que la fuente abra: si no, conserva el respaldo previo —
  justo el caso en que se necesita.
- Se apaga con `WAREHOUSE_BACKUP_ENABLED=false` (`.env`).

Revertir una corrida fallida:

```bash
mv data/silver/mipymes.backup.duckdb data/silver/mipymes.duckdb
```

**Por qué existe (incidente 2026-07-24):** un OOM a media carga dejó filas
parciales y corrompió el índice ART; el reintento incremental remató la
base y la única salida fue reconstruir ~8h desde Bronze. Con respaldo, esa
recuperación es mover un archivo.

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

## Cortes nuevos de INEGI: catálogos que se quedan cortos

Cada corte DENUE puede traer localidades, códigos SCIAN o etiquetas de
dominio que no existían. **El pipeline ahora lo dice con nombre y apellido**
(ver "Fail-fast" en la sección Database) en vez de fallar con una violación
de FK que no distingue el nivel:

```
❌ localidad fuera del catalogo denue_localidades: [('21', '119', '0057')]
   (agregar al seed data/seeds y re-correr).
```

### Procedimiento

**Un valor suelto**: agregarlo al parquet correspondiente en `data/seeds/`
(nombres de localidad SIEMPRE en MAYÚSCULAS, ordenado por su PK) y correr
`make db-migrate`. No se toca código.

**Un corte completo** (decenas o cientos de altas): escanear los CSV del
periodo y anexar de golpe las que falten. Caso real: el corte **2026-05
trajo 1,060 localidades nuevas**, y sus 24 archivos fallaban todos. El
escaneo lee cada ZIP, extrae `(cve_ent, cve_mun, cve_loc, localidad)`,
normaliza (pad de ceros, mayúsculas) y hace un anti-join contra el seed.

**Etiquetas de dominio renombradas**: si INEGI cambia el nombre de un valor
que ya existe, va como alias en `DOMAIN_VALUE_ALIASES`, NO como fila nueva
del catálogo (evita dos ids para un concepto). Caso real: `tipo_asent`
`"OTRO"` ≡ `"OTRA (ESPECIFICAR)"` desde 2026-05.

Un seed mal formado (PK duplicada, NULL en columna obligatoria) se detiene
en `make db-migrate` con el archivo y los valores ofensores.

---

## Normalize-Dates Step

The DENUE transform pipeline has 5 ordered sub-steps: **ingest -> normalize-dates -> h3-cells -> clee-resolution-radio -> clee-resolution-h3** (see "Comando único global" below for how they combine with SESNSP in one `make transform`).

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
# Run only normalize-dates (skip everything else)
uv run python -m src_etl.main transform --no-ingest --normalize-dates --no-h3-cells \
    --no-clee-resolution-radio --no-clee-resolution-h3 --no-sesnsp

# Force re-normalize all periods
uv run python -m src_etl.main transform --no-ingest --normalize-dates --no-h3-cells \
    --no-clee-resolution-radio --no-clee-resolution-h3 --no-sesnsp --force

# Full transform (DENUE + SESNSP, todo prendido por default)
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
make transform ARGS="--only h3-cells"

# Recompute ALL rows with valid coords (drop ix_estab_h3_9 first, see above)
make transform ARGS="--only h3-cells --force"
```

---

## CLEE Resolution Step (Entity Resolution)

The DENUE transform pipeline has 5 ordered sub-steps: **ingest -> normalize-dates -> h3-cells -> clee-resolution-radio -> clee-resolution-h3**. This section documents the CLEE resolution stage in detail.

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

    2. FOR each ref_period R in resolved_periods NEWER than P (nearest-first):
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
- **Reference order**: nearest-first — for each target, references are tried from the temporally CLOSEST to the farthest. Fase 7 (docs/findings.md 2026-07-21) measured 95.9% precision nearest-first vs 71.3% newest-first with identical thresholds; chaining through probabilistically-resolved periods costs only ~0.15 pts per hop. Explicación integrada del diseño + experimentos: `docs/clee_propagation.md`
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

Aísla la sección CLEE apagando el resto (ver "Comando único global"):

```bash
make transform ARGS="--only clee-radio"           # radio/BallTree -> clee_propagado_radio
make transform ARGS="--only clee-h3"              # H3 blocking -> clee_propagado_h3
make transform ARGS="--only clee"                 # ambas propagaciones
make transform ARGS="--only clee-radio --force"   # reset ONLY radio (+ su KB cache) y re-resolver
make transform ARGS="--only clee-h3 --force"      # reset ONLY h3 (+ su KB cache) y re-resolver
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
# Run only CLEE resolution radio (skip everything else)
uv run python -m src_etl.main transform --only clee-radio

# Force re-resolve all periods (resets clee_propagado_radio to NULL first)
uv run python -m src_etl.main transform --only clee-radio --force

# Or via Makefile
make transform ARGS="--only clee-radio"

# Full transform (DENUE completo + SESNSP, todo prendido por default)
uv run python -m src_etl.main transform
```

### Tips

- Target periods are processed **newest-first** (`ORDER BY period DESC`) so each resolved period feeds the next; within a target, references are tried **nearest-first** (temporal distance drives precision — Fase 7, docs/findings.md).
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

### Modelo en Silver (2 tablas nuevas, migración a2000sesnsp)

- **`sesnsp_delitos`** (98 filas): catálogo jerárquico bien jurídico →
  tipo → subtipo → modalidad. Su **fuente de verdad es
  `data/seeds/seed_sesnsp_delitos.parquet`** (versionado), desde donde lo
  siembra la migración `a2000sesnsp` — igual que los catálogos DENUE.
  El transform **NO descubre delitos del CSV**: valida que los del corte
  existan y los enlaza por FK, como un establecimiento contra SCIAN o
  tipo de asentamiento. Un delito nuevo se da de alta al FINAL del parquet
  (ids append-only: los hechos los referencian) y se corre `make
  db-migrate`.
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

SESNSP se aísla con `--only sesnsp` sobre el comando global (ya NO hay targets
`extract-sesnsp` / `transform-sesnsp`; ver "Comando único global"):

```bash
make extract   ARGS="--only sesnsp"           # CKAN discovery + descarga a data/bronze/sesnsp/
make transform ARGS="--only sesnsp"           # CSV mas reciente -> Silver (catalogo + hechos)
make transform ARGS="--only sesnsp --force"   # reprocesar el corte ya cargado
# make extract / make transform SIN flags ya incluyen SESNSP.
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
Diseño completo del cruce (definiciones de supervivencia/mortalidad,
query patrón negocio-intervalo, limitaciones): `docs/cruce_denue_sesnsp.md`.

---

## N3: Dimensión `entidades_economicas`

Una fila por identidad de negocio resuelta (`clee_propagado_radio`), no
por observación-periodo — la unidad de análisis de supervivencia/
mortalidad de la tesis. Preparada en código (`loaders/entidades.py`,
migración `a3000entidades`); **pendiente de aplicar** hasta que termine la
re-resolución CLEE con el código actual (los ids `NEW_*` van a cambiar).

- **Construcción**: `build_entidades_economicas(conn)` agrega
  `establecimientos` por `clee_propagado_radio` — atributos del periodo
  MÁS RECIENTE (desempate determinista por `id`), `primer_periodo`/
  `ultimo_periodo`/`n_periodos` del rango observado,
  `fecha_alta_date` = mínima conocida, `es_sintetica` = prefijo `NEW_`.
- **Rebuild total siempre** (DROP + CREATE + INSERT transaccional): los
  ids sintéticos no son estables entre corridas `--force`, así que la
  dimensión NUNCA se actualiza incrementalmente — se reconstruye después
  de cada re-resolución.
- **Sin FKs a catálogos a propósito**: artefacto derivado de una tabla
  que ya garantiza esos dominios; agregar FKs solo encarecería el
  INSERT masivo sin aportar integridad nueva.
- **Se construye en Silver** (no en Gold pese a que N3 originalmente lo
  proponía ahí): `make load` la copia a Gold sin lógica adicional, igual
  que el resto del esquema.

```bash
make build-entidades   # correr DESPUES de clee-resolution-radio --force
# equivalente: uv run python -m src_etl.main build-entidades
```

Log del stage: `data/etl_entidades.log`. Tracking: job `entidades_build`
en metadata (`process_tasks`).

---

## Fase 7: Evaluación con CLEE enmascarado (`scripts/evaluate_masked_clee.py`)

Mide precision/recall de la cascada de matching usando el `clee` OFICIAL
de INEGI como ground truth (no depende de `clee_propagado_*`, así que
puede correrse antes o después de cualquier re-resolución). Protocolo
completo diseñado en `todo.md` sección "Protocolo de evaluación".

- **Idea**: toma un periodo con CLEE oficial al 100% (default `05_2021`,
  el más viejo — análogo fiel a producción porque solo tiene referencias
  futuras), lo ENMASCARA, lo resuelve con la misma maquinaria de
  producción (`build_single_period_kb`, `propagate_clees_radio`,
  ventana nearest-first; `--ref-order newest` reproduce el orden legado
  para comparación) contra los demás periodos oficiales, y compara
  contra la verdad.
- **Partición del resultado en 5 categorías** (la pieza clave para no
  inflar métricas): evaluables (la verdad SÍ existe en alguna
  referencia) → `correcto`/`falso_match`/`falso_nuevo`; no-evaluables
  (la verdad NO existe en ninguna referencia, comportamiento correcto es
  NEW_*) → `nuevo_correcto`/`match_espurio`. `match_espurio` es la
  métrica que radio/H3 no pueden darse solas: cuánto "inventa" la
  cascada cuando no debería matchear nada.
- **100% aislado de Silver real**: `extract` lee read-only a parquets en
  `data/eval/fase7_masked_clee/`; `evaluate` trabaja solo sobre esos
  parquets, nunca abre Silver en modo escritura.

```bash
make eval-masked-clee-extract   # lectura Silver -> parquets
make eval-masked-clee           # nearest-first (produccion) -> *_nearest
make eval-masked-clee ARGS="--ref-order newest"   # orden legado, para comparar
make eval-masked-clee-chain     # dos saltos: target via intermedio resuelto
```

Salidas en `data/eval/fase7_masked_clee/`: `results*.parquet` (fila por
target con su categoría y qué referencia la resolvió) + `metrics*.json`
(global + segmentado por entidad, sector SCIAN, referencia). El sufijo
del archivo es el orden (`metrics.json` = newest legado,
`metrics_nearest.json` = producción, `metrics_chain.json` = dos saltos).
Log: `data/etl_fase7_eval.log`.

Nota: la atribución `resolved_by_ref` se hace por columnas (no por
posición) porque `propagate_clees_radio` devuelve las filas reordenadas
(resueltas primero) — una máscara posicional pre/post se desalinea
(bug corregido 2026-07-21; ver `docs/findings.md`).

---

## Cobertura de la propagación (`scripts/clee_propagation_stats.py`)

La otra mitad de la evaluación: Fase 7 mide PRECISIÓN sobre una muestra
con ground truth; este script mide COBERTURA y salud estructural de las
columnas `clee_propagado_*` tal como quedaron en el warehouse real (82.9M
filas, ~15 s, siempre READ-ONLY).

```bash
make propagation-stats                                     # radio + h3 del Silver
make propagation-stats ARGS="--column clee --no-agreement" # baseline: colisiones de la fuente
make propagation-stats ARGS="--db data/silver/otra.duckdb" # otra base o un .parquet de respaldo
```

Qué reporta (por corte y agregado, JSON en
`data/eval/propagation_stats/`, log `data/etl_propagation_stats.log`):

- **Tasa de vinculación** = filas de los cortes SIN CLEE oficial que
  quedaron con un CLEE real de INEGI en vez de `NEW_*`. Es el
  "porcentaje de éxito" de la propagación. Los cortes con CLEE oficial se
  reportan aparte: su 100% es trivial (Fase 1 determinística) y mezclarlos
  infla el número.
- **`NEW_*` nacidos por corte**: una identidad sintética "nace" en el
  corte más NUEVO en que aparece (la propagación va futuro→pasado) y de
  ahí se hereda hacia atrás. Cruzar contra los "nuevos" del log
  `data/etl_clee_resolution_{método}.log` valida la lectura.
- **Cadenas**: cuántos cortes cubre cada identidad (una identidad de un
  solo corte no aporta supervivencia).
- **Colisiones intra-periodo**: identidades repetidas dentro del mismo
  corte — imposibles por construcción, así que miden error duro sin
  ground truth. Comparar siempre contra `--column clee` (la fuente tiene
  0) para separar error del matching de ruido de origen.
- **Acuerdo radio vs H3**: los `NEW_*` llevan prefijo y uuid distintos
  por método, así que la comparación justa es el acuerdo *semántico*
  (idénticas + ambas sintéticas).

Flags: `--db` (`.duckdb` read-only o `.parquet` con un volcado de
`establecimientos`), `--column` (repetible; default = columnas del
registro `CLEEResolver.METHODS`), `--no-identity-metrics` (salta el
GROUP BY caro por identidad), `--no-agreement`, `--out`.

Línea base medida el 2026-07-30 (post nearest-first) en
`docs/findings.md` — incluye el dato del orden legado para comparar.

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
│   │   ├── indexes.py             # Suspensión/restitución automática de índices
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

## Makefile Targets — inventario completo

**Todos** los targets del `Makefile`, qué corren por debajo y si aceptan
`ARGS`. Solo los marcados con ✅ reenvían `ARGS="..."` al CLI; en el resto
`ARGS` se ignora en silencio (si necesitas flags ahí, invoca el módulo
directo con `uv run python -m src_etl.main ...`).

### ETL (comandos globales — sin `ARGS` corren TODO)

| Target | Equivale a | `ARGS` | Notas |
|---|---|---|---|
| `make extract` | `main extract` | ✅ | DENUE (Selenium) + SESNSP (CKAN) → Bronze |
| `make transform` | `main transform` | ✅ | 5 sub-pasos DENUE + SESNSP → Silver |
| `make load` | `main load` | ✅ | Silver → Gold (solo acepta `--force`) |
| `make pipeline` | `main pipeline` | ✅ | `[Extract →] Transform → Load` |
| `make pipeline-steps` | `extract transform load` | ❌ | Encadena los 3 targets sin flags |
| `make pipeline-full` | `setup extract transform load` | ❌ | Igual + `uv sync` primero |
| `make retry` | `main retry` | ❌ | **Placeholder**: hoy solo imprime un aviso |
| `make validate` | — | ❌ | **Placeholder** ("coming soon") |

### Dimensión N3 y evaluación Fase 7

| Target | Equivale a | `ARGS` | Notas |
|---|---|---|---|
| `make build-entidades` | `main build-entidades` | ❌ | Correr DESPUÉS de la resolución CLEE |
| `make eval-masked-clee-extract` | `scripts/evaluate_masked_clee.py extract` | ✅ | Silver read-only → parquets en `data/eval/` |
| `make eval-masked-clee` | `… evaluate` | ✅ | Default `--ref-order nearest` (producción) |
| `make eval-masked-clee-chain` | `… chain` | ✅ | Experimento de dos saltos |
| `make propagation-stats` | `scripts/clee_propagation_stats.py` | ✅ | Cobertura/salud de `clee_propagado_*`; Silver read-only |

### Base de datos y migraciones

| Target | Qué hace |
|---|---|
| `make db-migrate` | `alembic upgrade head` |
| `make db-downgrade` | `alembic downgrade -1` |
| `make db-reset` | **Destructivo**: `downgrade base` + `upgrade head` (pide confirmación interactiva) |
| `make db-status` | `alembic current --verbose` + historial + salud de índices (presentes/faltantes) |

### Metadata, logs y limpieza

| Target | Qué hace |
|---|---|
| `make metadata-query` | Últimos 5 job runs de `data/metadata.duckdb` (JSON) |
| `make metadata-stats` | `DenueTransformer.get_pipeline_status()` (JSON) |
| `make logs` | Contenido de `logs/downloads/latest/` + últimas 20 líneas de `app.log` |
| `make clean` | Borra `__pycache__/` y `*.pyc` |
| `make clean-logs` | Borra `logs/downloads/run_*` con más de 7 días |

### Tests, calidad y entorno

| Target | Qué hace |
|---|---|
| `make test` | `pytest` completo |
| `make test-unit` / `test-integration` / `test-db` | `pytest -m unit` / `-m integration` / `-m db` |
| `make test-fast` | `pytest -m "unit and not slow"` |
| `make test-verbose` | `pytest -vv` |
| `make test-cov` | Coverage HTML+term → `htmlcov/index.html` |
| `make lint` / `lint-fix` / `format` | `ruff check` / `check --fix` / `format` sobre `src_etl src_metadata src_api tests` |
| `make setup` / `make dev` | `uv sync` (dev = setup + mensaje) |
| `make api` | `fastapi dev src_api/main.py` |
| `make help` | Lista autogenerada de targets (default goal) |

### Inventario de flags del CLI (`uv run python -m src_etl.main <cmd>`)

Todo lo que va dentro de `ARGS="..."`. Los booleanos son flags de Typer
(`--x/--no-x`); la columna Default dice cómo arranca **sin** pasar nada.

| Comando | Flag | Default | Efecto |
|---|---|---|---|
| `extract` | `--denue/--no-denue` | ON | Descarga DENUE (Selenium, INEGI) |
| | `--sesnsp/--no-sesnsp` | ON | Descarga SESNSP (CKAN) |
| | `--only <token>` (repetible) | — | Allowlist; aquí solo aplican `denue` y `sesnsp` |
| | `--headless/--no-headless` | ON | Navegador headless (solo DENUE) |
| | `--output PATH` | `data/bronze/denue` | Override de destino (solo DENUE) |
| | `--force` | OFF | Re-descarga TODOS los archivos |
| `transform` | `--denue/--no-denue` | ON | Bloque DENUE completo |
| | `--ingest/--no-ingest` | ON | CSVs crudos → Silver |
| | `--normalize-dates/--no-normalize-dates` | ON | Normaliza `fecha_alta` |
| | `--h3-cells/--no-h3-cells` | ON | Backfill `h3_cell_9`/`h3_cell_5` |
| | `--clee-resolution-radio/--no-…` | ON | Propagación radio/BallTree |
| | `--clee-resolution-h3/--no-…` | ON | Propagación H3 (track de comparación) |
| | `--sesnsp/--no-sesnsp` | ON | Transform SESNSP |
| | `--only <token>` (repetible) | — | Allowlist (gana sobre los `--no-*`) |
| | `--force` / `-f` | OFF | Reprocesa todo (semántica por sección, ver abajo) |
| `load` | `--force` / `-f` | OFF | Carga aunque Gold esté al día |
| `pipeline` | `--extract/--no-extract` | **OFF** | Incluye la descarga (re-scrapea INEGI) |
| | *(mismos flags de sección que `transform`)* | ON | `--denue --ingest --normalize-dates --h3-cells --clee-resolution-radio --clee-resolution-h3 --sesnsp` |
| | `--load/--no-load` | ON | Load Silver → Gold al final |
| | `--only <token>` (repetible) | — | Aquí el token `load` SÍ aplica |
| | `--headless/--no-headless` | ON | Solo si `--extract` |
| | `--force` / `-f` | OFF | Reprocesa todo |
| `build-entidades` | *(sin flags)* | — | Rebuild completo de `entidades_economicas` |
| `retry` | *(sin flags)* | — | Placeholder |

### Flags del script de evaluación (`scripts/evaluate_masked_clee.py`)

| Subcomando | Flag | Default | Efecto |
|---|---|---|---|
| `extract` | `--target-period YYYY-MM-DD` | `2021-05-01` | Periodo objetivo a enmascarar |
| | `--state <cve_ent>` (repetible) | CDMX/Jalisco/Zacatecas | Muestra territorial |
| `evaluate` | `--ref-order nearest\|newest` | `nearest` | `nearest` = producción; `newest` = orden legado |
| `chain` | `--intermediate YYYY-MM-DD` | `2021-11-01` | Periodo intermedio del salto (debe estar en la muestra) |

### Comando único global (extract / transform / load / pipeline)

`extract`, `transform`, `load` y `pipeline` son **comandos globales**: sin
argumentos corren **todas** las fuentes y secciones. No hay que especificar
la fuente ni correr comandos por separado; el default es "hazlo todo" y cada
sección se **desactiva** con su flag `--no-*` (vía `ARGS="..."`). Ya no existe
`--source` **ni** targets tipo `extract-sesnsp` / `clee-resolution-radio`:
**todo se hace con el comando global + flags**.

- `make extract` → DENUE **+** SESNSP (Bronze).
- `make transform` → DENUE (`ingest → normalize-dates → h3-cells →
  clee-resolution-radio → clee-resolution-h3`) **+** SESNSP
  (`sesnsp_incidencia_municipal`), todo en una sola corrida.
- `make pipeline` → Transform global **+** Load (agrega `--extract` para
  incluir la descarga; extract va OFF por default porque re-scrapea INEGI).

Hay **dos modos** para elegir secciones (se pasan por `ARGS="..."`):

- **Substractivo — `--no-*`** (default ON): corre todo y apaga lo que listes.
- **Allowlist — `--only`** (repetible): corre SOLO lo que listes; el resto
  arranca OFF. Ideal para aislar una sección sin escribir la cadena larga de
  `--no-*`. `--only` gana sobre los `--no-*`. Tokens inválidos → error con la
  lista de válidos (validado por Enum).

| Sección | Flag opt-out (`--no-*`) | Token `--only` |
|---------|-------------------------|----------------|
| bloque DENUE completo | `--no-denue` | `denue` (grupo) |
| ingest de CSVs a Silver | `--no-ingest` | `ingest` |
| normalización `fecha_alta` | `--no-normalize-dates` | `normalize-dates` |
| backfill de celdas H3 | `--no-h3-cells` | `h3-cells` |
| propagación CLEE radio/BallTree | `--no-clee-resolution-radio` | `clee-radio` |
| propagación CLEE H3 (comparación) | `--no-clee-resolution-h3` | `clee-h3` |
| ambas propagaciones CLEE | — | `clee` (grupo) |
| transform SESNSP | `--no-sesnsp` | `sesnsp` |
| load Silver→Gold (solo `pipeline`) | `--no-load` | `load` |

Qué tokens acepta cada comando: `transform` los atómicos DENUE + `sesnsp`
+ grupos; `pipeline` esos **y** `load`; `extract` solo distingue fuente
(`denue` / `sesnsp` — cualquier token atómico de DENUE selecciona la
fuente completa, y `--only load` en extract/transform no corre nada).

**Recetario** con `--only` (reemplaza a los targets que se eliminaron):

```bash
# Solo SESNSP (antes extract-sesnsp / transform-sesnsp):
make extract   ARGS="--only sesnsp"
make transform ARGS="--only sesnsp"

# Poblar Silver SIN propagar CLEE (radio ni h3), incluye SESNSP:
caffeinate -dims make transform ARGS="--no-clee-resolution-radio --no-clee-resolution-h3"

# Solo CLEE radio (antes clee-resolution-radio); +" --force" para re-resolver:
caffeinate -dims make transform ARGS="--only clee-radio"

# Solo CLEE H3 (antes clee-resolution-h3):
caffeinate -dims make transform ARGS="--only clee-h3"

# Ambas propagaciones CLEE:
caffeinate -dims make transform ARGS="--only clee"

# Solo backfill H3 cells (antes h3-backfill):
make transform ARGS="--only h3-cells"
```

**Combinaciones frecuentes** (fin a fin):

```bash
# Rebuild TOTAL desde cero (descarga incluida), una sola orden:
caffeinate -dims make pipeline ARGS="--extract --force"

# Rebuild de Silver+Gold SIN volver a scrapear INEGI (default de pipeline):
caffeinate -dims make pipeline ARGS="--force"

# Silver completo pero SIN el track de comparación H3 (ahorra ~14h):
caffeinate -dims make transform ARGS="--no-clee-resolution-h3"

# Re-resolver CLEE radio y reconstruir lo que depende de esa columna:
caffeinate -dims make transform ARGS="--only clee-radio --force"
make build-entidades && make load

# Solo promover Silver -> Gold dentro del comando global:
make pipeline ARGS="--only load"          # equivalente a `make load`

# Preparar Silver para análisis sin tocar CLEE (ingest + fechas + H3):
make transform ARGS="--only ingest --only normalize-dates --only h3-cells"

# Ciclo de evaluación Fase 7 completo (no toca Silver en escritura):
make eval-masked-clee-extract
make eval-masked-clee                              # nearest (producción)
make eval-masked-clee ARGS="--ref-order newest"    # orden legado, comparación
make eval-masked-clee-chain                        # dos saltos

# Antes de cualquier corrida larga: verificar esquema e índices
make db-status
```

> Nota de costo: con el default "todo ON", `make transform` corre la
> propagación CLEE **radio y H3** (la H3 es un track de comparación). Son
> horas cada una; si solo necesitas producción, pasa
> `--no-clee-resolution-h3`.

> `caffeinate -dims` es obligatorio en todo lo que incluya propagación CLEE
> o un `--force` global (ver "Regla de corridas largas" arriba).

### Qué hace `--force` en cada sección (leer ANTES de pasarlo)

`--force` NO significa lo mismo en todas partes: es incremental-off en
unas y **destructivo** en otras. Tabla canónica:

| Sección | Qué hace `--force` | Costo / riesgo |
|---|---|---|
| `extract` | Re-descarga TODOS los archivos (ignora lo ya bajado) | Horas de scraping a INEGI |
| `ingest` | **`DELETE FROM establecimientos`** (`processor.py:138`) y re-procesa los ~943 archivos sobre tabla vacía | ☠️ Borra los ~77M de filas **incluidas ambas columnas `clee_propagado_*`**. Territorio de ~27.5h |
| `normalize-dates` | Re-normaliza `fecha_alta` en todos los periodos (no solo pendientes) | Minutos-horas |
| `h3-cells` | Recalcula `h3_cell_9`/`h3_cell_5` en TODAS las filas con coords válidas (no solo `IS NULL`) | Ver receta de índice abajo |
| `clee-radio` | Resetea a NULL **solo** `clee_propagado_radio` (+ su KB cache) y re-resuelve | ~1 noche; no toca la columna H3 |
| `clee-h3` | Idem para `clee_propagado_h3` (+ su KB cache) | ~1 noche; no toca la columna radio |
| `sesnsp` | Re-procesa el CSV consolidado | Minutos |
| `load` | Carga a Gold aunque esté al día | Minutos |

**Regla práctica**: si no quieres vaciar la tabla de hechos, **nunca**
pases `--force` con `ingest` activo. Usa `--only` para acotar a qué
secciones aplica:

```bash
# Force SIN re-ingestar (refresca fechas + celdas H3 + SESNSP sobre el Silver actual):
caffeinate -dims make transform ARGS="--only normalize-dates --only h3-cells --only sesnsp --force"

# Transform completo SIN propagación CLEE, en force (OJO: incluye ingest -> borra la tabla):
caffeinate -dims make transform ARGS="--no-clee-resolution-radio --no-clee-resolution-h3 --force"

# Re-resolver UNA sola propagación (la otra columna queda intacta):
caffeinate -dims make transform ARGS="--only clee-radio --force"
caffeinate -dims make transform ARGS="--only clee-h3 --force"
```

**Excepción de índices — `h3-cells --force`**: `h3_backfill.py` es el
único stage de escritura masiva que **no** usa `suspended_indexes()`
(sí lo hacen `ingest` en `processor.py:426` y la resolución CLEE en
`clee_resolver.py:623`, que se gestionan solos). Para un recompute
completo, soltar el índice a mano vale un orden de magnitud:

```bash
duckdb data/silver/mipymes.duckdb "DROP INDEX ix_estab_h3_9"
caffeinate -dims make transform ARGS="--only h3-cells --force"
duckdb data/silver/mipymes.duckdb "CREATE INDEX ix_estab_h3_9 ON establecimientos(h3_cell_9)"
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
data/silver/mipymes.duckdb              # Silver DuckDB warehouse (incl. clee_propagado_radio/_h3, h3_cell_9/5, sesnsp_*)
data/silver/kb_cache/{radio|h3}/      # Per-method KB cache (parquet, regenerable)
data/gold/mipymes.duckdb                # Gold DuckDB warehouse (production)
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

### Los seeds parquet son la fuente de verdad de los catálogos

**Regla:** el contenido de todo catálogo vive en `data/seeds/*.parquet`
(versionados en git) y en ningún otro lado. NO hay enums de Python que lo
dupliquen — `src_etl/db/enums.py` se eliminó justamente por eso: los mismos
valores existían como enum, como parquet generado desde el enum, y como
lista `DOMAIN_CATALOGS` contra la que validaba el ingest, así que dar de
alta una etiqueta nueva de INEGI obligaba a tocar código.

Aplica a **todo catálogo que tenga seed**, DENUE y SESNSP por igual:
`sesnsp_delitos` también se siembra desde su parquet en la migración, y el
transform de cada corte anual solo **valida y enlaza** contra él.

**Dar de alta un valor nuevo = editar el parquet y correr `make db-migrate`.
No se toca código.** Aplica a los 4 catálogos de dominio, SCIAN, los 3
niveles de geografía y los estratos de personal (cuyas cotas
`empleados_min/max` y `categoria` también son columnas del parquet, no
métodos de Python).

Los ids son surrogate y **append-only**: millones de filas los referencian
por FK, así que un valor nuevo se agrega al final y nunca se reordena.

#### Validación de los seeds (corre en la migración)

`loaders/seed_validation.py:validate_seed()` comprueba cada parquet contra
las restricciones **declaradas en el modelo ORM** (no en una lista aparte:
no pueden divergir) antes de insertar. Detecta PK duplicada, valor repetido
en una columna UNIQUE, NULL donde la tabla exige NOT NULL, columna
obligatoria ausente, columna que no existe en la tabla, y archivo vacío. El
error nombra archivo, tabla, columnas y ejemplos de los valores ofensores.

**Un seed corrupto detiene la migración**, no se repara a escondidas.
Verificado corrompiendo cada seed a propósito: PK duplicada (SCIAN), PK
compuesta duplicada (localidades), UNIQUE de columna (tipos de vialidad),
UNIQUE compuesto de tabla (`uq_sesnsp_delito`) y NULL en columna NOT NULL
(entidades) — los cinco hacen fallar `make db-migrate` nombrando archivo,
tabla, columnas y valores.

Sin esto un duplicado desaparecía en silencio, porque la siembra usa
`INSERT OR IGNORE`.

#### Sinónimos de dominio

Cuando INEGI cambia la etiqueta de un valor que ya existe, se declara como
alias en `denue_schema.DOMAIN_VALUE_ALIASES` en vez de agregar una fila
nueva al catálogo (dos ids para un solo concepto). Caso real: el corte
2026-05 empezó a usar `tipo_asent = "OTRO"` para lo que el catálogo llama
`"OTRA (ESPECIFICAR)"`. El alias se aplica en la limpieza, antes de validar.

### Relational schema: real PK/FK links by surrogate ids

Los catálogos de dominio llevan `id INTEGER PRIMARY KEY`, la etiqueta como
UNIQUE y una columna `descripcion` con el significado de cada valor.

`establecimientos` enlaza con **FOREIGN KEYs reales (8 en total)**:
`tipo_vial_id`, `tipo_asent_id`, `tipounieco_id`, `per_ocu_id` →
`denue_tipos_*`/`denue_estratos_personal(id)`; `codigo_act` → catálogo
SCIAN; más los 3 FKs compuestos de geografía. DDL canónico:
`loaders.denue.ESTABLECIMIENTOS_DDL` (mismo esquema que el modelo ORM).

#### Fail-fast: todo linkeo se valida contra la tabla, antes del INSERT

`EstablecimientoWriter.upsert()` verifica que cada valor del negocio exista
en su catálogo y falla con un mensaje accionable, en vez de dejar que
aparezca como violación de FK (que no dice qué nivel falló):

| Validación | Qué revisa | Contra |
|---|---|---|
| `validate_integrity()` | `tipo_vial`, `tipo_asent`, `tipounieco`, `per_ocu` | las tablas de catálogo, leídas de la BD |
| `_validate_codigo_act()` | `codigo_act` | `denue_actividades_economicas` |
| `_validate_geografia()` | `cve_ent`, `(cve_ent,cve_mun)`, `(cve_ent,cve_mun,cve_loc)` | entidades / municipios / localidades |
| `validate_delitos_del_csv()` | la jerarquía del delito de cada corte SESNSP | `sesnsp_delitos` |
| `validate_municipios_del_csv()` | los municipios del corte SESNSP (incl. sintéticos 998/999) | `denue_municipios` |

Los valores permitidos **se leen de la BD**, que es exactamente lo que
exigen los FKs — no de una copia en Python.

Otras implicaciones:
- **Ingest**: los CSV traen etiquetas; `upsert()` convierte etiqueta → id
  con LEFT JOIN contra los catálogos (`DOMAIN_ID_COLUMNS`).
- **Lecturas**: las etiquetas vuelven por JOIN (p.ej. `SILVER_READ_QUERY`
  resuelve `per_ocu`/`tipo_asent` bajo sus nombres viejos, así el pipeline
  de matching CLEE no cambia). Los relationships del ORM (`actividad`,
  `estrato`, `tipo_vialidad`, `tipo_asentamiento`, `tipo_unidad`,
  `entidad`, `municipio`, `localidad`) son FK-driven.
- **DuckDB no soporta `ALTER TABLE ... ADD CONSTRAINT`**: los FKs solo
  existen en tablas creadas con ellos. Las bases nuevas nacen con el DDL
  final desde la migración base.

### Índices: se gestionan solos, no hay comando manual

Los índices sirven para **consultar, no para cargar**: DuckDB mantiene cada
índice ART fila por fila, lo que hace las escrituras masivas órdenes de
magnitud más lentas (medido en este repo con `ix_estab_h3_9`). Pero
soltarlos a mano antes de cada carga deja la base a merced de que alguien lo
recuerde.

Por eso **no existe ningún comando de índices**. Las dos etapas que escriben
en masa —el ingest y la resolución CLEE— envuelven su trabajo en
`db/indexes.py:suspended_indexes()`, que los suelta al entrar y los recrea
en un `finally`, así que vuelven también si la etapa falla con excepción.

- El conjunto de índices se **deriva de `Base.metadata`**, no de SQL suelto:
  no puede divergir de la migración que los crea.
- Si el proceso muere de golpe (kill -9, corte de luz) quedan ausentes: la
  base sigue siendo **correcta**, solo consulta más lento, y la siguiente
  corrida los restituye porque `ensure_indexes()` es idempotente.
- `make db-status` reporta cuántos hay presentes y cuáles faltan.

Contexto de por qué importa: una consulta de trayectoria sobre 77M filas
**sin índices** tomó 83 ms (DuckDB es columnar, escanea rápido); mantener
16 índices durante 86M de INSERTs cuesta horas.

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
SILVER_DUCKDB_PATH=data/silver/mipymes.duckdb
GOLD_DUCKDB_PATH=data/gold/mipymes.duckdb
DUCKDB_THREADS=8
DUCKDB_MEMORY_LIMIT=16GB
```

| Backend | Migrations | Use Case |
|---------|-----------|----------|
| DuckDB | Alembic | Development, Analytics |
| PostgreSQL | Alembic | Production, Multi-user |

### Migraciones: cadena consolidada, sin pasos intermedios

**Estándar del proyecto: la base se crea desde cero.** La cadena son
**3 migraciones** que producen el diseño final directamente, agrupadas por
dominio y no por historia:

| Revisión | Crea |
|---|---|
| `a1000denue` | Catálogos (geografía, SCIAN con jerarquía, los 4 de dominio, estratos) + `establecimientos` con FKs por id, índices y nombres definitivos. **Siembra los catálogos desde los parquet.** |
| `a2000sesnsp` | `sesnsp_delitos` (**sembrado desde su parquet**) + `sesnsp_incidencia_municipal` |
| `a3000entidades` | `entidades_economicas` (dimensión N3, nace vacía) |

Antes eran 11, con rodeos que existían solo por historia: se creaban tipos
ENUM de DuckDB para después convertirlos en tablas de catálogo y luego
cambiar etiquetas por ids; se creaba `clee_master` para renombrarlo a
`clee_propagado_radio`; se creaba el catálogo SCIAN para arreglarle la PK
después. Con ellos se fue la maquinaria que esas conversiones exigían
(capturar y recrear índices, commits intermedios a nivel DBAPI, tipos ENUM
fantasma).

El esquema se genera desde `src_etl.db.models` (metadata del ORM), así que
**la migración no puede desincronizarse del modelo**.

**Al consolidar se verificó** que la cadena nueva produce exactamente el
mismo esquema que la histórica: tablas, columnas con tipo y nulabilidad,
PKs, UNIQUEs, FKs, índices y filas sembradas. La comparación destapó que el
modelo declaraba `latitud`/`longitud` como `Float` (precisión simple)
mientras el DDL canónico usaba `DOUBLE` — es decir, `create_all` construía
una tabla que producción nunca tuvo. Corregido a `Double`.

### Seed Loading

Los seeds se cargan **durante `make db-migrate`**, que es el único lugar
donde ocurre (no hay `make db-seed`; se eliminó). Orden por jerarquía de
FKs: `entidades` → `municipios` → `localidades` → `actividades_economicas`
→ catálogos de dominio → estratos.

```bash
make db-migrate          # crea el esquema Y siembra los catálogos
make db-status           # revisión: migración aplicada + salud de índices
```

Los loaders **no crean tablas ni toleran su ausencia**: si la tabla no
existe es que no se corrió la migración, y eso debe fallar, no repararse a
escondidas.

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
SILVER_DUCKDB_PATH=data/silver/mipymes.duckdb
GOLD_DUCKDB_PATH=data/gold/mipymes.duckdb
DUCKDB_THREADS=8
DUCKDB_MEMORY_LIMIT=16GB
METADATA_DUCKDB_PATH=data/metadata.duckdb

# PostgreSQL (when DATABASE_BACKEND=postgresql)
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=mipymes
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
TEST_DB_PATH=data/mipymes_warehouse_most_recent.db
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

> Inventario COMPLETO de targets, flags y combinaciones:
> "Makefile Targets — inventario completo" + "Comando único global".
