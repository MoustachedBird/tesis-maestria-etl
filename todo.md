# TODO — Optimización de la resolución CLEE (hot path + blocking H3)

Plan derivado de la revisión de eficiencia de la propagación CLEE (2026-07-16).
El cuello de botella dominante es el loop de matching en Python (gate de
actividad fuzzy recalculado por par + round-trip `to_dicts()`), no el BallTree.
Se atacan primero los quick wins sin cambio arquitectónico y después se migra
el pre-filtro espacial a blocking con celdas H3.

**REGLA DE ORO: no correr migraciones ni `ALTER TABLE` sobre los `.duckdb` de
`data/` sin confirmación explícita.** Los cambios de esquema se preparan en
código (migración Alembic + modelo ORM) y se aplican en un paso controlado.

**REGLA DE CONFIGURACIÓN (permanente, no repetir el error): toda variable
importante de configuración o calibración se lee del entorno (`.env`) con
default en código, y se documenta en `.env.example` Y en la documentación
(AGENTS.md). Las constantes que NO deben ser configurables (físicas como el
radio de la Tierra, o estructurales acopladas al esquema/datos materializados
como las resoluciones H3) se quedan en su archivo con un docstring que
explique su significado y POR QUÉ no son `.env`. Además: toda variable global
y toda función deben tener docstring.**

**PREMISA DE OPTIMIZACIÓN: toda mejora debe optimizar tiempo de ejecución Y
uso de RAM a la vez, aprovechando correctamente la CPU con paralelización** —
el pipeline corre en una Mac M4 Pro/Max (límite DuckDB 8GB en .env), no en
un servidor. Nada de caches sin cota, KBs monolíticas en RAM ni matrices
gigantes precomputadas; preferir procesamiento columnar/streaming, caches
acotados o en disco, y liberar memoria entre pasadas. Para CPU: preferir el
paralelismo interno de los motores (DuckDB threads, Polars, rapidfuzz
workers) antes que multiprocessing manual, y cuando se use multiprocessing,
fork+COW sin copiar datos grandes. Además, los datos DENUE provienen de
encuestas: cualquier campo puede venir vacío o faltar en un periodo
completo — todo código de matching debe tolerarlo (`fill_null("")`,
columnas ausentes → `""`).

---

## Fase 1 — Quick wins en el hot path (sin cambio de arquitectura) ✅ 2026-07-17

- [x] **Memoizar el gate de actividad SCIAN**: `passes_activity_gate()` en
      `matching.py` con `functools.cache` (clave simétrica normalizada) +
      fast-path de igualdad exacta. RAM acotada: universo ≤ ~635K pares del
      catálogo, decenas de MB en la práctica.
- [x] **Eliminar round-trip `to_dicts()` / `pl.DataFrame(dicts)`**: el loop
      ahora opera sobre listas columnares (`TARGET_MATCH_COLUMNS`, 7 cols en
      vez de ~20 como dicts); resultados se aplican por índice con
      `with_columns(pl.Series)`. `calculate_match_score` pasó a firma escalar.
      Mayor ahorro de RAM de la fase (GBs de dicts eliminados).
- [x] **Ordenar vecinos por distancia**: `sort_results=True` en
      `query_radius`; el primer COMBINED (más cercano) se conserva y un
      match directo lo reemplaza.
- [x] **Cache de KBs preparadas** (`data/silver/kb_cache/{period}.parquet`):
      `_get_ref_kb()` con escritura atómica (tmp+rename); invalidación al
      re-escribir un periodo y limpieza total con `--force`.
- [x] Robustez datos-de-encuesta: columnas de matching ausentes en un
      periodo se sustituyen por `""` (KB garantiza `KB_COLUMNS` completas).
- [ ] (Opcional) Vectorizar el scoring con `rapidfuzz.process.cpdist` sobre
      pares aplanados — probablemente vuelve innecesario el Pool.
- [ ] (Opcional) Evaluar gate por prefijo de `codigo_act` (ya materializado
      en el catálogo como sector/subsector/rama) en vez de fuzzy sobre
      nombres — cambia calibración, requiere validación de paridad.
- [x] Benchmark sintético (40K target × 200K KB, secuencial): **16.87s →
      6.65s (2.5x)** con matches idénticos (24,001). Paralelo (80K, 6
      workers): 4.02s, ~3.3x throughput vs secuencial. Falta benchmark
      sobre un periodo real (correr con `make clee-resolution`).

## Fase 2 — Esquema H3 (migración Alembic + ORM)

- [x] Migración Alembic `74b0a3dad572_add_h3_cell_columns_to_establecimientos`:
      añade `h3_cell_9` (BIGINT, blocking res 9 ≈ arista 174m) y `h3_cell_5`
      (BIGINT, particionado grueso res 5 ≈ arista 8.5km) + índice
      `ix_estab_h3_9`. Idempotente, mismo patrón que `ae36cad63494`.
- [x] Modelo ORM `Establecimiento`: columnas `h3_cell_9` / `h3_cell_5` +
      índice en `__table_args__` (necesario para el path DuckDB `create_all`).
- [x] Dependencia `h3` agregada (`uv add h3`, v4.5.0, API int:
      `h3.api.basic_int`). Decisión: el cómputo masivo usa la extensión
      comunitaria `h3` de DuckDB (out-of-core, cero RAM en Python);
      `polars-h3` descartado por ahora — verificado que h3-py y la
      extensión DuckDB producen celdas idénticas.
- [x] Aplicar esquema al Silver real: `make db-migrate` ejecutado por el
      usuario (2026-07-16), columnas H3 + índice creados. Gold hereda el
      esquema en el próximo `make load` (copia de archivo).

## Fase 3 — Cómputo y backfill de celdas H3

- [x] Nuevo stage `h3-cells` (`transformers/h3_backfill.py`,
      `H3CellBackfiller`): UPDATE por periodo vía extensión h3 de DuckDB,
      100% out-of-core, respeta DUCKDB_MEMORY_LIMIT/THREADS. Incremental
      por diseño (`h3_cell_9 IS NULL` + coords válidas), así que cubre
      TAMBIÉN los periodos nuevos del ingest — no fue necesario tocar
      `processor.py`. Tracking en metadata (job `denue_h3_backfill` +
      process_task por periodo). Integrado al pipeline
      (ingest → normalize-dates → h3-cells → clee-resolution), flag CLI
      `--h3-cells/--no-h3-cells`, target `make h3-backfill`.
- [x] Smoke test en DuckDB scratch: coords válidas pobladas, nulas/fuera
      de rango saltadas, filas ya pobladas intactas, celdas idénticas a
      h3-py, re-run idempotente (0 pendientes).
- [x] Backfill del Silver real completado por el usuario (2026-07-17):
      76,758,727/76,758,727 filas, 19 periodos, 100% de coords válidas.
- [x] Verificación post-backfill: celdas no nulas == filas con coords
      válidas (76.7M); spot-check 2,000 filas vs h3-py: res-9 idéntica.
      **Hallazgo**: la res-5 directa del punto difería del padre de la
      res-9 en 6.52% de filas (la jerarquía H3 no anida perfecto).
      Corregido: `h3_cell_5 := parent(h3_cell_9)` (5,002,823 filas en 1s —
      sin índice sobre h3_cell_5 no hay mantenimiento ART; confirma que
      la lentitud del backfill era 100% el índice). SQL del backfill
      actualizado para usar `h3_cell_to_parent` en futuras corridas.
- [x] **Columnas H3 computadas en el INSERT del ingest**
      (`EstablecimientoWriter.upsert`): expresiones h3 vectorizadas por
      DuckDB dentro del INSERT SELECT (guard de coords válidas, NULL si
      faltan), DDL del writer actualizado. Los periodos nuevos nacen con
      celdas; el stage h3-cells queda como red de seguridad incremental.
      `fecha_alta_date` se queda en normalize-dates a propósito: portar
      el parser de 6 formatos a Polars duplicaría la lógica en dos
      motores (va contra la centralización).

## Fase 4 — Resolución H3 en paralelo al baseline (diseño de 2 columnas)

Decisión 2026-07-17: en vez de REEMPLAZAR el BallTree, ambos métodos
coexisten en columnas separadas para comparación lado a lado (material
del capítulo metodológico de la tesis + rollout seguro):

Nombres autodescriptivos (feedback usuario 2026-07-17: "clee_master no
me dice nada"):

| Columna | Método | Estado |
|---------|--------|--------|
| `clee` | Original INEGI (fuente, intacta) | existe |
| `clee_propagado_radio` | Radio de tolerancia / BallTree (baseline) | rename de `clee_master`, migración lista |
| `clee_propagado_h3` | Blocking por celdas H3 | migración lista |

- [x] Migración `7427c9bfdb0d`: RENAME `clee_master` → `clee_propagado_radio`
      (metadata-only, preserva los 77M valores) + columna `clee_propagado_h3`
      (VARCHAR, idempotente). **Sin índice en la nueva a propósito**: el ART
      sobre 77M filas hace las escrituras masivas ~1000x más lentas (medido);
      indexar DESPUÉS de resolver si el análisis lo pide. Rename aplicado en
      TODO el código (resolver, entity_resolution, config, modelos, writer,
      AGENTS.md). Limitación DuckDB manejada: ALTER exige soltar/recrear los
      13 índices de la tabla (definiciones impresas antes como respaldo;
      fallback manual documentado en la migración).
- [x] Aplicado al Silver real por el usuario (2026-07-17): columnas
      renombradas/añadidas. **NOTA post-migración**: la recreación de índices
      no completó — el Silver quedó solo con `ix_estab_h3_9` (los ~12 de
      queries analíticas se perdieron; no afectan la resolución, solo queries
      ad-hoc). Recrearlos es un paso aparte de decenas de minutos si el
      análisis los necesita (ver "Índices pendientes" abajo).
- [x] Resolver parametrizado por método (`CLEEResolver(method="radio"|"h3")`,
      dict `METHODS`): columna destino + `propagate_*` + sufijo de prefijo.
      Fase 1 determinística escribe `{columna} = clee`; force resetea SOLO
      la columna del método (no pisa la del otro).
- [x] Diseño "una función por columna, sin duplicar": core privado
      `_propagate_clees(df, kb, tol, master_column, neighbor_finder)` +
      wrappers `propagate_clees_radio` / `propagate_clees_h3`. KB identidad
      neutralizada a `clee_id` (agnóstica de método).
- [x] `find_h3_neighbors` (matching.py): candidatos por `grid_disk(h3_cell_9,
      k=3)` como joins de Polars (multihilo, streaming) + post-filtro
      haversine exacto ≤ SPATIAL_RADIUS_M en el motor de expresiones.
      **Verificado: produce EXACTAMENTE los mismos vecinos que el BallTree**
      (0 diferencias en 1500 filas sintéticas). Misma cascada reutilizada.
- [x] Config `H3_GRID_DISK_K = 3`; `h3_cell_9`/`h3_cell_5` + ambas columnas
      clee añadidas a SILVER_READ_QUERY y COLS_NEEDED_FOR_MATCHING.
- [x] KB cache separado por método (`kb_cache/{radio|h3}/`); KB_COLUMNS
      incluye `h3_cell_9`.
- [x] Orquestador: `run_clee_resolution_h3_stage()`; CLI
      `--clee-resolution-h3` (default OFF); `make clee-resolution-h3`.
      NEW_* de H3 llevan sufijo de prefijo (no colisionan con radio).
- [x] **Paridad verificada end-to-end** (scratch, esquema real, 2 periodos):
      ambos métodos poblaron 800/800 filas con asignaciones IDÉNTICAS
      (800/800 propagados coinciden radio==h3). 157 tests en verde.
- [ ] Correr H3 sobre el Silver real: `make clee-resolution-h3` (radio ya
      está poblado). Luego medir paridad real y benchmark tiempo/RAM H3 vs
      BallTree.
- [x] **Hallazgo del review (2026-07-17): RAM de `disk_pairs` en
      `find_h3_neighbors`** — los pares (celda_target, celda_vecina) se
      acumulaban como lista Python de tuplas (~37M tuplas ≈ varios GB con
      ~1M de celdas distintas). Corregido: 2 arrays numpy preasignados
      (16 bytes/par). Medido con 990K celdas distintas (peor caso de un
      periodo): 36.6M pares en 4.1s, 0.59GB — dentro del presupuesto.
      Tests de paridad H3 vs BallTree añadidos
      (`tests/unit/test_h3_candidates.py`, cubre el pendiente de Fase 6);
      de paso `collect(streaming=True)` → `collect(engine="streaming")`
      (deprecado en Polars 1.25).
- [ ] (Opcional) Particionado de KB por `h3_cell_5` en vez de slices para
      podar particiones sin solape — optimización, no correctitud (el slice
      actual ya es correcto para ambos métodos).

## Índices pendientes de recrear en el Silver real (2026-07-17)

La migración de rename recreó índices en scratch pero en el Silver real solo
sobrevivió `ix_estab_h3_9`. Los definidos en `models.py`/migración base
(ix_estab_geo, ix_estab_clee, ix_estab_coords, ix_estab_postal, etc.) NO
están. No afectan la resolución CLEE (que usa ix_estab_h3_9 y scans), pero sí
las queries analíticas de la tesis. Recrearlos con:
`CREATE INDEX ix_estab_geo ON establecimientos(cve_ent,cve_mun,cve_loc);` etc.
Costo: decenas de min (ART sobre 77M). Hacerlo cuando el análisis los pida.

## Fase 5 — (Opcional, habilitado por H3) KB consolidada

- [ ] Evaluar KB nacional deduplicada por `clee_propagado_radio` (atributos del periodo
      más reciente): con candidatos generados vía join en DuckDB (out-of-core),
      la RAM deja de ser la restricción y el loop T×R colapsa a una pasada por
      target.

## Fase 6 — Validación y documentación

- [ ] Paridad de matches: correr un periodo muestra con BallTree vs H3 y
      comparar asignaciones de `clee_propagado_radio` (deben ser ≈ idénticas; diferencias
      solo por el post-filtro exacto vs radio BallTree).
- [ ] `uv run pytest` en verde + tests nuevos para cómputo de celdas y
      generación de candidatos.
- [ ] Benchmark final del pipeline completo de resolución (tiempo total,
      pico de RAM) vs baseline.
- [ ] Actualizar `AGENTS.md` (sección CLEE Resolution: blocking H3, nuevas
      constantes, columnas nuevas) y este `todo.md`.

---

# Auditoría de paralelización por etapa (2026-07-17)

Estado del aprovechamiento de CPU en cada etapa del pipeline, y los casos
donde la paralelización NO es posible (con la razón).

## Estado actual por etapa

| Etapa | Paralelismo hoy | Estado |
|-------|-----------------|--------|
| Extract (descargas) | 3 descargas concurrentes (`max_parallel_downloads`) | ✅ OK — network-bound, más concurrencia no ayuda |
| Extract (scraping Selenium) | Secuencial | ⛔ No paralelizable (ver abajo) |
| Ingest (ZIP→Silver) | Polars `read_csv` multihilo interno; loop de archivos secuencial | ✅ Aceptable — el parseo (lo caro) usa todos los cores; el INSERT es single-writer |
| normalize-dates | UPDATE SQL → paralelismo interno de DuckDB | ✅ OK — DuckDB paraleliza el UPDATE internamente |
| h3-cells | UPDATE SQL → paralelismo interno de DuckDB (threads de settings) | ✅ OK |
| CLEE matching (cascada) | `multiprocessing.Pool` fork+COW, ≥50K filas, `MATCH_WORKERS` | ✅ OK |
| CLEE BallTree (build+query) | **Single-thread** (sklearn no expone n_jobs en `query_radius`) | ⚠️ Hot spot no paralelo — se resuelve en Fase 4 (H3-join en DuckDB, paralelo por diseño); no invertir en paralelizar BallTree |
| CLEE escritura resultados | Batches secuenciales de 50K vía temp table | ✅ Aceptable — single-writer |
| Load (Silver→Gold) | Copia de archivo única | ⛔ No paralelizable (ver abajo) |

## Casos donde la paralelización NO es posible (razones)

- **DuckDB es single-writer por archivo**: no se pueden procesar periodos
  en procesos paralelos para UPDATEs (normalize-dates, h3-cells, escritura
  CLEE). Compensación: cada UPDATE individual sí usa todos los threads de
  DuckDB internamente.
- **Ventana deslizante CLEE**: los periodos target se resuelven
  secuencialmente por DEPENDENCIA DE DATOS inherente — cada periodo
  resuelto se convierte en referencia del siguiente. Solo el matching
  interno de cada pasada es paralelizable (ya lo está).
- **Load Silver→Gold**: copia de un solo archivo de ~22GB — I/O-bound del
  disco, paralelizar no aporta.
- **Scraping Selenium**: una sola sesión de navegador contra el sitio de
  INEGI (estado de sesión + cortesía con el servidor); las descargas
  resultantes sí van en paralelo.

## Acciones pendientes detectadas por la auditoría

- [x] **BUG de config corregido (2026-07-17): `DUCKDB_THREADS` y
      `DUCKDB_MEMORY_LIMIT` ahora se aplican** vía
      `utils/warehouse.py:connect_configured()` en `clee_resolver`,
      `date_normalizer`, `h3_backfill` y `DuckDBWarehouse`. Regla: toda
      conexión nueva a Silver/Gold debe usar ese helper, nunca
      `duckdb.connect()` directo.
- [ ] Aplicar `connect_configured()` también en `processor.py` /
      `loaders/denue.py` (escritura del ingest) — pendiente de revisar
      cómo gestionan sus conexiones.
- [ ] **Balanceo de chunks en matching paralelo**: los chunks son slices
      contiguos (1 por worker); zonas densas (CDMX) vs rurales generan
      workers desbalanceados. Usar ~4× chunks que workers con
      `imap_unordered` (ya se usa) para que los rápidos tomen más trabajo.
- [ ] `assign_new_clees()` usa `map_elements` (lambda Python por fila,
      single-thread): generar los UUIDs en bulk (lista Python + Series).
      Impacto menor (solo filas sin match).
- [ ] (Ya anotado en Fase 1) Vectorizar scoring con `rapidfuzz.process.cpdist
      (workers=-1)` — paralelismo C multihilo sin overhead de fork/pickle.
      Eliminaría de raíz el residuo de COW del fork (ver abajo).

## Medición empírica de escalado (2026-07-17, M4 Max 14 cores / 36GB)

Benchmark sintético 160K target × 200K KB (matches idénticos 96,004 en
los tres casos; medido con 1 core ocupado por el backfill H3):

| MATCH_WORKERS | Tiempo | Speedup | RSS máx proceso padre |
|---------------|--------|---------|----------------------|
| 1 (secuencial)| 28.50s | 1.0x    | 1.38GB |
| 6             | 8.26s  | 3.5x    | 1.74GB |
| 11            | 6.17s  | 4.6x    | 1.78GB |

Análisis (Amdahl): fracción serial ≈ 4.2s (~15%) = BallTree build/query +
preparación columnar + ensamblado — techo teórico ~7x con workers
infinitos. Ese 15% serial lo elimina la Fase 4 (H3-join en DuckDB,
paralelo por diseño). Acciones tomadas/conclusiones:

- [x] `MATCH_WORKERS` subido de 6 → 10 en `.env` (los 10 P-cores; los
      4 E-cores aportan poco y el default auto cpu-3=11 mostró retornos
      decrecientes). RAM peor caso con 10 workers: 10×0.5GB COW residual
      + padre + DuckDB 8GB ≈ 16-17GB « 36GB. Seguro.
- [ ] **Hallazgo backfill H3: el UPDATE de DuckDB corre con sink
      single-thread** (~2,000 filas/s observadas, 1 core) — el costo real
      es la reescritura de filas + mantenimiento del índice ART
      `ix_estab_h3_9` fila a fila, no el cómputo h3. Para futuros re-runs
      con `--force`: DROP INDEX ix_estab_h3_9 → backfill → CREATE INDEX
      (orden de magnitud más rápido). No aplica al flujo incremental
      normal (periodos nuevos son ~1-4M filas).
- Memoria del sistema durante backfill real: 90% libre, RSS del proceso
  9.6GB (el memory_limit de DuckDB gobierna buffers, no el RSS total del
  proceso — extensiones y Python van aparte; margen amplio en 36GB).

## Verificación de seguridad RAM del paralelismo (2026-07-17)

**fork + copy-on-write en CPython es imperfecto**: los refcounts y los
flags del GC escriben en las cabeceras de los objetos Python, así que
cuando un worker itera las listas `_shared_kb_*`, las páginas tocadas se
copian de facto. Peor caso teórico: cada worker materializa la partición
de KB que toca (~0.5GB con `KB_MAX_ROWS_PER_PARTITION=1000000`).

Mitigaciones aplicadas:
- `gc.freeze()` antes del fork / `gc.unfreeze()` después
  (`entity_resolution.py:_run_matching_parallel`): elimina las escrituras
  del GC en los hijos (las de refcount son inherentes a CPython).
- `KB_MAX_ROWS_PER_PARTITION` acota el tamaño de lo compartido — es el
  knob principal de RAM del matching (documentado en .env.example).
- Presupuesto peor caso con config actual (6 workers × ~0.5GB partición +
  padre + DuckDB 8GB): ~12-14GB — seguro en la M4 Pro/Max.
- Solución definitiva si se quiere eliminar el residuo: cpdist (sin fork).

**Chunks del target**: se picklean slices columnares (7 listas), costo
temporal ~2× el target en el padre durante el fan-out — acotado y muy
inferior al costo previo de los dicts.

## Variables de entorno normalizadas (2026-07-17)

- [x] `extractors/denue.py` hardcodeaba `max_retries=3,
      max_parallel_downloads=3, timeout_seconds=300` al construir
      `DownloadConfig`, silenciando las env vars `DOWNLOAD_*`. Corregido:
      solo fija `download_directory`; el resto viene del entorno.
- [x] `.env`: `DOWNLOAD_MAX_PARALLEL_DOWNLOADS=3`, `DOWNLOAD_MAX_RETRIES=3`,
      `DOWNLOAD_TIMEOUT_SECONDS=300` explícitas (mismos valores efectivos
      que el hardcode — sin cambio de comportamiento).
- [x] `.env.example`: todas las variables de recursos documentadas con
      ejemplos simples (qué hace cada una, regla práctica de MATCH_WORKERS,
      knob de RAM de KB_MAX_ROWS_PER_PARTITION, etc.).
- [x] `.env.test`: recursos mínimos y deterministas para la suite
      (MATCH_WORKERS=1 secuencial, DUCKDB_THREADS=2, MEMORY_LIMIT=2GB,
      descargas sin paralelismo).

---

# Auditoría de reutilización / duplicación / código muerto (2026-07-17)

Directriz: lo transversal (logging, conexiones, patrones de stage) debe
vivir en UN lugar. Hallazgos ordenados por valor.

## Duplicado transversal — centralizar

- [x] **Logging centralizado** (2026-07-17): helper único
      `get_stage_logger(name, log_file)` en `core/logging.py` (handlers
      dedicados + propagate=False). Eliminados los `logging.basicConfig()`
      a nivel de módulo de `date_normalizer` y `processor` (configuraban
      el root logger de todo el proceso al importar); `h3_backfill`
      migrado al helper. Regla: stages nuevos usan `get_stage_logger`.
- [x] **Query de periodos unificada**: `get_distinct_periods(conn,
      descending)` en `utils/warehouse.py`, usada por `clee_resolver` y
      `date_normalizer`. (La de `h3_backfill` es un agregado por periodo
      con filtros propios — query distinta, se queda.)
- [x] **Sanitización consolidada**: `clean_contact_columns()` es el punto
      único (rellena `TEXT_COLS_TO_SANITIZE` presentes + deriva
      tel/email/www `_clean`); `_load_period()` ya no duplica el fill.
      El relleno de `build_single_period_kb()` se conserva como defensa
      del path de KB cacheada (parquet externo).
- [x] **`load_h3_extension(conn)`** en `utils/warehouse.py`: patrón
      LOAD/INSTALL en un solo lugar (usado por h3_backfill y el writer).
- [ ] (Opcional, valor medio) Boilerplate de stage repetido: create_run →
      task por periodo → complete_run en `date_normalizer`, `h3_backfill`
      y `clee_resolver`. Un `StageRunner` lo unificaría; evaluar si vale
      el churn.
- [x] Conexiones DuckDB: YA centralizadas en `connect_configured()`
      (2026-07-17). `EstablecimientoWriter` hereda de `DuckDBWarehouse`,
      así que el ingest también quedó cubierto.
- Paralelización: SIN duplicación — vive solo en `entity_resolution.py`
  (`_get_num_workers` es la única fuente de workers). Nada que hacer.

## Código muerto / borrable

- [x] `db/database.py`: alias muerto `DuckDBWarehouse = get_db` eliminado
      (2026-07-17) — 0 usos y colisionaba de nombre con
      `utils.warehouse.DuckDBWarehouse`.
- [ ] `AGENTS.md` referencia `src_etl/transformers/_legacy/` que YA NO
      existe. Actualizar layout (junto con lo demás pendiente de Fase 6).
- [ ] (Opcional) Helpers de `Establecimiento` en `models.py`: 10 métodos
      con 0 usos fuera de models/tests. Varios ahora duplican al esquema:
      `get_scian_sector/subsector` ≈ columnas `sector`/`subsector`;
      `get_employee_range/get_business_size` ≈ `denue_estratos_personal`.
      Podar los redundantes si se quiere reducir superficie (los tests
      que los cubren se ajustan).
- Artefactos locales regenerables (no trackeados, borrar cuando estorben):
  `htmlcov/`, `coverage.xml`.
- `data/metadata.duckdb.backup_pre_rename` (~2GB): backup manual previo al
  rename — SOLO el usuario decide borrarlo (política de datos).

## Reutilizable existente (para Fase 4, no reinventar)

- `connect_configured()` para toda conexión nueva.
- Patrón kb_cache parquet (`_get_ref_kb`) para cualquier cache de derivados.
- `H3_BLOCKING_RES`/`H3_PARTITION_RES` + columnas ya pobladas.
- Columna `sector` del catálogo SCIAN para el gate por prefijo (opcional
  Fase 1) sin recomputar nada.
- Patrón de logging con progreso/ETA de `h3_backfill` como plantilla del
  helper centralizado.

---

# TODO — Normalización del esquema (Gold)

Derivado de la revisión de normalización de `models.py` (2026-07-16).
Diagnóstico: catálogos geográficos OK (3NF), pero hay un PK compuesto erróneo
en actividades (2NF), atributos de entidad repetidos por periodo en
`establecimientos` (3NF), un grupo repetitivo de calles cruzadas (1NF) y
enums (`per_ocu`, `tipounieco`) cuya semántica vive solo en Python.
Objetivo: **esquema estrella en Gold** — Silver se queda ancho como espejo
de los CSV de INEGI (no normalizar Silver).

## N1 — Bugfix catálogo SCIAN (prioridad alta, alimenta Fase 1 del gate)

- [x] Cambiar PK de `denue_actividades_economicas` de `(codigo_act, nombre_act)`
      a `codigo_act` solo. Verificado en seed: 1,127 códigos únicos de 6
      dígitos, cero duplicados.
- [x] Materializar jerarquía SCIAN como columnas: `sector` (2 díg.),
      `subsector` (3 díg.), `rama` (4 díg.).
- [x] Migración Alembic `173b3ded90cd` (recrear tabla, DuckDB no soporta
      DROP CONSTRAINT: create-new → copy → drop → rename; aborta si detecta
      códigos con nombres múltiples) + modelo ORM + `scian.py`.
- [x] Seed parquet NO se modifica: la jerarquía se deriva del código al
      cargar (`_with_scian_hierarchy`). El loader es esquema-consciente para
      que la migración de seeds vieja (`dff9d0804542`) siga siendo replayable.

## N2 — Catálogo de estratos de personal (barata, valor inmediato)

- [x] Nueva tabla `denue_estratos_personal`: `per_ocu` PK, `empleados_min`,
      `empleados_max`, `categoria` (Micro/Small/Medium/Large/Unknown, valores
      del enum como fuente de verdad).
- [x] Migración Alembic `bbe078d563ac` + modelo ORM `EstratoPersonal` +
      loader `src_etl/loaders/estratos.py` (genera las 8 filas desde
      `PersonalOcupado`; integrado a `seed_catalog_tables`).
- [x] Verificado: cadena completa de 6 migraciones corre limpia contra un
      DuckDB scratch desde cero; ciclo downgrade→upgrade de las 2 nuevas OK;
      PK rechaza duplicados; 157 tests passed (baseline intacto).
- [x] Aplicado al Silver real: `make db-migrate` ejecutado por el usuario
      (2026-07-16) sin errores — catálogo SCIAN normalizado + estratos
      sembrados. Gold hereda en el próximo `make load`.

## N2.5 — Normalización de dominios ENUM + linkeo de catálogos (2026-07-17)

Los 4 dominios de tipo vivían como tipos ENUM de DuckDB en `establecimientos`
y los catálogos nuevos (p. ej. `denue_estratos_personal`) no se relacionaban
con nada. Normalizado:

- [x] 3 catálogos nuevos sembrados desde los enums (fuente única,
      `loaders/dominios.py`): `denue_tipos_vialidad` (39),
      `denue_tipos_asentamiento` (45), `denue_tipos_unidad_economica`
      (3, con `descripcion` — semántica FIJO/SEMIFIJO ahora en SQL).
- [x] Migración `ae66f32a9bed`: crea+siembra catálogos, convierte las 4
      columnas ENUM → VARCHAR (datos preservados) y borra los tipos.
      Maneja 3 limitaciones de DuckDB verificadas: ALTER bloqueado por
      CUALQUIER índice de usuario (baile drop→recrear), DROP INDEX debe
      commitearse antes del ALTER (commit intermedio a nivel DBAPI), y
      DROP TYPE con dependencia fantasma de sesión (best-effort).
- [x] `TipoVial` (enums.py) sincronizado con el dominio real del ingest:
      33 → 39 valores (faltaban CALLE DE RODAJE, PUENTE, TUNEL, PASO A
      DESNIVEL, GLORIETA, RAMPA); `ENUM_CATALOGS` → `DOMAIN_CATALOGS`,
      ahora DERIVADO de los enums (antes lista duplicada a mano).
- [x] **Linkeo**: relationships viewonly en `Establecimiento` (`actividad`,
      `estrato`, `tipo_vialidad`, `tipo_asentamiento`, `tipo_unidad`,
      `entidad`, `municipio`, `localidad`). SIN FK físico hacia los
      catálogos de dominio A PROPÓSITO: cada FK en DuckDB crea un índice
      ART implícito sobre 77M filas (escrituras ~1000x más lentas, medido)
      y DuckDB no soporta ADD CONSTRAINT. Integridad = validate_integrity()
      del ingest (mismo dominio que siembra los catálogos). FKs de
      geografía (físicos, preexistentes) se conservan.
- [x] Helpers redundantes de `Establecimiento` podados (get_employee_range,
      get_business_size, get_scian_sector/subsector — ítem de la auditoría
      de código muerto): esa semántica vive en los catálogos.
- [x] Writer DDL → VARCHAR; `_bootstrap_seeds` ya no crea tipos ENUM y
      siembra los dominios. Verificado: cadena de 8 migraciones desde cero,
      ciclo downgrade→upgrade, y simulación del Silver real (columnas ENUM
      + índice + datos → convertido con datos intactos y JOINs funcionando).
      Tests nuevos `test_dominio_catalogs.py` (11); suite 168 en verde.
- [x] **Aplicado al Silver real por el usuario (2026-07-18)** vía
      `make db-migrate` (junto con N2.6). `make load` pendiente para
      propagar a Gold.

## N2.6 — Esquema relacional por surrogate ids con PK/FK reales (2026-07-17/18)

Decisión del usuario: relaciones FÍSICAS por id, no joins por string
(supersede el "sin FK físico" de N2.5). Implementado:

- [x] Catálogos con `id INTEGER PRIMARY KEY` (posición 1-based en su enum,
      `enums.domain_id()` — **los enums quedan append-only**), label
      UNIQUE y columna `descripcion` con el significado de cada valor
      (39 vialidades + 45 asentamientos + 3 unidades; estratos ya
      materializa min/max/categoría).
- [x] **Seeds en parquet** (petición usuario 2026-07-18): los 4 catálogos
      nuevos se materializan como `data/seeds/seed_tipos_*.parquet` y
      `seed_estratos_personal.parquet` (mismo patrón que geografía/SCIAN,
      trackeados en git). Los enums + descripciones en código son la
      fuente que GENERA los archivos: si un parquet falta o difiere, el
      loader lo regenera (`ensure_seed_parquets`/`ensure_seed_parquet`);
      la carga a DB siempre lee del parquet.
- [x] `establecimientos` con **8 FKs reales**: `tipo_vial_id`,
      `tipo_asent_id`, `tipounieco_id`, `per_ocu_id`, `codigo_act` (SCIAN,
      verificado 0 huérfanos en el Silver real) + los 3 de geografía
      (ahora también declarados en el ORM). DDL canónico único:
      `loaders.denue.ESTABLECIMIENTOS_DDL` = modelo ORM.
- [x] Ingest: writer convierte label→id vía LEFT JOIN con catálogos;
      `validate_integrity()` (labels) + `_validate_codigo_act()` dan
      fail-fast claro ANTES del FK. `SILVER_READ_QUERY` resuelve labels
      por JOIN con los mismos nombres de columna → pipeline CLEE intacto.
- [x] Migración `c3a26327aec3`: catálogos al esquema nuevo + rebuild de la
      tabla de hechos si está vacía; con datos imprime la instrucción del
      script. Loaders reseedean solo si el contenido difiere (seguro con
      FKs entrantes; DELETE bloqueado por DuckDB si hay referencias).
- [x] **Script ONE-TIME `scripts/reorganize_domain_ids.py`**
      (`make reorganize-domain-ids`): reconstruye la tabla (DuckDB no
      tiene ADD CONSTRAINT y DROP COLUMN está bloqueado por los índices
      implícitos de FKs) — copia por periodo con progreso/ETA, verifica
      paridad por dominio antes del swap, recrea índices traducidos a
      `*_id`, CHECKPOINT. Idempotente; una corrida fallida no toca el
      original. Requiere ~1.3x el tamaño de la base en disco libre.
- [x] Verificado end-to-end en scratch: cadena de 9 migraciones desde
      cero (8 FKs), ciclo downgrade→re-upgrade, simulación del Silver
      real (ENUM + datos → migraciones → script → paridad 5,000/5,000,
      FK rechaza ids inválidos, SILVER_READ_QUERY OK, idempotencia),
      smoke del writer (label→id + guard de codigo_act). 172 tests verdes.
- [x] **Aplicado al Silver real por el usuario (2026-07-18)**:
      `make db-migrate` + `make reorganize-domain-ids`. Verificado
      post-aplicación (2026-07-18): revisión `c3a26327aec3`; 76,758,727
      filas (conteo exacto preservado); 4 columnas `*_id` / 0 labels
      viejos; 8 FKs activos; 0 NULLs en los 4 ids (paridad total con los
      labels originales); 0 tipos ENUM huérfanos; `ix_estab_h3_9`
      recreado; `clee_propagado_radio` y `h3_cell_9/5` intactos (76.7M);
      catálogos 39/45/3/8 con descripción; `SILVER_READ_QUERY` resuelve
      labels OK.
- [ ] (Opcional, limpieza) **Eliminar `scripts/reorganize_domain_ids.py`
      + target `reorganize-domain-ids` del Makefile**: era ONE-TIME y ya
      se aplicó al Silver real — nunca volverá a usarse (bases nuevas
      nacen relacionales vía writer/migraciones; el script queda en el
      historial de git si se necesitara consultar). Borrar también
      `data/etl_reorganize_ids.log` cuando estorbe.
- [ ] `make load` para propagar el esquema nuevo a Gold. Nota: el archivo
      .duckdb del Silver no encoge tras el rebuild (bloques se
      reutilizan); compactar con COPY FROM DATABASE si se desea.

## N3 — Dimensión de entidad económica (la clave para la tesis)

Depende de que la resolución CLEE esté estable. Es la misma tabla que la
"KB consolidada" de la Fase 5 del plan CLEE — un solo artefacto, dos usos.

- [ ] Nueva tabla `entidades_economicas` (una fila por `clee_propagado_radio`):
      `clee_propagado_radio` PK, `es_sintetica` (prefijo NEW_*), atributos del periodo
      más reciente (`nom_estab`, `raz_social`, `codigo_act`, `cve_ent`,
      `cve_mun`, `cod_postal`, `latitud`, `longitud`, `h3_cell_9`),
      `primer_periodo`, `ultimo_periodo`, `n_periodos`, `fecha_alta_date`.
- [ ] Paso de construcción en el pipeline (post CLEE-resolution, en Load):
      agregación por `clee_propagado_radio` sobre Silver → tabla en Gold.
- [ ] Migración Alembic + modelo ORM.
- [ ] `establecimientos` pasa a leerse como tabla de observaciones
      (hechos por periodo); documentar el patrón de consulta de
      supervivencia/mortalidad sobre la dimensión.
- [ ] OJO: los `clee_propagado_radio` sintéticos (NEW_*) no son estables entre corridas
      con `--force` — reconstruir la dimensión tras cada re-resolución.

## N4 — (Opcional) 1NF de calles cruzadas

- [ ] Tabla `establecimiento_vialidades` (`establecimiento_id` FK,
      `secuencia` 1-3, `tipo_vial`, `nom_vial`) reemplazando
      `tipo_v_e_1/2/3` + `nom_v_e_1/2/3`. Solo si se necesita consultarlas;
      valor analítico bajo (no se usan en matching).

## Decisiones: qué NO normalizar

- Silver completo (espejo de fuente, ancho a propósito).
- Dirección/asentamientos como catálogos (la compresión columnar de DuckDB
  ya deduplica esos strings).
- Contacto (telefono/correoelec/www) en tabla aparte.
- Duplicados derivados aceptados como lineage/performance: `fecha_alta_str`
  + `fecha_alta_date`, `clee` + `clee_propagado_radio`, `h3_cell_5` (derivable de
  `h3_cell_9`).

---

## Notas

- Índices H3 válidos caben en BIGINT firmado (bit alto siempre 0).
- Res 9: arista promedio ≈ 174m; res 5: ≈ 8.5km.
- El plan anterior de este archivo (reestructuración a monolito modular en
  `app/`) fue reemplazado por decisión del usuario el 2026-07-16; recuperable
  del historial de la conversación si se retoma.
