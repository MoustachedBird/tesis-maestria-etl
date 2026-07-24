# Reconstruir Silver desde cero

El estándar del proyecto: la base se crea desde cero, no se migra en sitio.
Toda la secuencia bajo `caffeinate -dims` — son ~28 horas y un reposo de
macOS a media carga la interrumpe.

## Antes de empezar

**No borres `data/metadata.duckdb`.** De ahí sale la lista de los 943
archivos a procesar (`download_tasks`); sin él habría que re-extraer todo
desde INEGI.

Si existe una base previa, respáldala renombrando en vez de borrar:

```bash
mv data/silver/mipymes.duckdb data/silver/mipymes_backup_$(date +%Y%m%d).duckdb
mv data/silver/mipymes.duckdb.wal data/silver/mipymes_backup_$(date +%Y%m%d).duckdb.wal 2>/dev/null
```

## Secuencia

```bash
# 1. Esquema + siembra de catálogos (3 migraciones consolidadas)
make db-migrate

# 2. Ingest + normalize-dates + h3-cells de los 943 archivos
caffeinate -dims make transform ARGS="--force --no-clee-resolution"

# 3. SESNSP (catálogo de delitos + hecho mensual)
caffeinate -dims make transform-sesnsp ARGS="--force"

# 4. Resolución CLEE — la etapa más larga
caffeinate -dims make clee-resolution-radio ARGS="--force"

# 5. Dimensión N3 (SIEMPRE después de la resolución CLEE)
make build-entidades

# 6. Opcional: Silver -> Gold (copia del archivo completo)
make load
```

### Por qué `--force` es obligatorio en el paso 2

`metadata.duckdb` recuerda como SUCCESS los archivos procesados contra la
base anterior. Sin `--force`, el modo incremental los saltaría y te
quedarías con una base vacía. Con `--force`, la lista de trabajo se arma
desde `download_tasks` (las descargas), no desde el historial de
procesamiento. `--force` **vacía** la tabla de hechos; no la dropea, para
que el esquema siga viniendo de la migración.

### Índices

No hay nada que hacer. El ingest y la resolución CLEE los suspenden solos
mientras escriben y los restituyen al terminar, incluso si la etapa falla.
`make db-status` reporta cuántos hay presentes.

## Qué observar mientras corre

```bash
tail -f data/etl_process.log | grep -E "Procesando|Éxito|❌"
```

Cada stage escribe su propio archivo en `data/`: `etl_process.log` (ingest),
`etl_normalize.log`, `etl_h3_backfill.log`, `etl_clee_resolution_radio.log`,
`etl_sesnsp.log`, `etl_entidades.log`.

Si un archivo falla, el pipeline sigue con el siguiente y lo marca FAILED en
metadata; se reintenta en la corrida siguiente. Los errores accionables
esperables son de catálogo (una localidad o un código SCIAN nuevo): ver
"Cortes nuevos de INEGI" en AGENTS.md.

## Si el proceso muere

DuckDB commitea por archivo, así que lo ingerido está a salvo. Reanudar es
correr el paso 2 **sin `--force`** (con `--force` empezaría de cero), pero
antes verificar en metadata que los pendientes no tengan SUCCESS viejos que
el incremental saltaría.

## Incorporar un corte nuevo (p. ej. 2027)

No es un rebuild: se extrae el corte y se corre el transform **incremental**
(sin `--force`), luego la resolución CLEE y `build-entidades`. Si el corte
trae localidades o etiquetas nuevas, el fail-fast las nombra y se agregan al
seed correspondiente.
