# Comandos de operación

Todos se corren **dentro de indominus**, desde `~/tesis-maestria-etl`. El
login es por contraseña, así que no van envueltos en `ssh`.

Las corridas largas van en `tmux` para que sobrevivan a que se caiga la
sesión SSH — una corrida pegada a la terminal muere con ella.

---

## 1. Base de datos de pruebas

**No hay que crearla a mano.** `conftest.py` la crea en el arranque de la
sesión de pytest si el servidor responde y la base no existe. Solo se
saltan los tests cuando no hay servidor, que es cuando saltarlos es lo
correcto.

La URL se deriva de la config del proyecto cambiando el nombre de la base
por `<POSTGRES_DB>_test`, y nunca devuelve la de producción: crearla no pone
en riesgo `mipymes` ni los ZIP descargados. El esquema tampoco se crea a
mano — el fixture `pg_engine` lo construye desde el metadata del ORM.

Si el rol no tiene permiso para crear bases, el motivo del skip lo dice y
trae el comando:

```bash
docker compose exec -T postgres psql -U postgres \
  -c 'CREATE DATABASE "mipymes_test" OWNER etl_rw;'
```

```bash
# Verificar que existe
docker compose exec -T postgres psql -U etl_rw -d postgres -c "\l mipymes_test"

# Empezar de cero (la base de pruebas es desechable por definición)
docker compose exec -T postgres psql -U etl_rw -d postgres \
  -c "DROP DATABASE IF EXISTS mipymes_test;" \
  -c "CREATE DATABASE mipymes_test OWNER etl_rw;"
```

### Correr los tests

```bash
docker compose run --rm tools pytest -q                 # todo
docker compose run --rm tools pytest -m postgres -q     # solo los que tocan base
docker compose run --rm tools pytest -m "not postgres" -q
docker compose run --rm tools pytest tests/unit/test_staging.py -q -x
```

Si la salida dice `SKIPPED ... Postgres de pruebas no disponible`, la base
de pruebas falta o el servidor no responde: el motivo va entre paréntesis.

---

## 2. Extract — descarga de Bronze

```bash
# Todo: DENUE (Selenium) + SESNSP (CKAN)
docker compose run --rm etl extract

# Solo una fuente
docker compose run --rm etl extract --only denue
docker compose run --rm etl extract --only sesnsp

# Re-descargar todo ignorando lo ya bajado
docker compose run --rm etl extract --force
```

**Sin `--force` ya reintenta lo fallido.** El scraper se salta las URLs con
al menos una descarga `SUCCESS` en `meta.download_tasks`, así que lo que
quedó en error vuelve a la cola solo. `--force` es para reconstruir Bronze
entero, no para reintentar.

`extract` sale con **código 1** si alguna descarga falló — hoy eso incluye
el CSV de SESNSP, que devuelve 403 desde `repodatos.atdt.gob.mx`. Es un
problema de la fuente, no del pipeline.

---

## 3. Transform — Bronze a Silver

Tres pasos independientes. Correrlos por separado es lo que permite repetir
uno sin tocar los otros.

### Ingest: ZIP → `silver.establecimientos`

```bash
docker compose run --rm etl transform --only ingest

# Reconstruir todos los cortes
docker compose run --rm etl transform --only ingest --force

# Estrenar con un corte antes de soltar los veinte
docker compose run --rm etl transform --only ingest --period 03_2018
```

La unidad de carga es el **corte**, no el archivo: las decenas de ZIP de un
periodo se concatenan y se enganchan de golpe como partición. Un corte que
falla no detiene a los demás — su partición no se enganchó y Silver sigue
consistente.

### Resolución CLEE radio → `silver.clee_radio`

```bash
docker compose run --rm etl transform --only clee-radio

# Reconstruir la tabla entera
docker compose run --rm etl transform --only clee-radio --force
```

El método de referencia de la tesis: candidatos por radio métrico
(BallTree) + similitud difusa.

### Resolución CLEE H3 → `silver.clee_h3`

```bash
docker compose run --rm etl transform --only clee-h3
docker compose run --rm etl transform --only clee-h3 --force
```

Pista de comparación: mismos umbrales, candidatos por celda H3 en vez de
radio. **No sustituye a radio**, convive con ella.

Cada método escribe **solo su tabla**, así que `--force` en uno no puede
dañar al otro.

### Las dos resoluciones juntas

```bash
docker compose run --rm etl transform --only clee          # alias de grupo
```

### Todo el transform

```bash
docker compose run --rm etl transform                      # ingest + clee + sesnsp
docker compose run --rm etl transform --no-sesnsp          # solo DENUE
docker compose run --rm etl transform --only denue         # ingest + las dos CLEE
```

---

## 4. Todo junto

```bash
# Extract + transform, de cero
tmux new -d -s etl 'docker compose run --rm etl pipeline --extract --force 2>&1 | tee -a logs/pipeline-$(date +%Y%m%d-%H%M).out'

# Solo transform (Bronze ya está en disco)
tmux new -d -s etl 'docker compose run --rm etl pipeline --force 2>&1 | tee -a logs/pipeline-$(date +%Y%m%d-%H%M).out'
```

`--extract` está **apagado por default** en `pipeline`: sin él solo
transforma, que es lo que se quiere cuando los ZIP ya están.

**Aislamiento:** `extract` corre sus fuentes aisladas (una que falle no
impide la otra), pero `pipeline --extract` **no**: ahí una excepción aborta
la corrida completa. Si quieres aislamiento con el pipeline, encadena con
`;` y no con `&&` — `extract` sale con código 1 por el 403 de SESNSP y
`&&` saltaría el transform:

```bash
tmux new -d -s etl 'docker compose run --rm etl extract ; docker compose run --rm etl transform --force 2>&1 | tee -a logs/run-$(date +%Y%m%d-%H%M).out'
```

---

## 5. Seguimiento

### Seguir una corrida en vivo

Lo más simple es no tocar archivos:

```bash
tmux attach -t clee        # Ctrl-b d para salir sin matarla
```

Si prefieres `tail -f`, lanza con el symlink `logs/latest.out` y la ruta deja
de cambiar entre corridas:

```bash
tmux new -d -s clee 'F=logs/clee-radio-$(date +%Y%m%d-%H%M).out; ln -sfn "$(basename $F)" logs/latest.out; docker compose run --rm etl transform --only clee-radio --force 2>&1 | tee -a "$F"'

tail -f logs/latest.out    # siempre la corrida más reciente
```

### Qué archivo leer

**El `.out` del `tee`**, siempre. Es el único con la vista completa y el
único que es por corrida.

| Archivo | Qué trae | Se acumula |
|---|---|---|
| `logs/<etapa>-<fecha>.out` | **todo**: el stage, el `COPY` y el `staging` | no, uno por corrida |
| `data/etl_process.log` | solo las líneas del stage | **sí**, entre corridas |
| `logs/downloads/<fecha>/run_*/app.log` | solo descargas (JSON) | no |

`copy.py` y `staging.py` escriben por structlog, que va a stdout; el stage
logger va a su archivo **y** a stdout. Por eso `data/etl_process.log` no
tiene las líneas de `COPY`, y por eso un `grep` sobre él se queda corto.

Que se acumule es lo que hace que `grep ERROR | tail -5` devuelva errores de
anteayer. Con el `.out` ese problema no existe.

### ¿Sigue vivo?

Un solo comando responde las dos cosas — si la sesión existe y hace cuánto
escribió:

```bash
L=$(ls -t logs/*.out | head -1); tmux ls; echo "$L | hace $(( $(date +%s) - $(stat -c %Y "$L") ))s"; tail -2 "$L"
```

El `COPY` emite un latido por bloque de 500k filas con el acumulado y la
velocidad, así que durante una carga de millones de filas el log avanza cada
pocos segundos. Si la última línea tiene **más de un par de minutos**, ahí sí
hay algo raro.

```
COPY en curso  table=silver.establecimientos_2015_01_staging bloque=3/6
               filas=1500000 de=2683667 segundos=12.4 filas_s=120967
```

Cuando nada escribe y quieres saber qué hace el servidor:

```bash
docker stats --no-stream --format '{{.Name}}  CPU {{.CPUPerc}}  MEM {{.MemUsage}}' | grep -iE 'etl|postgres'
```

Logs estructurados (un directorio por corrida; `latest` apunta a la más
reciente, y DENUE y SESNSP crean uno cada uno):

```bash
tail -f logs/downloads/latest/app.log
grep -h '"level": "error"' logs/downloads/$(date +%Y-%m-%d)/*/app.log
tail -f logs/downloads/latest/app.log | jq -r '"\(.timestamp[11:19]) \(.level) \(.event) \(.activity // .url // "")"'
```

---

## 6. Estado de la base

```bash
# Revisión de esquema aplicada
docker compose exec -T postgres psql -U etl_rw -d mipymes -c "select version_num from meta.alembic_version;"

# Historial de corridas
docker compose exec -T postgres psql -U etl_rw -d mipymes -c \
  "select job_name, status, total_tasks, tasks_succeeded, tasks_failed, left(coalesce(error_summary,''),60) as err
   from meta.job_runs order by start_time desc limit 10;"

# Archivos SIN ningún intento exitoso (lo que de verdad falta)
docker compose exec -T postgres psql -U etl_rw -d mipymes -c \
  "select t.task_name, t.error_type, left(coalesce(t.error_message,''),70) as err
   from meta.download_tasks t
   where t.status <> 'SUCCESS'
     and not exists (select 1 from meta.download_tasks s
                     where s.source_url = t.source_url and s.status = 'SUCCESS')
   order by t.task_name;"

# Cobertura de la resolución por método y origen
docker compose exec -T postgres psql -U etl_rw -d mipymes -c \
  "select origen, count(*) filas, count(distinct clee_resuelto) identidades
   from silver.clee_radio group by origen order by origen;"
```

Un archivo fallido deja su fila `FAILED` para siempre: el reintento
**inserta una fila nueva** en vez de actualizarla. Por eso la pregunta útil
es "¿tiene algún `SUCCESS`?" y no "¿su última fila es `FAILED`?".

### Endpoints de observabilidad

```bash
curl -s localhost:8000/api/v1/ops/health/pipeline | jq
curl -s localhost:8000/api/v1/ops/runs | jq '.[0:5]'
curl -s localhost:8000/api/v1/ops/runs/<run_id>/tasks | jq
```

El API entra con el rol `api_ro`, que solo tiene `SELECT` en `gold` y
`meta`: no puede leer `silver` ni escribir nada.

---

## 7. Empezar de cero

Destructivo. Borra la base **y** los ZIP descargados.

```bash
docker compose down -v                                        # tira el volumen de datos
docker run --rm -v "$PWD/data:/data" alpine \
  rm -rf /data/bronze/denue /data/bronze/sesnsp               # los ZIP son de root
docker compose up -d                                          # base + migraciones limpias
```

`data/seeds/` **no se toca**: esos parquets no se re-descargan de INEGI,
vienen en git.

Los archivos los escribe el contenedor como `root`, así que se borran desde
un contenedor. En indominus no hay `sudo` disponible.

---

## 8. Métricas de la resolución

```bash
docker compose run --rm tools python scripts/clee_propagation_stats.py
docker compose run --rm tools python scripts/clee_propagation_stats.py --method radio
docker compose run --rm tools python scripts/clee_propagation_stats.py --method radio --method h3
docker compose run --rm tools python scripts/clee_propagation_stats.py --no-identity-metrics
```

Sin `--method` analiza todos los registrados y compara los dos primeros fila
a fila. Las métricas por identidad son la parte cara (`GROUP BY` sobre 82.9M
filas); `--no-identity-metrics` las omite.

---

## 9. Mantenimiento

```bash
docker compose build etl tools      # tras un git pull que toque src/
docker compose up -d                # aplica migraciones pendientes
docker compose ps
docker compose logs -f postgres
docker compose exec postgres psql -U etl_rw -d mipymes
```

El código va **horneado** en la imagen `etl`: un `git pull` que toque `src/`
no surte efecto hasta reconstruirla. Es a propósito — una corrida del
pipeline debe ser reproducible desde la imagen, no depender del working
tree.
