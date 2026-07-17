# TODO — Optimización de la resolución CLEE (hot path + blocking H3)

Plan derivado de la revisión de eficiencia de la propagación CLEE (2026-07-16).
El cuello de botella dominante es el loop de matching en Python (gate de
actividad fuzzy recalculado por par + round-trip `to_dicts()`), no el BallTree.
Se atacan primero los quick wins sin cambio arquitectónico y después se migra
el pre-filtro espacial a blocking con celdas H3.

**REGLA DE ORO: no correr migraciones ni `ALTER TABLE` sobre los `.duckdb` de
`data/` sin confirmación explícita.** Los cambios de esquema se preparan en
código (migración Alembic + modelo ORM) y se aplican en un paso controlado.

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
- [ ] **Backfill del Silver real (~100M filas): BLOQUEADO — DBeaver
      mantiene lock sobre `data/silver/denue.duckdb`.** Cerrar la conexión
      de DBeaver y correr `make h3-backfill`.
- [ ] Verificación post-backfill: % de filas con celda no nula ≈ % con
      coords válidas; spot-check de celdas contra h3-py.

## Fase 4 — Reemplazar BallTree por blocking H3

- [ ] Generación de candidatos = join por celda: target expandido con
      `grid_disk(h3_cell_9, k=3)` (k=3 en res 9 garantiza cubrir ≥500m en el
      peor caso) contra `h3_cell_9` de la KB.
- [ ] Post-filtro haversine exacto ≤ `SPATIAL_RADIUS_M` (vectorizado en
      Polars/numpy) para recuperar el radio exacto de 500m.
- [ ] Sustituir `find_spatial_neighbors` manteniendo la interfaz
      (indices, distances) para no tocar la cascada de matching.
- [ ] Particionado espacial por `h3_cell_5` en vez de slices arbitrarios de
      KB (`KB_MAX_ROWS_PER_PARTITION`): las filas pendientes solo consultan
      particiones con solape espacial real.
- [ ] Config: nuevas constantes en `clee_resolution_config.py`
      (`H3_BLOCKING_RES = 9`, `H3_GRID_DISK_K = 3`, `H3_PARTITION_RES = 5`).

## Fase 5 — (Opcional, habilitado por H3) KB consolidada

- [ ] Evaluar KB nacional deduplicada por `clee_master` (atributos del periodo
      más reciente): con candidatos generados vía join en DuckDB (out-of-core),
      la RAM deja de ser la restricción y el loop T×R colapsa a una pasada por
      target.

## Fase 6 — Validación y documentación

- [ ] Paridad de matches: correr un periodo muestra con BallTree vs H3 y
      comparar asignaciones de `clee_master` (deben ser ≈ idénticas; diferencias
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

## N3 — Dimensión de entidad económica (la clave para la tesis)

Depende de que la resolución CLEE esté estable. Es la misma tabla que la
"KB consolidada" de la Fase 5 del plan CLEE — un solo artefacto, dos usos.

- [ ] Nueva tabla `entidades_economicas` (una fila por `clee_master`):
      `clee_master` PK, `es_sintetica` (prefijo NEW_*), atributos del periodo
      más reciente (`nom_estab`, `raz_social`, `codigo_act`, `cve_ent`,
      `cve_mun`, `cod_postal`, `latitud`, `longitud`, `h3_cell_9`),
      `primer_periodo`, `ultimo_periodo`, `n_periodos`, `fecha_alta_date`.
- [ ] Paso de construcción en el pipeline (post CLEE-resolution, en Load):
      agregación por `clee_master` sobre Silver → tabla en Gold.
- [ ] Migración Alembic + modelo ORM.
- [ ] `establecimientos` pasa a leerse como tabla de observaciones
      (hechos por periodo); documentar el patrón de consulta de
      supervivencia/mortalidad sobre la dimensión.
- [ ] OJO: los `clee_master` sintéticos (NEW_*) no son estables entre corridas
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
  + `fecha_alta_date`, `clee` + `clee_master`, `h3_cell_5` (derivable de
  `h3_cell_9`).

---

## Notas

- Índices H3 válidos caben en BIGINT firmado (bit alto siempre 0).
- Res 9: arista promedio ≈ 174m; res 5: ≈ 8.5km.
- El plan anterior de este archivo (reestructuración a monolito modular en
  `app/`) fue reemplazado por decisión del usuario el 2026-07-16; recuperable
  del historial de la conversación si se retoma.
