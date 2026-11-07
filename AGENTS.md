# AGENTS.md - Development Guide for ETL Core

## IMPORTANT: Destructive Operations Policy

**NEVER delete, truncate, or overwrite data without explicit user confirmation first.** This includes:
- `DROP`/`TRUNCATE`/`DELETE` sobre una tabla con datos, y `DROP`/`DETACH` de una partición
- `docker compose down -v` (se lleva el volumen de PostgreSQL entero)
- Borrar `data/bronze/` (son horas de scraping a INEGI) o `data/seeds/*.parquet`
  (son la fuente de verdad de los catálogos, no un derivado)
- `--force` en `ingest`: vacía la tabla de hechos (ver la tabla de `--force` abajo)
- Cualquier operación que pueda causar pérdida de datos

**No hay red de seguridad**: el respaldo automático que existía con DuckDB
desapareció con PostgreSQL, y no se reemplazó. Lo único recuperable sin
volver a descargar es lo que se deriva de Bronze y de los seeds.

Always ask the user before executing any destructive operation, even if it seems like a necessary intermediate step.

---

## Reglas permanentes del proyecto

Directrices de trabajo definidas en `todo.md` (2026-07) y vigentes para
TODO cambio futuro; se replican aquí porque AGENTS.md es la guía de
desarrollo canónica.

### Regla de oro — migraciones y esquema

**No aplicar migraciones ni `ALTER TABLE` sobre una base con datos sin
confirmación explícita del usuario.** Los cambios de esquema se
preparan en código (migración Alembic + modelo ORM + DDL en el
loader/writer correspondiente) y se aplican en un paso controlado
(`docker compose up -d migrate`) que ejecuta el usuario. Esto aplica también a
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
la workstation Linux (48 hilos / 125 GiB, `docs/entorno_linux.md`). Nada de
caches sin cota, KBs monolíticas en RAM ni matrices gigantes
precomputadas; preferir procesamiento columnar/streaming out-of-core,
caches acotados o en disco, y liberar memoria entre pasadas. Para CPU:
preferir el paralelismo interno de los motores (workers de Postgres,
Polars, rapidfuzz) antes que multiprocessing manual; cuando se use
multiprocessing, fork+COW sin copiar datos grandes. Los datos DENUE
provienen de encuestas: cualquier campo puede venir vacío o faltar en un
periodo completo — todo código de matching debe tolerarlo
(`fill_null("")`, columnas ausentes → `""`).

### Regla de corridas largas — que sobrevivan a la sesión

El pipeline corre en la workstation Linux (`docs/entorno_linux.md`), así que
**todo comando largo (ingest masivo, resolución CLEE, permanencia) se lanza
desacoplado de la terminal**, con su salida a un archivo:

```bash
nohup docker compose run --rm etl transform --only clee-radio > clee.log 2>&1 &
sleep 10
tail -f clee.log      # Ctrl+C corta el tail, no la corrida
```

Razón: las corridas duran horas o días y un SSH que se cae no debe
llevárselas. `ARRANQUE.md` trae el runbook completo con esta forma.

Si por excepción se lanza desde la Mac, ahí la amenaza es el reposo y el
comando va con **`caffeinate -dims` antepuesto** — un sleep/wake a medio
camino mató un rebuild de 27.5h (2026-07-23).

**Los índices no se tocan a mano.** El ingest y la resolución CLEE los
suspenden y restituyen solos (ver "Índices" en Database); no hay comando
manual precisamente para que la base no dependa de que alguien lo recuerde.

**Si una corrida larga muere a la mitad, se reanuda, no se rehace.** Las
tres etapas largas son reanudables y el default es reanudar: el ingest salta
los ZIP con `process_task` en SUCCESS, la resolución CLEE los cortes que ya
tienen partición, y la permanencia los cortes y bloques ya escritos. `--force`
es lo contrario y en `ingest` es destructivo.

### Reglas operativas

- **Conexiones**: toda conexión nueva usa `shared/db/session.py:get_engine()`
  o `get_session_factory()`, que vienen del pool configurado
  (`SQLALCHEMY_POOL_SIZE`/`MAX_OVERFLOW`); nunca un `create_engine()` suelto.
  Para escrituras masivas, `shared/db/copy.py` (`COPY ... FROM STDIN`), no
  `INSERT` fila por fila.
- **Logging**: los stages usan
  `shared/logging.py:get_stage_logger(name, log_file)` — jamás
  `logging.basicConfig()` a nivel de módulo ni `print()`.
- **Observabilidad**: todo job nuevo se registra en `meta` (`job_runs` +
  la tabla de tareas que le corresponda) vía `MetadataTracker`, mismo
  contrato que los jobs existentes. Sin eso, "cuánto tardó" y "terminó
  bien" solo se contestan leyendo un log.
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
`docker compose up -d migrate`. No se toca código.

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
en `docker compose up -d migrate` con el archivo y los valores ofensores.

---

## `fecha_alta_date`: se deriva en el ingest, no es un paso

`transformers/enrich.py:normalize_fecha_alta()` convierte `fecha_alta_str`
(la cadena cruda de INEGI) en `fecha_alta_date` (DATE, siempre `YYYY-MM-01`)
**en Polars, con los datos todavía en memoria, antes del `COPY`**.

No es una sección del CLI: era una etapa aparte que hacía `UPDATE` sobre la
tabla ya cargada, y derivarla en vuelo ahorra una pasada de escritura sobre
82.9M filas. Por eso no hay `--only normalize-dates`: para rehacerla hay
que re-ingestar el corte.

Se normaliza siempre al **día 1 del mes**: el dato original no trae día, así
que inventarlo sería falsear precisión.

### 6 Known fecha_alta_str Formats

| Format | Example | Rows | Pattern |
|--------|---------|------|---------|
| YYYY-MM | "2010-07" | ~68M | Most common |
| YYYY-MM (trailing space) | "2011-03 " | 15,192 | Strip then parse |
| YYYY MM (space separator) | "2013 07" | 7,383 | Replace space with - |
| YYYY_MM (underscore) | "2022_11" | 10 | Replace _ with - |
| MES_ESPANOL ANNO | "JULIO 2010" | ~14.5M | Map Spanish month -> number |
| DD/MM/YYYY | "06/10/2023" | 12 | Extract year-month |

Los volúmenes están medidos sobre el Silver completo y viven también como
comentario en `enrich.py`, junto a los regex.

**Lo que no encaja en ninguno queda NULL, y eso es información**: dice que
INEGI publicó un formato nuevo. Para detectarlo en un corte nuevo:

```sql
SELECT fecha_alta_str, count(*)
FROM silver.establecimientos
WHERE period = DATE '2026-05-01' AND fecha_alta_date IS NULL
  AND fecha_alta_str IS NOT NULL AND fecha_alta_str <> ''
GROUP BY 1 ORDER BY 2 DESC;
```

El mapa de meses en español es `SPANISH_MONTHS` en
`src/etl/transformers/enrich.py`; agregar un formato es un regex más en ese
archivo, y luego re-ingestar el corte afectado.

---

## H3 Cells: `h3_cell_9` and `h3_cell_5`

Two BIGINT columns on `silver.establecimientos` hold precomputed H3 cell
indexes derived from `latitud`/`longitud`. Valid H3 indexes always fit in a
signed BIGINT (high bit is 0).

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
- **Secondary planned consumer.** El experimento de KB consolidada (Fase 5)
  usaría `h3_cell_5` como llave de partición/poda por la misma razón.
- **Cost and exit criteria.** Keeping the column costs ~0: se deriva en la
  misma pasada que `h3_cell_9`, no tiene índice, y es un BIGINT de baja
  cardinalidad (México son del orden de miles de celdas res-5 distintas).
  Es derivable de `h3_cell_9` en cualquier momento, así que si la poda se
  rechaza la columna se puede tirar; hasta entonces se queda materializada
  para poder probar la optimización sin otra pasada de 82.9M filas.

### Cómo se pueblan las columnas

`transformers/enrich.py:add_h3_cells()` las calcula **en el ingest, con los
datos en memoria y antes del `COPY`**, con h3-py (`h3.api.basic_int`).
Guarda: coordenadas fuera de [-90,90]/[-180,180], nulas, o columnas
ausentes → celdas NULL. Un corte nuevo nace con las celdas puestas.

No hay segunda ruta: la etapa `h3-cells` que hacía un `UPDATE` por periodo
sobre la tabla ya cargada desapareció junto con `normalize-dates`, por el
mismo motivo (una pasada de escritura de más sobre 82.9M filas). Para
recalcular hay que re-ingestar el corte.

### How `h3_cell_9` drives matching

`find_h3_neighbors(ref_df, target_df, radius)` (`matching.py`) is the
candidate generator of the H3 propagation method (el que escribe
`silver.clee_h3`):

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
includes `h3_cell_9`, so the KB cache (`data/kb_cache/{radio|h3}/`)
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

- `ix_estab_h3_9` es el único índice que el método H3 necesita. No hay
  índice sobre `h3_cell_5` a propósito: la columna no tiene consumidor
  todavía, y cada índice de más sobre una tabla de 82.9M filas se paga en
  cada `COPY` de cada corte.
- El ingest escribe por partición: un corte nuevo son ~4M filas, así que el
  mantenimiento del índice es por partición y no sobre la tabla entera. Ese
  es el motivo de que ya no haya un paso de backfill con `DROP INDEX` /
  `CREATE INDEX` alrededor.

---

## CLEE Resolution Step (Entity Resolution)

El bloque DENUE de `transform` tiene cinco secciones en orden: **ingest -> clee-radio -> clee-h3 -> permanencia-radio -> permanencia-h3**. Esta seccion documenta la resolucion de identidad en detalle.

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

**Paso 1A — candidatos por RADIO (método baseline, `clee_radio.clee_resuelto`).**
Literalmente: dibujar un círculo de 500m alrededor del negocio target y
preguntar qué negocios del periodo de referencia caen adentro. Para no
comparar contra los ~4M de negocios de la referencia uno por uno, las
coordenadas de referencia se indexan en un **BallTree** (un árbol de
búsqueda espacial) con distancia **haversine** (la distancia real en
metros sobre la esfera terrestre). Es exacto, pero cada consulta recorre
el árbol de forma secuencial (un solo core).

**Paso 1B — candidatos por H3 (método nuevo, `clee_h3.clee_resuelto`).**
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
   `clee_resuelto = '14TAC12345'` (el mismo valor que ya tenía la fila
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

**Phase 1 -- Deterministic Resolution** (`_resolve_deterministic_periods`):
- Identifies periods where INEGI assigned CLEE codes (2021+ data); basta UNA
  fila con `clee` no vacío para que el corte cuente como cubierto.
- Copia la identidad del dato: `clee_resuelto = e.clee`, `origen = DIRECTO`,
  y escribe la partición del corte en `silver.clee_<metodo>`.
- These periods become the initial **resolved set** -- the anchor references for Phase 2.
- No BallTree, no fuzzy matching -- purely deterministic copy.
- **Incremental**: un corte que ya tiene su partición se conserva
  (`_ya_resuelto`), así que un corte nuevo con CLEE oficial cuesta minutos,
  no una noche.

**Phase 2 -- Probabilistic Resolution** (`_resolve_probabilistic_periods`):
- Processes older periods (2015-2020) that lack CLEE codes from INEGI.
- Uses a **sliding-window approach with an expanding reference set** (see below).
- After exhausting all references, unresolved rows get a synthetic ID: `NEW_{prefix}-{uuid_hex_12}`.
- El corte se escribe reemplazando su partición y entra al conjunto resuelto.

### Sliding Window with Expanding Reference Set

This is the core mechanism. It is **not** a fixed-size window nor a monolithic KB.

```
resolved_periods = [deterministic periods from Phase 1]  # e.g., 2021-2024

FOR each target_period P in probabilistic_periods (newest-first):
    1. Load P from Silver, con la identidad que ya tuviera (LEFT JOIN contra
       clee_<metodo>): en incremental eso conserva lo ya resuelto

    2. FOR each ref_period R in resolved_periods NEWER than P (nearest-first):
        a. If P has 0 remaining unresolved rows -> BREAK early
        b. Load R from Postgres
        c. Build ephemeral KB from R  (build_single_period_kb)
        d. Free R from RAM  (del + gc.collect)
        e. propagate_clees(target=P, ref_kb=KB_R, spatial_tol=500m)
           -> Partially fills clee_resuelto on P's matched rows
        f. Free KB_R from RAM  (del + gc.collect)

    3. assign_new_clees() for any rows still unresolved
    4. Escribe la particion de P en clee_<metodo>: se construye en una tabla
       aparte y se engancha al final, asi que hasta ese momento la particion
       anterior sigue siendo la vigente (_write_period_results)
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

`build_single_period_kb(df_ref, master_column)` (`entity_resolution.py`):
- Filters to rows with valid identity in `master_column` (non-null, non-empty)
- Selects only 11 columns (`KB_COLUMNS`): `clee_id`, `latitud`, `longitud`,
  `h3_cell_9`, `nom_estab`, `raz_social`, `nombre_act`, `tel_clean`,
  `email_clean`, `www_clean`, `cod_postal`
- `clee_id` es la identidad a propagar y es **neutral respecto al método**:
  ahí se copia el valor de `clee_resuelto` del método que corre. Por eso el
  motor de matching no sabe de radio ni de H3.
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
`CLEEResolver(method="radio")` writes `clee_radio.clee_resuelto`
(`find_spatial_neighbors`, BallTree) and `CLEEResolver(method="h3")` writes
`clee_h3.clee_resuelto` (`find_h3_neighbors`, grid_disk + exact haversine
post-filter). Both share the sliding-window engine and matching cascade
(`_propagate_clees` core + `propagate_clees_radio`/`propagate_clees_h3`
wrappers). `h3_cell_9` feeds H3 blocking; `h3_cell_5` = parent(h3_cell_9),
stored for future KB partition pruning (currently unused, near-zero cost) —
full details in the **H3 Cells** section above. Same run-order semantics per
method: force reconstruye SOLO la tabla de ese método; the KB cache is split
per method (`kb_cache/radio/` vs `kb_cache/h3/`); NEW_* synthetic IDs from
the H3 method carry a distinct prefix suffix so they never collide with
radio's.

**Una tabla por método, no una columna por método.** Es lo que permite que
`--force` de un método no toque al otro, y que ninguno de los dos escriba en
`establecimientos`: la tabla de hechos quedó de solo lectura para esta etapa
(migración `b0003saca_clee_del_hecho`).

Aísla la sección CLEE apagando el resto (ver "Comando único global"):

```bash
docker compose run --rm etl transform --only clee-radio           # BallTree -> silver.clee_radio
docker compose run --rm etl transform --only clee-h3              # H3 blocking -> silver.clee_h3
docker compose run --rm etl transform --only clee                 # ambas propagaciones
docker compose run --rm etl transform --only clee-radio --force   # rehace silver.clee_radio (+ su KB cache)
docker compose run --rm etl transform --only clee-h3 --force      # rehace silver.clee_h3 (+ su KB cache)
```

`--force` is per-method: reconstruye la tabla del método activo y limpia
solo su KB cache (`kb_cache/h3/` o `kb_cache/radio/`) — la tabla del otro
método y `establecimientos` no se tocan. **No nulifica nada antes de
recalcular**: cada corte se escribe reemplazando su partición, así que el
resultado previo sigue vigente hasta que el nuevo está completo, y una
corrida que muere a la mitad no deja la identidad en blanco.

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
| `CLEEResolver.run()` | `clee_resolver.py:685` | Entry point. Abre la corrida en `meta`, invoca `process_national()` |
| `process_national()` | `clee_resolver.py:619` | Orchestrator. Phase 1 then Phase 2 |
| `_resolve_deterministic_periods()` | `clee_resolver.py:375` | Phase 1: copia `clee` de INEGI, `origen = DIRECTO` |
| `_resolve_probabilistic_periods()` | `clee_resolver.py:462` | Phase 2: sliding window loop |
| `_get_sorted_periods()` | `clee_resolver.py:185` | Returns all periods sorted newest-first |
| `_get_periods_with_clee_coverage()` | `clee_resolver.py:206` | Classifies periods as deterministic vs. probabilistic |
| `_load_period()` | `clee_resolver.py:236` | Carga un corte de Silver con la identidad que ya tuviera |
| `_write_period_results()` | `clee_resolver.py:325` | Escribe la partición del corte en `clee_<metodo>` (staging + ATTACH) |
| `build_single_period_kb()` | `entity_resolution.py:360` | Builds ephemeral KB from one reference period |
| `_propagate_single_kb()` | `entity_resolution.py:550` | Matching engine for one KB partition (candidatos + cascade) |
| `_propagate_clees()` | `entity_resolution.py:666` | Core compartido: particiona la KB si hace falta y delega |
| `propagate_clees_radio()` / `_h3()` | `entity_resolution.py:757` / `:771` | Wrappers que inyectan el generador de candidatos |
| `assign_new_clees()` | `entity_resolution.py:786` | Assigns `NEW_{prefix}-{uuid12}` to unmatched rows |
| `find_spatial_neighbors()` | `matching.py:102` | BallTree haversine spatial search |
| `find_h3_neighbors()` | `matching.py:171` | grid_disk + post-filtro haversine exacto |
| `calculate_match_score()` | `matching.py:410` | Cascade matching: gate -> Path A/B/C |

### Tips

- Target periods are processed **newest-first** so each resolved period feeds the next; within a target, references are tried **nearest-first** (temporal distance drives precision — Fase 7, docs/findings.md).
- Peak RAM is controlled by loading only one reference period at a time (~5-8 GB).
- `--force` reconstruye la tabla del método; lo normal es NO pasarlo. Úsalo solo cuando cambia el algoritmo de matching o se corrige un seed.
- Synthetic IDs (`NEW_*`) are stable within a single run but NOT across runs with `--force`. Do not use them as permanent foreign keys in downstream systems — y es la razón por la que la permanencia se niega a reanudar si la identidad se re-resolvió a media construcción.
- **Un corte nuevo (2021+) trae CLEE oficial**, así que entra por la fase 1 y cuesta minutos. La fase 2 solo existe para los cortes 2015-2020.
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

### CLI

SESNSP se aísla con `--only sesnsp` sobre el comando global (no hay targets
`extract-sesnsp` / `transform-sesnsp`; ver "Comando único global"):

```bash
docker compose run --rm etl extract   --only sesnsp           # descubrimiento CKAN + descarga a data/bronze/sesnsp/
docker compose run --rm etl transform --only sesnsp           # CSV mas reciente -> Silver (catalogo + hechos)
docker compose run --rm etl transform --only sesnsp --force   # reprocesar el corte ya cargado
# extract / transform SIN flags ya incluyen SESNSP.
```

El `DELETE` y el `COPY` van en una transacción, así que un fallo deja la
tabla como estaba. Se registra en `meta.process_tasks` (job
`sesnsp_transform`) y el incremental es por **nombre de archivo**: un CSV
re-publicado con el mismo nombre no se reprocesa sin `--force`. Log del
stage: `data/etl_sesnsp.log`.

### Cruce negocios ↔ crimen (ejemplo)

```sql
-- Establecimientos y homicidios dolosos por municipio-año
SELECT n.anio, n.establecimientos, c.carpetas
FROM (SELECT EXTRACT(YEAR FROM period) AS anio, COUNT(DISTINCT id) AS establecimientos
      FROM silver.establecimientos WHERE cve_ent='23' AND cve_mun='005' GROUP BY 1) n
JOIN (SELECT EXTRACT(YEAR FROM fecha) AS anio, SUM(carpetas) AS carpetas
      FROM silver.sesnsp_incidencia_municipal i
      JOIN silver.sesnsp_delitos d ON d.id = i.delito_id
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

## N3: Permanencia (`silver.permanencia_<metodo>`)

Una fila por identidad resuelta, no por observación-periodo: la unidad de
análisis de supervivencia de la tesis.

```
silver.establecimientos    82.9M   una fila cada vez que INEGI lo vio
silver.permanencia_<m>     ~10M    una fila por establecimiento
```

**Una tabla por método** (`permanencia_radio`, `permanencia_h3`), igual
que `clee_radio`/`clee_h3`: el método va en el `FROM`, así que una
consulta no puede mezclarlos por descuido. Con una sola tabla y columna
`metodo`, un `WHERE` olvidado duplica cada establecimiento y los agregados
salen al doble, en silencio.

### Qué guarda y qué no

- **Ventana observada**: `primer_periodo`/`ultimo_periodo` para reportar,
  `idx_primer`/`idx_ultimo` para calcular. Los cortes distan entre 4 y 12
  meses, así que el tiempo se mide en PERIODOS y no en días.
  `duracion_periodos` es la variable de tiempo del modelo.
- **Ingredientes de censura, no el veredicto**: `cortes_tras_ultimo`,
  `huecos`, `presente_desde_el_inicio` (censura por la izquierda). El
  estatus de cierre no se almacena en ninguna parte, porque el protocolo
  llama *preliminar* a la regla: se deriva en la consulta que lo necesite
  (ver abajo), así que mover el umbral no cuesta ni una columna ni un
  objeto de base de datos.
- **Variables del establecimiento** que el protocolo nombra: tamaño
  (`categoria_tamano`, `empleados_min/max`, `delta_empleados_min`),
  `tipounieco`, `tipo_asent`, `codigo_act`, geografía (`cve_ent`,
  `cve_mun`, `cve_loc`, `ageb`, `h3_cell_5/9`).
- **Confianza en la identidad**: `cortes_directo`/`cortes_propagado`
  miden cuánto de la identidad es observado frente a inferido.
- **Cuántas variantes hubo, no cuáles**: `n_nombres`, `n_direcciones`,
  `cambio_municipio`, `cambio_actividad`, `tiene_telefono`... Los valores
  se recuperan con el join a los hechos (ver abajo). Materializarlos como
  arrays duplicaría ~3-4 GB para ahorrar un join barato justo en el caso en
  que se necesita —un establecimiento, o una muestra. El único array que se
  queda es `cortes_idx`, porque reconstruirlo exige rehacer el mapeo
  periodo→índice.

### Dos derivaciones que NO son objetos de base de datos

Ambas vivieron como vistas (`estatus_<metodo>`, `variantes_<metodo>`) y se
eliminaron en `b0006`: eran consultas guardadas sobre datos que ya están en
el warehouse, sin un solo consumidor. El SQL de referencia es este, y está
ejecutándose en `tests/unit/test_permanencia.py` (`estatus()` y
`variantes()`), así que no se puede podrir en silencio.

**La regla de cierre** — el umbral es un parámetro de la consulta, no una
constante del esquema. El protocolo lo llama preliminar; así se mueve sin
reconstruir nada:

```sql
SELECT clee_resuelto,
       CASE
           WHEN cortes_tras_ultimo = 0 THEN 'ACTIVO'      -- censura por la derecha
           WHEN cortes_tras_ultimo >= 2 THEN 'CERRADO'    -- el umbral del protocolo
           ELSE 'INCIERTO'                                -- una ausencia no basta
       END AS estatus,
       huecos > 0 AS reaparecio   -- demostró que ausencia ≠ cierre
FROM silver.permanencia_radio;
```

**Los valores que tuvo un establecimiento** — se parte de la identidad, se
filtra la tabla del método y se regresa a los hechos por su PK
`(id, period)`. Es el camino rápido porque `clee_resuelto` está indexado en
`clee_<metodo>`, y es cara solo si se pide sin filtro, que es justo la
consulta que el análisis no hace:

```sql
SELECT i.period, e.nom_estab, e.nom_vial, e.numero_ext, e.telefono,
       ep.categoria AS categoria_tamano, ta.tipo_asent
FROM silver.clee_radio i
JOIN silver.establecimientos e ON e.id = i.id AND e.period = i.period
LEFT JOIN silver.denue_estratos_personal ep ON ep.id = e.per_ocu_id
LEFT JOIN silver.denue_tipos_asentamiento ta ON ta.id = e.tipo_asent_id
WHERE i.clee_resuelto = '14010000123456'
ORDER BY i.period;
```

El reparto de trabajo sigue siendo el mismo: el **agregado sobre los 10M**
lo responde la tabla (`SELECT count(*) FROM silver.permanencia_radio WHERE
n_nombres > 1`), el **detalle de uno** lo responde el join.

**Rebuild total siempre**: los ids sintéticos `NEW_*` no son estables
entre corridas, así que no hay nada que preservar. Cada método va en su
propia transacción, para que una falla en h3 no tire lo que radio
construyó.

```bash
docker compose run --rm etl transform --only permanencia-radio
docker compose run --rm etl transform --only permanencia-h3
docker compose run --rm etl transform --only permanencia      # las dos
```

### Troceado y reanudable

El llenado **no** es un `INSERT ... SELECT` único. Va en dos fases, cada una
troceada por la llave que su grano admite, y **cada vuelta commitea**: un
fallo en la vuelta 14 no se lleva las 13 anteriores.

| fase | troceo | vueltas | por qué esa llave |
|---|---|---|---|
| `permanencia_obs_<m>` | por corte | 20 | el grano es la **observación**: cada fila es de un solo corte, y la vuelta lee una sola partición de los hechos y de la identidad |
| agregados + `INSERT` | por bloque de `md5(clee_resuelto)` | `PERMANENCIA_BLOQUES` (32) | el grano es la **identidad**, que *cruza* los cortes; trocear por año daría agregados sobre un fragmento de su vida, y por `cve_ent` partiría a los que se mudan |

```
▶ obs 9/20: cargando el corte 2019-11-01...
✔ obs 9/20: 2019-11-01 -> 4,218,903 observaciones
▶ indice y estadisticas sobre 82,896,801 observaciones...
   bloque 7/32 ventana: 312,044 filas en 18.3s
   bloque 7/32 primera: 312,044 filas en 9.1s
✔ bloque 7/32: 312,044 establecimientos en 1.2 min
```

`permanencia_obs_<m>` es **`UNLOGGED`** y no `TEMP`: una temporal muere en
el primer `COMMIT`, y el troceo necesita commitear. No escribe WAL, así que
cuesta casi lo mismo. Se borra al terminar.

**Troceo ≠ más lento.** El `GROUP BY` completo ordena 10M de grupos con una
decena de `COUNT(DISTINCT)` cada uno y se derrama a disco; los ~300k grupos
de un bloque caben en `work_mem`. Evitar el derrame suele valer más que dar
32 vueltas.

**Reanuda por default**, igual que la resolución CLEE, que salta los cortes
ya resueltos. El avance se deriva de las propias tablas —una vuelta que
falló hizo rollback y no dejó filas, así que "tiene filas" equivale a "está
completa"—, sin registro aparte:

```bash
docker compose run --rm etl transform --only permanencia            # reanuda
docker compose run --rm etl transform --only permanencia --force    # rehace
```

Si la **resolución CLEE se re-corrió** desde que se armó la tabla de
trabajo, reanudar mezclaría identidades de dos corridas (las `NEW_*` no son
estables), así que se niega y pide `--force`. Lo detecta comparando
`max(resuelto_en)` de `clee_<metodo>` contra la marca que deja en el
`COMMENT` de la tabla de trabajo.

### Registro en `meta` y decisión de reconstruir

Cada construcción abre una corrida en `meta.job_runs`
(`job_name = denue_permanencia_<metodo>`, `job_type = PERMANENCIA`) y una
tarea en `meta.process_tasks` (`task_name = target_table =
permanencia_<metodo>`, `source_path` = las dos tablas de Silver que la
alimentan). Ahí quedan `rows_extracted` (observaciones leídas),
`rows_loaded` (establecimientos escritos), `duration_seconds`, `status` y
`error_message`: lo mismo que ya registran el ingest y la resolución CLEE, y
lo que hace que "cuánto tardó" y "terminó bien" se contesten con SQL en vez
de leyendo `data/etl_permanencia.log`.

```sql
SELECT r.job_name, t.status, t.rows_extracted, t.rows_loaded,
       round(t.duration_seconds / 60) AS minutos, t.error_message
FROM meta.process_tasks t
JOIN meta.job_runs r USING (run_id)
WHERE r.job_type = 'PERMANENCIA'
ORDER BY t.created_at DESC;
```

El `end_time` de la última tarea `SUCCESS` es además el **reloj con que se
decide si hay algo que hacer**, comparado contra `max(resuelto_en)` de
`clee_<metodo>`:

| estado previo | qué hace |
|---|---|
| nunca construida, o la corrida anterior murió a la mitad | reanuda (cortes y bloques pendientes) |
| construida y la identidad no cambió | `SKIPPED`, no toca nada |
| construida pero la identidad se re-resolvió después | **reconstruye desde cero** |

El último renglón es el que importa en el pipeline semanal: la tabla
vigente quedó stale —sus agregados son de otro conjunto de identidades— y
esperar a que alguien se acordara de `--force` habría servido la
permanencia del corte anterior en silencio. El `SKIPPED` del renglón de
en medio también es trabajo ahorrado de verdad: la tabla de trabajo se
borra al terminar, así que sin él una segunda corrida rellenaría los 20
cortes (horas) para descubrir al final que todos los bloques ya estaban
escritos.

`/ops/health/pipeline` reporta `permanencia` como etapa propia, al lado de
`extract`, `transform` y `clee_resolution`.

Lo que se **pierde** con el troceo: el reemplazo todo-o-nada. Mientras
corre, la tabla destino está parcialmente llena. Se acepta porque es
derivada, no tiene consumidor hasta Gold, y rehacerla no cuesta datos.

### Latido dentro de cada vuelta

Cada `PERMANENCIA_LATIDO_SEGUNDOS` (60) un hilo consulta `pg_stat_activity`
con su propia conexión —la de la carga está bloqueada:

```
⏳ bloque 7/32 ventana: 2.0 min | estado=active | IO:BufFileWrite | temp +4.1 GiB
```

`wait_event` dice en qué fase está (leyendo hechos, derramando a disco,
esperando un lock). Lo que **no** se puede reportar es "filas escritas hasta
ahora": la vuelta vive en una transacción sin commitear, así que desde otra
conexión la tabla se ve sin esas filas hasta el final.

Y si prefieres preguntarle a la base:

```sql
SELECT pid, state, wait_event_type, wait_event, now() - query_start AS corriendo
FROM pg_stat_activity
WHERE datname = 'mipymes' AND state <> 'idle';
```

Log del stage: `data/etl_permanencia.log`. **Los tests escriben en ese mismo
archivo** (el logger se construye a nivel de módulo con ruta fija), así que
las líneas de 4 cortes / 7 establecimientos son de `pytest`, no de una
corrida real.

---

## Fase 7: Evaluación con CLEE enmascarado (`scripts/evaluate_masked_clee.py`)

Mide precision/recall de la cascada de matching usando el `clee` OFICIAL
de INEGI como ground truth (no depende de `clee_<metodo>`, así que
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
docker compose run --rm tools python scripts/evaluate_masked_clee.py extract   # lectura Silver -> parquets
docker compose run --rm tools python scripts/evaluate_masked_clee.py evaluate           # nearest-first (produccion) -> *_nearest
docker compose run --rm tools python scripts/evaluate_masked_clee.py evaluate --ref-order newest   # orden legado, para comparar
docker compose run --rm tools python scripts/evaluate_masked_clee.py chain     # dos saltos: target via intermedio resuelto
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
tablas `silver.clee_<metodo>` tal como quedaron en el warehouse real
(82.9M filas, siempre de solo lectura).

```bash
docker compose run --rm tools python scripts/clee_propagation_stats.py                           # radio + h3
docker compose run --rm tools python scripts/clee_propagation_stats.py --method radio            # un solo metodo
docker compose run --rm tools python scripts/clee_propagation_stats.py --no-identity-metrics     # salta el GROUP BY caro
```

Correrlo durante una resolución ya no bloquea nada (PostgreSQL admite
lectores concurrentes), aunque las cifras saldrán de un estado a medio
resolver.

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

Flags: `--method` (repetible; default = las claves del registro
`CLEEResolver.METHODS`), `--no-identity-metrics` (salta el GROUP BY caro
por identidad), `--no-agreement`, `--out`.

Línea base medida el 2026-07-30 (post nearest-first) en
`docs/findings.md` — incluye el dato del orden legado para comparar.

---

## Project Overview

**Name:** `tesis-core` (v0.1.0)
**Purpose:** ETL pipeline analyzing mortality and survival of SMEs in Mexico using INEGI's DENUE microdata.

- **Language:** Python 3.12+
- **Package Manager:** uv
- **Frameworks:** FastAPI (API), Typer (CLI), SQLAlchemy 2.0 (ORM), Alembic (migrations)
- **Base de datos:** PostgreSQL 17 — una base, tres schemas (`silver`,
  `gold`, `meta`); driver psycopg3, escrituras masivas con `COPY`
- **DataFrames:** Polars
- **Almacenamiento:** `silver.establecimientos` particionada por `period`;
  los seeds de catálogos, en Parquet versionado
- **Ejecución:** Docker Compose, sin nada nativo
- **Architecture:** Hexagonal/Clean Architecture with medallion data lake (Bronze/Silver/Gold)
- **Design Decision:** Monolithic ETL -- YAGNI; single service doesn't justify microservices

---

## Directory Layout

Cuatro paquetes bajo `src/`, y `shared/` es el único que los otros tres
pueden importar (`arquitectura.md` §4.1; lo verifica
`tests/unit/test_layer_boundaries.py`).

```
etl-core/
├── src/shared/                    # Sin dependencias sobre etl/api/meta
│   ├── config.py                  # Settings (pydantic-settings), DATABASE_URL
│   ├── logging.py                 # Logger estructurado + get_stage_logger()
│   ├── db/
│   │   ├── base.py                # Base declarativa, SCHEMAS, get_table()
│   │   ├── session.py             # get_engine() / get_session_factory()
│   │   ├── copy.py                # COPY ... FROM STDIN con latido
│   │   ├── partitions.py          # create/attach/detach/drop de particiones
│   │   ├── staging.py             # Carga en tabla aparte y ATTACH al validar
│   │   ├── query.py               # silver_read_query() para el matching
│   │   ├── dependencies.py        # Dependencias de FastAPI
│   │   └── models/
│   │       ├── silver.py          # establecimientos + catalogos + sesnsp
│   │       ├── clee.py            # clee_radio / clee_h3 (identidad resuelta)
│   │       ├── permanencia.py     # permanencia_radio / permanencia_h3
│   │       ├── meta.py            # job_runs, download/process/clee tasks, lineage
│   │       └── gold.py            # Sin tablas todavia
│   └── enums/                     # jobs, sections, clee, layer, sources
│
├── src/etl/
│   ├── main.py                    # CLI (Typer): extract, sync-bronze, transform, pipeline
│   ├── core/download_config.py    # Reintentos y timeouts de descarga
│   ├── db/indexes.py              # Suspension de indices (hoy sin llamadores)
│   ├── extractors/
│   │   ├── denue.py               # Scraping INEGI (Selenium) -> Bronze
│   │   ├── sesnsp.py              # Descubrimiento CKAN + descarga
│   │   └── bronze_sync.py         # Registra en meta un Bronze que llego por copia
│   ├── transformers/
│   │   ├── denue.py               # Orquestador de las secciones DENUE
│   │   ├── processor.py           # Ingest: ZIP -> particion de silver.establecimientos
│   │   ├── enrich.py              # fecha_alta_date + h3_cell_9/5, antes del COPY
│   │   ├── catalogs.py            # Resolucion de FKs a catalogo (fail-fast)
│   │   ├── clee_resolver.py       # CLEEResolver: identidad a traves del tiempo
│   │   └── sesnsp.py              # CSV ancho -> largo + catalogo de delitos
│   ├── loaders/
│   │   ├── permanencia.py         # silver.permanencia_<metodo> (troceada, reanudable)
│   │   ├── seeds.py               # Orquestador de la siembra
│   │   ├── geography.py           # entidades / municipios / localidades
│   │   ├── scian.py               # Actividades economicas
│   │   ├── estratos.py            # denue_estratos_personal
│   │   ├── dominios.py            # vialidad / asentamiento / unidad economica
│   │   └── sesnsp.py              # Catalogo de delitos (append-only)
│   └── utils/
│       ├── browser.py             # Chrome driver para Selenium
│       ├── denue_schema.py        # Mapeo de columnas, enums, alias de dominio
│       ├── denue_scraping.py      # Scraping de la tabla de INEGI
│       ├── downloader.py          # HTTP con reintentos
│       ├── file_validator.py      # SHA256, tamano, validez del ZIP
│       ├── entity_resolution.py   # Motor de propagacion (KB + cascada)
│       ├── matching.py            # Candidatos (BallTree / H3) + score de cascada
│       └── clee_resolution_config.py  # Umbrales .env + constantes estructurales
│
├── src/api/                       # FastAPI (solo lee gold y meta)
│   ├── main.py                    # App, lifespan, CORS
│   └── routers/ops.py             # /ops/runs, /ops/health/pipeline
│
├── src/meta/tracker.py            # MetadataTracker: ciclo de vida de corridas
│
├── data/
│   ├── bronze/denue|sesnsp/       # Descargas crudas (no committed)
│   ├── kb_cache/{radio|h3}/       # KB por metodo (parquet, regenerable)
│   ├── eval/                      # Salidas de la Fase 7 (no committed)
│   ├── etl_*.log                  # Log por stage
│   └── seeds/                     # Parquet committed: la fuente de verdad
│       ├── seed_entidades.parquet            # 32
│       ├── seed_municipios.parquet           # 2,486 (incl. 998/999 del SESNSP)
│       ├── seed_localidades.parquet          # 40,388
│       ├── seed_actividades.parquet          # 1,127 SCIAN
│       ├── seed_estratos_personal.parquet    # 8
│       ├── seed_tipos_{vialidad,asentamiento,unidad_economica}.parquet
│       └── seed_sesnsp_delitos.parquet       # 98 (ids append-only)
│
├── alembic/versions/              # 7 revisiones, b0000 (baseline) -> b0006
├── tests/{unit,integration}/      # conftest.py levanta el Postgres de pruebas
├── AGENTS.md                      # Esta guia
├── ARRANQUE.md                    # Runbook de arranque en limpio
├── arquitectura.md                # Decisiones de arquitectura (la migracion)
├── Dockerfile.etl                 # Imagen con Chrome para el scraping
├── docker-compose.yml             # Unica interfaz de ejecucion
├── pyproject.toml / uv.lock       # Dependencias
└── pytest.ini                     # Marcadores: unit, integration, postgres
```

---

## Build Commands

Todo corre en contenedor. **No hay Makefile ni ejecución nativa**: `uv` y el
venv solo existen dentro de las imágenes, y `docker-compose.yml` es la única
interfaz. El inventario completo está más abajo en "Comandos".

```bash
docker compose --profile manual --profile tools build   # construye AMBAS imágenes
docker compose up -d                 # Postgres + migraciones + API

docker compose run --rm etl pipeline                      # ETL
docker compose run --rm tools pytest                      # tests
docker compose run --rm tools ruff check src/ tests/      # lint
docker compose exec postgres psql -U etl_rw -d mipymes    # SQL
```

Dos imágenes desde un solo `Dockerfile.etl`:

| Target | Lleva | Para qué | Arquitectura |
|---|---|---|---|
| `tools` | deps del proyecto + pytest/ruff | migraciones, tests, scripts, API | portable (arm64 y amd64) |
| `etl` | lo anterior + Chrome | el pipeline (el scraping de DENUE usa Selenium) | amd64 |

Chrome vive solo en `etl` porque lo usan dos archivos (`utils/browser.py`,
`utils/denue_scraping.py`). Tenerlo en la base obligaba a que tests y
migraciones cargaran un navegador de ~500 MB que no usan, y volvía la imagen
inconstruible en arm64.

---

## Comandos — inventario completo

`docker-compose.yml` es la **única** interfaz de ejecución: no hay Makefile ni
ejecución nativa. Los servicios one-shot (`etl`, `tools`) van bajo profile para
que `up` no los arranque; `docker compose run` activa su profile solo.

Los flags del CLI van directo al final del comando, sin `ARGS=`:

```bash
docker compose run --rm etl transform --only clee-radio --force
```

### Levantar y bajar el stack

| Comando | Qué hace |
|---|---|
| `docker compose --profile manual --profile tools build` | Construye ambas imágenes. **Los profiles no son opcionales**: `docker compose build` a secas salta los servicios bajo profile (`etl` es uno) y se queda con la imagen vieja sin avisar. Nombrar el servicio (`docker compose build etl`) también funciona |
| `docker compose up -d` | Postgres → migraciones → API. Deja la base lista y migrada |
| `docker compose down` | Detiene todo, **conserva** el volumen de datos |
| `docker compose down -v` | **Destructivo**: borra el volumen `etl-pgdata` |
| `docker compose logs -f postgres` | Sigue los logs de la base |

### ETL (comandos globales — sin flags corren TODO)

El CLI tiene **cuatro** comandos, no más:

| Comando | Notas |
|---|---|
| `docker compose run --rm etl sync-bronze` | Registra en `meta` un Bronze que llegó por copia; idempotente |
| `docker compose run --rm etl extract` | DENUE (Selenium) + SESNSP (CKAN) → Bronze |
| `docker compose run --rm etl transform` | DENUE + SESNSP → Silver. `--period 03_2018` carga un corte solo |
| `docker compose run --rm etl pipeline` | `[Extract →] Transform` |

No hay `load` ni `retry`: Gold no tiene tablas todavía, así que no hay nada
que promover, y el reintento se hace volviendo a correr la sección (todas
son incrementales).

### Evaluación Fase 7

| Comando | Notas |
|---|---|
| `docker compose run --rm tools python scripts/evaluate_masked_clee.py extract` | Silver read-only → parquets en `data/eval/` |
| `docker compose run --rm tools python scripts/evaluate_masked_clee.py evaluate` | Default `--ref-order nearest` (producción) |
| `docker compose run --rm tools python scripts/evaluate_masked_clee.py chain` | Experimento de dos saltos |
| `docker compose run --rm tools python scripts/clee_propagation_stats.py` | Cobertura/salud de `clee_<metodo>`; Silver read-only |

### Base de datos y migraciones

| Comando | Qué hace |
|---|---|
| `docker compose up -d migrate` | `alembic upgrade head`. Lo hace solo `docker compose up -d` |
| `docker compose run --rm tools alembic current --verbose` | Revisión aplicada + historial |
| `docker compose run --rm tools alembic downgrade -1` | Revierte una revisión |
| `docker compose exec postgres psql -U etl_rw -d mipymes` | Sesión psql |
| `docker compose exec postgres createdb -U etl_rw mipymes_test` | Base de pruebas (idempotente: falla si ya existe) |

### Tests y calidad

| Comando | Qué hace |
|---|---|
| `docker compose run --rm tools pytest` | Suite completa |
| `docker compose run --rm tools pytest -m unit` | Solo unitarios (`-m integration`, `-m db` análogos) |
| `docker compose run --rm tools pytest -m postgres` | Los que exigen servidor; se saltan solos sin él |
| `docker compose run --rm tools pytest --cov=src --cov-report=html` | Coverage → `htmlcov/index.html` |
| `docker compose run --rm tools pytest -m postgres` | Solo lo que exige servidor |
| `docker compose run --rm tools ruff check src/ tests/ scripts/ alembic/` | Lint (`--fix` para corregir, `ruff format` para formatear) |

### Inventario de flags del CLI

Los booleanos son flags de Typer (`--x/--no-x`); la columna Default dice
cómo arranca **sin** pasar nada.

| Comando | Flag | Default | Efecto |
|---|---|---|---|
| `extract` | `--denue/--no-denue` | ON | Descarga DENUE (Selenium, INEGI) |
| | `--sesnsp/--no-sesnsp` | ON | Descarga SESNSP (CKAN) |
| | `--only <token>` (repetible) | — | Allowlist; aquí solo aplican `denue` y `sesnsp` |
| | `--headless/--no-headless` | ON | Navegador headless (solo DENUE) |
| | `--output PATH` | `data/bronze/denue` | Override de destino (solo DENUE) |
| | `--force` | OFF | Re-descarga TODOS los archivos |
| `sync-bronze` | `--bronze PATH` | `data/bronze/denue` | Directorio a registrar en `meta` |
| `transform` | `--denue/--no-denue` | ON | Bloque DENUE completo |
| | `--ingest/--no-ingest` | ON | CSVs crudos → Silver |
| | `--clee-resolution-radio/--no-…` | ON | Propagación radio/BallTree |
| | `--clee-resolution-h3/--no-…` | ON | Propagación H3 (track de comparación) |
| | `--permanencia-radio/--no-…` | ON | `silver.permanencia_radio` |
| | `--permanencia-h3/--no-…` | ON | `silver.permanencia_h3` |
| | `--sesnsp/--no-sesnsp` | ON | Transform SESNSP |
| | `--period MM_YYYY` (repetible) | — | Carga SOLO esos cortes (solo afecta al ingest) |
| | `--only <token>` (repetible) | — | Allowlist (gana sobre los `--no-*`) |
| | `--force` / `-f` | OFF | Reprocesa todo (semántica por sección, ver abajo) |
| `pipeline` | `--extract/--no-extract` | **OFF** | Incluye la descarga (re-scrapea INEGI) |
| | *(mismos flags de sección que `transform`)* | ON | — |
| | `--only <token>` (repetible) | — | Allowlist |
| | `--headless/--no-headless` | ON | Solo si `--extract` |
| | `--force` / `-f` | OFF | Reprocesa todo |

### Flags del script de evaluación (`scripts/evaluate_masked_clee.py`)

| Subcomando | Flag | Default | Efecto |
|---|---|---|---|
| `extract` | `--target-period YYYY-MM-DD` | `2021-05-01` | Periodo objetivo a enmascarar |
| | `--state <cve_ent>` (repetible) | CDMX/Jalisco/Zacatecas | Muestra territorial |
| `evaluate` | `--ref-order nearest\|newest` | `nearest` | `nearest` = producción; `newest` = orden legado |
| `chain` | `--intermediate YYYY-MM-DD` | `2021-11-01` | Periodo intermedio del salto (debe estar en la muestra) |

### Comando único global (extract / transform / pipeline)

`extract`, `transform` y `pipeline` son **comandos globales**: sin
argumentos corren **todas** las fuentes y secciones. No hay que especificar
la fuente ni correr comandos por separado; el default es "hazlo todo" y cada
sección se **desactiva** con su flag `--no-*`. No existe `--source` **ni**
targets tipo `extract-sesnsp` / `clee-resolution-radio`: **todo se hace con
el comando global + flags**.

- `docker compose run --rm etl extract` → DENUE **+** SESNSP (Bronze).
- `docker compose run --rm etl transform` → DENUE (`ingest → clee-radio →
  clee-h3 → permanencia-radio → permanencia-h3`) **+** SESNSP, todo en una
  sola corrida.
- `docker compose run --rm etl pipeline` → el transform global (agrega
  `--extract` para incluir la descarga; va OFF por default porque re-scrapea
  INEGI).

Hay **dos modos** para elegir secciones:

- **Substractivo — `--no-*`** (default ON): corre todo y apaga lo que listes.
- **Allowlist — `--only`** (repetible): corre SOLO lo que listes; el resto
  arranca OFF. Ideal para aislar una sección sin escribir la cadena larga de
  `--no-*`. `--only` gana sobre los `--no-*`. Tokens inválidos → error con la
  lista de válidos (validado por Enum).

| Sección | Flag opt-out (`--no-*`) | Token `--only` |
|---------|-------------------------|----------------|
| bloque DENUE completo | `--no-denue` | `denue` (grupo) |
| ingest de CSVs a Silver | `--no-ingest` | `ingest` |
| propagación CLEE radio/BallTree | `--no-clee-resolution-radio` | `clee-radio` |
| propagación CLEE H3 (comparación) | `--no-clee-resolution-h3` | `clee-h3` |
| ambas propagaciones CLEE | — | `clee` (grupo) |
| permanencia radio | `--no-permanencia-radio` | `permanencia-radio` |
| permanencia H3 | `--no-permanencia-h3` | `permanencia-h3` |
| ambas permanencias | — | `permanencia` (grupo) |
| transform SESNSP | `--no-sesnsp` | `sesnsp` |

Qué tokens acepta cada comando: `transform` y `pipeline` los atómicos DENUE
+ `sesnsp` + grupos; `extract` solo distingue fuente (`denue` / `sesnsp` —
cualquier token atómico de DENUE selecciona la fuente completa).

**Recetario** con `--only`:

```bash
# Solo SESNSP:
docker compose run --rm etl extract   --only sesnsp
docker compose run --rm etl transform --only sesnsp

# Poblar Silver SIN propagar CLEE (y por lo tanto sin permanencia):
docker compose run --rm etl transform --only ingest --only sesnsp

# Un solo corte, para estrenar el ingest antes de soltar los veinte:
docker compose run --rm etl transform --only ingest --period 03_2018

# Solo CLEE radio; +" --force" para rehacer la tabla del metodo:
nohup docker compose run --rm etl transform --only clee-radio > clee.log 2>&1 &

# Solo la permanencia (va DESPUES de la resolucion, lee clee_<metodo>):
nohup docker compose run --rm etl transform --only permanencia > permanencia.log 2>&1 &
```

**Combinaciones frecuentes** (fin a fin):

```bash
# Rebuild TOTAL desde cero, descarga incluida (dias):
nohup docker compose run --rm etl pipeline --extract --force > pipeline.log 2>&1 &

# Rebuild de Silver SIN volver a scrapear INEGI (default de pipeline):
nohup docker compose run --rm etl pipeline --force > pipeline.log 2>&1 &

# Silver completo pero SIN el track de comparacion H3 (ahorra ~14h):
nohup docker compose run --rm etl transform \
  --no-clee-resolution-h3 --no-permanencia-h3 > transform.log 2>&1 &

# Re-resolver CLEE radio y reconstruir lo que depende de esa identidad:
nohup docker compose run --rm etl transform --only clee-radio --force > clee.log 2>&1 &
docker compose run --rm etl transform --only permanencia-radio   # detecta la identidad nueva y rehace

# Ciclo de evaluacion Fase 7 completo (no toca Silver en escritura):
docker compose run --rm tools python scripts/evaluate_masked_clee.py extract
docker compose run --rm tools python scripts/evaluate_masked_clee.py evaluate                      # nearest (produccion)
docker compose run --rm tools python scripts/evaluate_masked_clee.py evaluate --ref-order newest   # orden legado
docker compose run --rm tools python scripts/evaluate_masked_clee.py chain                         # dos saltos

# Antes de cualquier corrida larga: verificar esquema
docker compose run --rm tools alembic current --verbose
```

> Nota de costo: con el default "todo ON", `docker compose run --rm etl transform` corre la
> propagación CLEE **radio y H3** (la H3 es un track de comparación) y las
> dos permanencias. Son horas cada una; si solo necesitas producción, pasa
> `--no-clee-resolution-h3 --no-permanencia-h3`.

> Todo lo que incluya propagación CLEE, permanencia o un `--force` global va
> con `nohup ... &` (ver "Regla de corridas largas" arriba).

### Qué hace `--force` en cada sección (leer ANTES de pasarlo)

`--force` NO significa lo mismo en todas partes, y en todas es "tira el
avance y rehaz". Tabla canónica:

| Sección | Qué hace `--force` | Costo / riesgo |
|---|---|---|
| `extract` | Re-descarga TODOS los archivos (ignora lo ya bajado) | Horas de scraping a INEGI |
| `ingest` | Reconstruye **los 20 cortes**: cada uno se re-lee de sus ZIP, se carga en staging y reemplaza su partición | ~27.5h. No vacía la tabla de golpe: cada partición se reemplaza entera y Silver nunca se ve a medias. Los `id` son hash del contenido, así que re-ingestar los MISMOS ZIP los reproduce idénticos y `clee_<metodo>` sigue cuadrando; lo que sí la deja huérfana es un corte re-descargado con correcciones (esas filas cambian de `id`) |
| `clee-radio` | Reconstruye `silver.clee_radio` (+ limpia su KB cache) | ~1 noche; no toca `clee_h3` ni los hechos |
| `clee-h3` | Idem para `silver.clee_h3` | ~1 noche; no toca `clee_radio` |
| `permanencia-radio` / `-h3` | Descarta el avance y reconstruye `silver.permanencia_<metodo>` | Horas. Rara vez necesario: si la identidad cambió, la etapa lo detecta y rehace sola |
| `sesnsp` | Re-procesa el CSV consolidado | Minutos |

**Regla práctica**: un `--force` global (`transform --force`) arrastra al
`ingest` y con él a las ~27.5h y a la re-resolución. Usa `--only` para
acotar a qué secciones aplica:

```bash
# Force SIN re-ingestar (rehace solo lo que pediste sobre el Silver actual):
nohup docker compose run --rm etl transform --only clee-radio --force > clee.log 2>&1 &

# Re-resolver UNA sola propagación (la tabla del otro método queda intacta):
nohup docker compose run --rm etl transform --only clee-h3 --force > clee_h3.log 2>&1 &
```

**El orden importa después de un `--force` de identidad.** Reconstruir
`clee_<metodo>` deja la permanencia de ese método stale; la propia etapa lo
nota (compara `max(resuelto_en)` contra su última tarea `SUCCESS` en `meta`)
y reconstruye sin que haya que pedírselo:

```bash
docker compose run --rm etl transform --only clee-radio --force
docker compose run --rm etl transform --only permanencia-radio   # rehace sola
```

---

## Architecture

### ETL Pipeline Flow

Tres comandos (`src/etl/main.py`), y dentro de cada uno secciones
seleccionables con `--only` / `--no-*`. `pipeline` = `extract` (opt-in) +
`transform`.

```
extract
  denue    DenueExtractor (Selenium)   -> data/bronze/denue/<corte>/*.zip
  sesnsp   SesnspExtractor (CKAN)      -> data/bronze/sesnsp/*.csv
           bronze_sync                  registra en meta lo que ya esta en disco

transform  (en este orden; cada seccion lee lo que la anterior escribio)
  ingest             ZIP           -> silver.establecimientos   (particionada por corte)
  clee-radio         Silver        -> silver.clee_radio          (identidad, BallTree)
  clee-h3            Silver        -> silver.clee_h3             (identidad, blocking H3)
  permanencia-radio  Silver+clee   -> silver.permanencia_radio   (una fila por establecimiento)
  permanencia-h3     Silver+clee   -> silver.permanencia_h3
  sesnsp             CSV           -> silver.sesnsp_incidencia_municipal
```

Ni la fecha de alta ni las celdas H3 son secciones: el ingest las deriva
antes del `COPY` (`transformers/enrich.py`).

Qué registra cada sección en `meta`:

```
  extract            -> download_tasks         (job_type DOWNLOAD)
  ingest             -> process_tasks          (job_type TRANSFORM, una tarea por ZIP)
  sesnsp             -> process_tasks          (job_type TRANSFORM, job sesnsp_transform)
  clee-radio | -h3   -> clee_resolution_tasks  (job_type CLEE_RESOLUTION)
  permanencia-*      -> process_tasks          (job_type PERMANENCIA, una tarea por metodo)
```

### Metadata Layer

Mismo PostgreSQL que los datos (base `mipymes`, schema `meta`), no un
almacén aparte: que esté en la misma base es lo que permite hacer JOIN
entre una tarea y las filas que describe.

```
schema meta  (5 tablas + alembic_version)

  job_runs                  una corrida: job_name, job_type, status, duracion
    ├─ download_tasks       un archivo descargado (reintentos, hash, velocidad)
    ├─ process_tasks        un ZIP/CSV ingestado, o una tabla derivada completa
    └─ clee_resolution_tasks  una pasada de resolucion de identidad
  data_lineage              origen -> destino de cada escritura

Las tres tablas de tareas cuelgan de job_runs con FK ON DELETE CASCADE.
`data_lineage` la referencia opcional: hay linaje que no nace de una tarea
(la siembra de un catalogo, por ejemplo).
```

### Storage Layer

Silver, Gold y meta viven en PostgreSQL. En disco solo queda lo que entra
al pipeline, lo regenerable y los logs.

```
data/bronze/denue/<corte>/*.zip   # descargas crudas de INEGI (484 archivos, ~9.1G)
data/bronze/sesnsp/*.csv          # CSV consolidado del SESNSP, uno por corte publicado
data/seeds/*.parquet              # catalogos versionados: la fuente de verdad
data/kb_cache/{radio|h3}/         # KB de referencia por metodo (parquet, regenerable)
data/etl_*.log                    # un log por stage (ver abajo)
```

`gold` existe como schema desde el baseline pero todavía no tiene tablas:
no hay nada que promover.

### Logging Layer

Dos mecanismos distintos. El **log por stage** es un archivo de texto por
etapa, que es lo que se le hace `tail -f` durante las horas que tarda:

```
data/etl_process.log                      # ingest
data/etl_clee_resolution_{radio|h3}.log   # resolucion de identidad, uno por metodo
data/etl_permanencia.log                  # permanencia, los dos metodos
data/etl_sesnsp.log                        # SESNSP
```

El **log estructurado** es solo de las descargas, que son cientos de
tareas con reintentos y necesitan algo consultable:

```
logs/downloads/YYYY-MM-DD/run_TIMESTAMP/
├── app.log       (JSON, una linea por evento)
└── errors.log    (solo errores)
logs/downloads/latest -> la corrida mas reciente
```

---

## Database

### ORM Models (15 tables en `silver`)

| Model | Table | Primary Key | Rows |
|-------|-------|------------|------|
| `Establecimiento` | `establecimientos` | `(id, period)` | 82.9M |
| `CleeRadio` | `clee_radio` | `(id, period)` | 82.9M |
| `CleeH3` | `clee_h3` | `(id, period)` | 82.9M |
| `PermanenciaRadio` | `permanencia_radio` | `clee_resuelto` | ~10M |
| `PermanenciaH3` | `permanencia_h3` | `clee_resuelto` | ~10M |
| `SesnspDelito` | `sesnsp_delitos` | `id` (surrogate, seed order) | 98 |
| `SesnspIncidenciaMunicipal` | `sesnsp_incidencia_municipal` | `(cve_ent, cve_mun, delito_id, fecha)` | ~30.7M |
| `ActividadEconomica` | `denue_actividades_economicas` | `codigo_act` | 1,127 |
| `EstratoPersonal` | `denue_estratos_personal` | `id` (surrogate) | 8 |
| `TipoVialidad` | `denue_tipos_vialidad` | `id` (surrogate) | 39 |
| `TipoAsentamiento` | `denue_tipos_asentamiento` | `id` (surrogate) | 45 |
| `TipoUnidadEconomica` | `denue_tipos_unidad_economica` | `id` (surrogate) | 3 |
| `Entidad` | `denue_entidades` | `cve_ent` | 32 |
| `Municipio` | `denue_municipios` | `(cve_ent, cve_mun)` | 2,486 |
| `Localidad` | `denue_localidades` | `(cve_ent, cve_mun, cve_loc)` | 40,388 |

Las tres primeras están **particionadas por corte** (`period`); las cuentas
de catálogo son las que imprime la siembra de la migración.

### Los seeds parquet son la fuente de verdad de los catálogos

**Regla:** el contenido de todo catálogo vive en `data/seeds/*.parquet`
(versionados en git) y en ningún otro lado. NO hay enums de Python que lo
dupliquen — `src/etl/db/enums.py` se eliminó justamente por eso: los mismos
valores existían como enum, como parquet generado desde el enum, y como
lista `DOMAIN_CATALOGS` contra la que validaba el ingest, así que dar de
alta una etiqueta nueva de INEGI obligaba a tocar código.

Aplica a **todo catálogo que tenga seed**, DENUE y SESNSP por igual:
`sesnsp_delitos` también se siembra desde su parquet en la migración, y el
transform de cada corte anual solo **valida y enlaza** contra él.

**Dar de alta un valor nuevo = editar el parquet y correr `docker compose up -d migrate`.
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
(entidades) — los cinco hacen fallar `docker compose up -d migrate` nombrando archivo,
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
- **Las tablas particionadas no llevan FK saliente hacia los hechos**:
  `clee_<metodo>.id` es FK *lógica* a `establecimientos.id`, sin constraint,
  porque un FK real bloquearía el `ATTACH`/`DROP` de particiones que da el
  reemplazo atómico por corte.

### Índices: no hay comando manual, y ya no hace falta suspenderlos

Con PostgreSQL particionado el problema se resolvió por la vía del
particionado, no por la de soltar índices:

- **El ingest carga en una tabla de staging** que Silver no ve
  (`shared/db/staging.py`), la valida, y la engancha como partición con un
  `ATTACH` —operación de catálogo, instantánea—. El índice que se mantiene
  durante el `COPY` es el de una tabla de ~4M filas, no el de la de 82.9M; y
  si la carga muere a la mitad se tira la tabla de staging y Silver nunca se
  enteró.
- **La resolución CLEE ya no escribe en `establecimientos`**: escribe en
  `silver.clee_<metodo>`, también por reemplazo de partición. Los índices de
  la tabla de hechos se quedan donde están porque esa etapa solo la LEE, y
  los necesita para el pruning y los filtros por corte.
- Un índice creado sobre el padre es un **índice particionado**: se propaga
  solo a cada partición nueva. Nadie tiene que acordarse de crearlo para el
  corte que acaba de llegar.

`src/etl/db/indexes.py` conserva el mecanismo de suspensión
(`suspended_indexes()`, `ensure_indexes()`, definición derivada de
`Base.metadata`) **pero hoy no tiene ningún llamador en el pipeline**: lo
ejercitan solo sus tests. Está ahí por si una carga masiva vuelve a
necesitarlo; no asumir que el ingest lo usa.

### Establecimiento (43 columns)

| Category | Columns |
|----------|---------|
| PK | `(id, period)` |
| Identification | `clee` (el que publica INEGI, 2021+), `nom_estab`, `raz_social` |
| Economic (FKs) | `codigo_act`, `tipounieco_id`, `per_ocu_id` |
| Address | `tipo_vial_id` (FK), `nom_vial`, `numero_ext`, `letra_ext`, `numero_int`, `letra_int`, `edificio`, `edificio_e` |
| Cross Streets | `tipo_v_e_1/2/3`, `nom_v_e_1/2/3` |
| Settlement | `tipo_asent_id` (FK), `nomb_asent`, `nom_cencom`, `num_local`, `cod_postal` |
| Geographic | `cve_ent`, `cve_mun`, `cve_loc`, `ageb`, `manzana` |
| Coordinates | `latitud`, `longitud` |
| H3 cells | `h3_cell_9` (res 9, blocking key), `h3_cell_5` (res 5 = parent of `h3_cell_9`, reserved) — see **H3 Cells** section |
| Contact | `telefono`, `correoelec`, `www` |
| Temporal | `fecha_alta_str`, `fecha_alta_date`, `period` |

### La identidad resuelta NO es una columna del hecho: es `silver.clee_<metodo>`

Fue una columna (`clee_propagado_radio` / `_h3`) y dejó de serlo en la
migración `b0003saca_clee_del_hecho`. Hoy cada método tiene su tabla, con la
misma llave y el mismo particionado que los hechos:

| Columna | Qué guarda |
|---|---|
| `id`, `period` | PK; `id` es FK **lógica** a `silver.establecimientos` (sin constraint, para no bloquear el swap de particiones) |
| `clee_resuelto` | La identidad asignada a esa observación |
| `origen` | `DIRECTO` (el `clee` de INEGI), `PROPAGADO` (heredado por matching) o `NUEVO` (sintético `NEW_{prefix}-{uuid12}`) |
| `resuelto_en` | Cuándo se produjo la fila. Es lo que permite citar un resultado congelado en la tesis, y el reloj con que la permanencia decide si reconstruir |

Por qué una tabla y no una columna:

- **El método se aísla**: rehacer radio no toca H3, y ninguno escribe en la
  tabla de hechos (que para esta etapa es de solo lectura).
- **El reemplazo es atómico por corte**: `DROP` de la partición vieja +
  `ATTACH` de la nueva en la misma transacción. Nunca hay un corte sin
  identidad a la vista, que es lo que pasaba al nulificar la columna antes
  de recalcular.
- **`origen` y `resuelto_en` no caben en el hecho**: son propiedades de la
  corrida que resolvió, no del establecimiento.
- El join sigue siendo barato porque `period` está en las dos PK: es
  partition-wise, 20 joins de ~4M en vez de uno de 82.9M × 82.9M
  (`docs/separacion_ingest_clee.md` §3).

### Índices de `silver.establecimientos`

Los declara el modelo ORM (`models/silver.py`), así que la migración y el
código no pueden divergir. Son índices **particionados**: creados sobre el
padre, se propagan solos a cada partición nueva.

| Índice | Columnas | Para qué |
|-------|---------|-----------|
| `ix_estab_h3_9` | `h3_cell_9` | Blocking del método CLEE H3 |
| `ix_estab_geo` | `cve_ent, cve_mun, cve_loc` | Consulta geográfica (la más común) |
| `ix_estab_activity_period` | `codigo_act, period` | Actividad económica en el tiempo |
| `ix_estab_composite` | `cve_ent, codigo_act, per_ocu_id` | Estado + giro + tamaño |
| `ix_estab_coords` | `latitud, longitud` | Consultas espaciales |
| `ix_estab_type_size` | `tipounieco_id, per_ocu_id` | Filtro por tipo de unidad |
| `ix_silver_establecimientos_{clee,cod_postal,codigo_act,cve_ent,cve_mun}` | una columna cada uno | Vienen de `index=True` en el modelo |

Deliberadamente **sin** índice: `h3_cell_5` (sin consumidor) y `period`
(es la clave de partición, así que dentro de cada partición es constante:
20 árboles que mantener para cero selectividad).

No declarar en `__table_args__` un índice de una sola columna que ya trae
`index=True`: se generan dos sobre el mismo campo. Así nacieron
`ix_estab_clee` e `ix_estab_postal`, eliminados en la migración `a4000idx`.

### Backend Configuration

Un solo motor: **PostgreSQL**, una base con tres schemas. No hay flag de
backend que elegir.

```bash
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=mipymes
POSTGRES_USER=etl_rw          # dueño: escribe silver, gold y meta
POSTGRES_RO_USER=api_ro       # SELECT en gold y meta, nunca en silver
```

El driver es **psycopg3** (`postgresql+psycopg`), no psycopg2:
`shared/db/copy.py` necesita `COPY ... FROM STDIN` y psycopg2 está en modo
mantenimiento. La URL la arma `shared/config.py:DATABASE_URL`; no hay
`SILVER_DATABASE_URL` ni `GOLD_DATABASE_URL` separadas porque es una sola
base.

### Migraciones: un baseline y lo que cambió después

**Estándar del proyecto: la base se crea desde cero.** `b0000` es el
baseline de PostgreSQL —reemplazó la cadena histórica de DuckDB— y las seis
que siguen son los cambios reales de esquema desde entonces:

| Revisión | Qué hace |
|---|---|
| `b0000baseline` | Los tres schemas, las 11 tablas de `silver`, las 5 de `meta` con FKs a `job_runs`, el rol de solo lectura, y **siembra los catálogos desde los parquet** |
| `b0001drop_normalize_tasks` | Tira `meta.normalize_tasks`: la normalización de fechas dejó de ser una etapa |
| `b0002clee_por_metodo` | Crea `silver.clee_radio` y `silver.clee_h3` (particionadas, sin particiones) |
| `b0003saca_clee_del_hecho` | Quita `clee_propagado_radio/_h3` de `establecimientos`: la identidad vive en la tabla del método |
| `b0004texto_libre_sin_tope` | Quita el tope de largo al texto libre de INEGI (el primer ingest real falló por eso) |
| `b0005permanencia_por_metodo` | Cambia `entidades_economicas` por `permanencia_radio` / `permanencia_h3` |
| `b0006sin_vistas_de_permanencia` | Elimina las cuatro vistas `estatus_<metodo>` / `variantes_<metodo>`: eran consultas, no esquema |

El DDL se genera desde el metadata del ORM (`src.shared.db.models`), no con
`op.create_table` a mano, así que **la migración no puede desincronizarse
del modelo**.

Verificar antes de una corrida larga:

```bash
docker compose run --rm tools alembic current --verbose
```

### Seed Loading

Los seeds se cargan **dentro de la migración** (servicio `migrate`, que
`docker compose up -d` encadena), y ese es el único lugar donde ocurre: no
hay un comando de siembra aparte. Orden por jerarquía de
FKs: `entidades` → `municipios` → `localidades` → `actividades_economicas`
→ catálogos de dominio → estratos.

```bash
docker compose up -d migrate          # crea el esquema Y siembra los catálogos
docker compose run --rm tools alembic current --verbose           # revisión: migración aplicada + salud de índices
```

Los loaders **no crean tablas ni toleran su ausencia**: si la tabla no
existe es que no se corrió la migración, y eso debe fallar, no repararse a
escondidas.

### API Endpoints

```
GET /health                            # Sonda basica del proceso
GET /api/v1/health/db                  # Conectividad con PostgreSQL
GET /api/v1/info                       # Version y configuracion publica
GET /api/v1/ops/runs                   # Ultimas corridas de meta.job_runs
GET /api/v1/ops/runs/{run_id}          # Una corrida con sus conteos reales
GET /api/v1/ops/runs/{run_id}/tasks    # Tareas de esa corrida
GET /api/v1/ops/health/pipeline        # Estado de la ultima corrida POR ETAPA
GET /api/v1/docs                       # Swagger UI
GET /api/v1/redoc                      # ReDoc
```

El API usa el rol `api_ro`: `SELECT` en `gold` y `meta`, **nunca** en
`silver`. `/ops/health/pipeline` responde 200 siempre —el endpoint está sano
aunque el pipeline no— y el veredicto va en el cuerpo; mezclar las dos cosas
haría que un ETL fallido se viera igual que un API caído.

---

## Environment Variables

`.env.example` es la referencia completa y comentada; esto es el resumen.
No hay `DATABASE_BACKEND` ni variables de DuckDB: el motor es PostgreSQL y
nada más, y ningún módulo de `src/` lee una `DUCKDB_*`.

```bash
# PostgreSQL: una sola base, tres schemas (silver, gold, meta).
# Driver psycopg3 (`postgresql+psycopg`), no psycopg2: `shared/db/copy.py`
# necesita COPY ... FROM STDIN y psycopg2 esta en modo mantenimiento.
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=mipymes
POSTGRES_USER=etl_rw
POSTGRES_PASSWORD=your_password
POSTGRES_RO_USER=api_ro
POSTGRES_RO_PASSWORD=another_password

# Filas por bloque de COPY en las escrituras masivas: es la cota de RAM del
# ingest (RAM_pico ~ filas x bytes_por_fila). Bajarlo si un ingest se queda
# sin memoria.
COPY_CHUNK_ROWS=500000

# SQLAlchemy
SQLALCHEMY_ECHO=false
SQLALCHEMY_POOL_SIZE=5
SQLALCHEMY_MAX_OVERFLOW=10
SQLALCHEMY_POOL_PRE_PING=true

# Postgres de pruebas para los tests marcados `postgres`. Vacia = se deriva
# de POSTGRES_* con sufijo `_test`. Esos tests se saltan solos si no hay
# servidor, asi que dejarla vacia es valido.
TEST_POSTGRES_URL=

# Application
LOG_LEVEL=INFO
API_TITLE=DENUE ETL API
API_VERSION=0.1.0
API_PREFIX=/api/v1

# Descargas: DOWNLOAD_TIMEOUT_SECONDS es el read timeout de requests y su
# reloj se reinicia con cada chunk, asi que no acota la duracion total. El
# tope real lo pone DOWNLOAD_MAX_DOWNLOAD_SECONDS (levanta Timeout, que es
# retryable). Las descargas son secuenciales por decision.
DOWNLOAD_MAX_DOWNLOAD_SECONDS=900

# DENUE scraping: la espera a la tabla de INEGI es por CONTENIDO (filas con
# enlace .zip), no por presencia del elemento. Si no junta MIN_LINKS dentro
# del timeout se levanta ScrapeIncompleteError y el run queda FAILED en
# meta.job_runs.
DENUE_SCRAPE_TIMEOUT_SECONDS=120
DENUE_SCRAPE_MIN_LINKS=100

# SESNSP (crime incidence, ver seccion SESNSP arriba)
SESNSP_CKAN_PACKAGE_URL=https://www.datos.gob.mx/api/3/action/package_show?id=incidencia_delictiva
SESNSP_RESOURCE_NAME=Incidencia delictiva municipal
# SESNSP_DOWNLOAD_URL=            # override manual, salta el descubrimiento CKAN

# Permanencia: en cuantos bloques de identidad trocear la fase de agregados,
# y cada cuanto emitir el latido de progreso (0 lo apaga).
PERMANENCIA_BLOQUES=32
PERMANENCIA_LATIDO_SEGUNDOS=60
```

---

## Metadata Tracking (`src/meta`)

### MetadataTracker Usage

Cada método abre y cierra su propia transacción, así que lo que se registra
sobrevive aunque la etapa que lo invocó falle después — es deliberado: el
historial de una corrida fallida es justo lo que se quiere conservar.

```python
from src.meta.tracker import MetadataTracker
from src.shared.enums import JobStatus, TaskStatus

tracker = MetadataTracker()          # session_factory del proyecto; se inyecta en tests

# Ciclo de vida de la corrida
run_id = tracker.create_run("denue_downloader", job_type="DOWNLOAD", config={...})
tracker.complete_run(run_id, JobStatus.SUCCESS)

# Descargas
task_id = tracker.create_task(run_id, "download_comercio_2024_11", url, path)
tracker.start_task(task_id)
tracker.complete_task(task_id, TaskStatus.SUCCESS, result)

# Proceso (un ZIP/CSV, o una tabla derivada entera)
task_id = tracker.create_process_task(run_id, task_name, source_path, period, target_table)
tracker.start_process_task(task_id)
tracker.complete_process_task(task_id, TaskStatus.SUCCESS, rows_loaded=1000)

# Resolución CLEE
task_id = tracker.create_clee_resolution_task(run_id, task_name, region, states)
tracker.complete_clee_resolution_task(task_id, TaskStatus.SUCCESS, entities_matched=8000)

# Consultas
summary = tracker.get_run_summary(run_id)
metrics = tracker.get_process_metrics()
ultima = tracker.get_last_successful_process_task(task_name, target_table)  # reloj de frescura
ya_esta = tracker.has_successful_process_task(task_name, target_table)      # incremental
```

### Monitoring Queries

```sql
-- Descargas fallidas con reintento disponible
SELECT task_name, error_type, attempt_number, max_retries
FROM meta.download_tasks
WHERE status = 'FAILED' AND is_retryable = TRUE AND attempt_number < max_retries;

-- Rendimiento por corte
SELECT period, COUNT(*), AVG(file_size_bytes)/1024/1024 AS avg_mb,
       AVG(download_speed_mbps), AVG(duration_seconds)
FROM meta.download_tasks WHERE status = 'SUCCESS' GROUP BY period;

-- Tasa de exito por dia
SELECT run_date::date AS dia, COUNT(*),
  ROUND(100.0 * SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END) / COUNT(*), 2) AS pct
FROM meta.job_runs GROUP BY 1 ORDER BY 1 DESC;

-- Como termino la ultima corrida de cada etapa
SELECT job_type, job_name, status, run_date,
       round(duration_seconds / 60) AS minutos
FROM meta.job_runs r
WHERE run_date = (SELECT max(run_date) FROM meta.job_runs WHERE job_type = r.job_type)
ORDER BY run_date DESC;
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

Declarados en `pytest.ini` (con `--strict-markers`: un marcador no
declarado es error, no un typo silencioso).

```python
@pytest.mark.unit          # Rapido, aislado, fixtures manuales
@pytest.mark.integration   # Usa base de datos real
@pytest.mark.db            # Relacionado con la base
@pytest.mark.slow          # Consultas sobre decenas de millones de filas
@pytest.mark.catalog       # Tablas de catalogo
@pytest.mark.quality       # Validacion de calidad de datos
@pytest.mark.postgres      # Exige el servidor de pruebas; se SALTA solo si no esta
@pytest.mark.tables(*n)    # Tablas que el fixture `connection` debe crear
```

### Fixtures (conftest.py)

**Objetos sueltos** (sin base): `sample_establecimiento()`,
`sample_establecimiento_minimal()`, `sample_establecimiento_invalid_coords()`,
`sample_entidad()`, `sample_municipio()`, `sample_localidad()`,
`sample_actividad_economica()`.

**Contra PostgreSQL** (marcar el test `postgres`):

| Fixture | Qué da |
|---|---|
| `pg_engine` | Engine al Postgres de pruebas, con los tres schemas creados. Scope de sesión |
| `silver_engine` | `DROP SCHEMA silver CASCADE` + `create_all` de las tablas de la capa: cada test parte de un `silver` limpio |
| `tracker` | `MetadataTracker` sobre un `meta` recién recreado y vacío |

La base de pruebas sale de `TEST_POSTGRES_URL`; si no está definida se
deriva de `POSTGRES_*` con sufijo `_test` (nunca la de producción), y si el
servidor responde pero la base falta, `conftest.py` la crea. Los tests
`postgres` se saltan solos cuando no hay servidor, así que la suite corre
completa en una máquina sin nada levantado.

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
from src.etl.core.config import settings
from src.etl.transformers.denue import DenuePipelineOrchestrator

# Local - Metadata
from src.meta.tracker import MetadataTracker
from src.meta.models import JobStatus
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

Verify cycle (contra una base desechable, nunca la real):
```bash
docker compose run --rm tools alembic upgrade head     # aplica las 7 revisiones
docker compose run --rm tools alembic downgrade -1     # revierte la ultima
docker compose run --rm tools alembic upgrade head     # la vuelve a aplicar
```

---

## Git Rules

**Committed:** `data/seeds/*.parquet`, `uv.lock`, `*.py`, `*.md`, config files
**Ignored:** `data/bronze/*`, `data/kb_cache/*`, `data/eval/*`, `data/etl_*.log`, `.env`, `logs/`, `.ruff_cache/`, `htmlcov/`, `coverage.xml`

---

## Docker

Un solo `Dockerfile.etl` con dos targets (`tools` y `etl`); compose los
construye por nombre de servicio, no hace falta `docker build` a mano.

```bash
docker compose build etl                       # solo la imagen del pipeline
docker compose build tools                     # solo la de tests/migraciones
docker compose --profile manual --profile tools build   # ambas

docker compose run --rm etl extract --force    # cuidado: re-scrapea TODO
docker compose run --rm etl pipeline
```

**Tras cambiar código en `src/`, hay que reconstruir `etl`**: esa imagen
hornea el código, a diferencia de `tools`/`migrate`, que lo montan como
volumen. Sin el build se corre una versión vieja sin ningún aviso.

---

## Quick Reference

```bash
# Arranque en limpio (el runbook completo es ARRANQUE.md)
docker compose --profile manual --profile tools build
docker compose up -d                                    # Postgres -> migraciones -> API

# Dia a dia
docker compose run --rm tools pytest -q
docker compose run --rm tools ruff check src/ tests/ scripts/ alembic/

# Correr el ETL
docker compose run --rm etl extract                     # 1: Bronze desde INEGI + SESNSP
docker compose run --rm etl transform                   # 2: Bronze -> Silver (todas las secciones)
# o las dos de un tiron:
docker compose run --rm etl pipeline --extract

# Ver como va
docker compose run --rm tools alembic current --verbose
curl -s localhost:8000/api/v1/ops/health/pipeline | python3 -m json.tool
docker compose exec postgres psql -U etl_rw -d mipymes -c \
  "SELECT job_type, job_name, status, run_date FROM meta.job_runs ORDER BY run_date DESC LIMIT 10"
```

> Inventario COMPLETO de comandos, flags y combinaciones:
> "Comandos — inventario completo" + "Comando único global".
