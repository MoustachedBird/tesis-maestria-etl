# Arranque en limpio

Copiar y pegar. Cada bloque es independiente; correr en orden la primera vez.

## 0. Configuración (una sola vez por máquina)

```bash
cd ~/tesis-maestria-etl
cp -n .env.example .env
```

Editar `.env` y poner `POSTGRES_PASSWORD` y `POSTGRES_RO_PASSWORD`.

Los contenedores corren como root, así que lo que escriben en `logs/` y
`data/` queda con dueño root. Para borrarlo sin `sudo`, el bloque 1 usa un
contenedor.

## 1. Borrar todo y reconstruir

Destruye la base de datos, los logs y el Bronze descargado.

```bash
cd ~/tesis-maestria-etl
kill %1 2>/dev/null
docker ps -q --filter "name=tesis-maestria-etl-etl-run" | xargs -r docker rm -f
docker ps -q --filter "name=tesis-maestria-etl-tools-run" | xargs -r docker rm -f
docker compose down -v
docker run --rm --entrypoint sh -v "$PWD:/w" -w /w python:3.12-slim -c \
  'rm -rf logs/downloads data/bronze data/etl_*.log extract.log ingest.log clee.log permanencia.log htmlcov coverage.xml'
git pull
docker compose --profile manual --profile tools build
docker compose up -d
docker compose logs migrate --no-log-prefix | tail -25
```

`--profile manual --profile tools` no es opcional: `docker compose build`
sin ellos salta los servicios que viven bajo profile, y `etl` es uno. Se
queda con la imagen vieja sin avisar.

Lo que escribieron corridas anteriores quedó con dueño root. El contenedor
de arriba también es root, así que borra sin `sudo`.

Esperado (la cadena son once revisiones; `b0000` crea y las demás ajustan):

```
   Schemas creados: silver, gold, meta, verificacion
   silver: 11 tablas
   meta: 5 tablas
   Rol api_ro: SELECT en gold, meta (sin acceso a silver)
   Geografia: {'denue_entidades': 32, 'denue_municipios': 2486, 'denue_localidades': 40388}
   SCIAN: {'denue_actividades_economicas': 1127}
   Dominios: {'denue_tipos_vialidad': 39, 'denue_tipos_asentamiento': 45, 'denue_tipos_unidad_economica': 3}
   Estratos: {'denue_estratos_personal': 8}
   silver: 2 tablas de resolucion CLEE (clee_radio, clee_h3)
   silver: 2 tablas de permanencia (permanencia_radio, permanencia_h3)
   silver.entidades_economicas eliminada (estaba vacia)
   silver: 4 vistas de permanencia eliminadas
   silver: 2 tablas de permanencia recreadas con 45 columnas, sin valores de un corte concreto (permanencia_radio, permanencia_h3)
   hay que reconstruirlas: transform --only permanencia --force
   silver: silver.establecimientos.h3_cell_7 agregada (NULL en lo ya cargado)
   silver: clee_celda_h3_7 creada (metodo `celda_h3_7`, sin particiones)
   gold: negocios_clee_oficial creada (38 columnas, vacia)
   verificacion: 4 tablas creadas, vacias (muestras, muestra_negocios, veredictos, evidencias)
   verificacion: SELECT para api_ro
```

Al final: **16 tablas en `silver`** (11 del baseline + 3 de CLEE + 2 de
permanencia), **5 en `meta`** (más `alembic_version`, que es de Alembic),
**1 en `gold`** (`negocios_clee_oficial`, vacía hasta `transform --only
gold`) y **4 en `verificacion`** (vacías hasta que corran el muestreo y el
agente).

## 2. Descargar Bronze

484 archivos desde INEGI. Horas.

```bash
cd ~/tesis-maestria-etl
nohup docker compose run --rm etl extract > extract.log 2>&1 &
sleep 10
tail -f logs/downloads/latest/app.log
```

`Ctrl+C` corta el `tail`, no la descarga.

## 3. Vigilar

```bash
cd ~/tesis-maestria-etl
tail -f logs/downloads/latest/errors.log
```

```bash
docker compose exec postgres psql -U etl_rw -d mipymes -c \
"SELECT status, COUNT(*), pg_size_pretty(SUM(file_size_bytes)) AS bytes
 FROM meta.download_tasks GROUP BY 1"
```

```bash
docker compose exec postgres psql -U etl_rw -d mipymes -c \
"SELECT period,
        COUNT(*) FILTER (WHERE status='SUCCESS') AS ok,
        COUNT(*) FILTER (WHERE status='FAILED')  AS fallidos
 FROM meta.download_tasks GROUP BY 1 ORDER BY 1"
```

```bash
docker compose exec postgres psql -U etl_rw -d mipymes -c \
"SELECT task_name, error_type, error_message
 FROM meta.download_tasks WHERE status='FAILED' LIMIT 20"
```

```bash
du -sh data/bronze; find data/bronze -name '*.zip' | wc -l
```

Esperado al terminar: 484 en SUCCESS, ~9.1G, 20 cortes.

## 4. Cargar UN corte y revisarlo

```bash
cd ~/tesis-maestria-etl
docker compose run --rm etl transform --only ingest --period 03_2018
```

```bash
docker compose exec postgres psql -U etl_rw -d mipymes -c \
"SELECT period, COUNT(*) FROM silver.establecimientos GROUP BY 1 ORDER BY 1"
```

```bash
docker compose exec postgres psql -U etl_rw -d mipymes -c \
"SELECT COUNT(*) AS filas,
        COUNT(fecha_alta_date) AS con_fecha,
        COUNT(h3_cell_9) AS con_h3,
        COUNT(clee) AS con_clee
 FROM silver.establecimientos"
```

```bash
docker compose exec postgres psql -U etl_rw -d mipymes -c \
"SELECT task_name, status, rows_extracted, error_message
 FROM meta.process_tasks ORDER BY created_at DESC LIMIT 25"
```

## 5. Cargar los 20 cortes

Horas.

```bash
cd ~/tesis-maestria-etl
nohup docker compose run --rm etl transform --only ingest > ingest.log 2>&1 &
sleep 10
tail -f ingest.log
```

```bash
docker compose exec postgres psql -U etl_rw -d mipymes -c \
"SELECT period, COUNT(*) FROM silver.establecimientos GROUP BY 1 ORDER BY 1"
```

```bash
docker compose exec postgres psql -U etl_rw -d mipymes -c \
"SELECT COUNT(*) FROM silver.establecimientos"
```

Esperado: `82896801` filas en 20 periodos.

## 6. SESNSP

```bash
cd ~/tesis-maestria-etl
docker compose run --rm etl extract --only sesnsp
docker compose run --rm etl transform --only sesnsp
docker compose exec postgres psql -U etl_rw -d mipymes -c \
"SELECT COUNT(*) FROM silver.sesnsp_incidencia_municipal"
```

## 6.5. Rellenar `h3_cell_7` (antes de la resolución por celda)

`b0008` agregó la columna y el ingest la calcula desde entonces, pero los
cortes que ya estaban cargados la traen en NULL. Va **antes** de
`clee-celda-h3-7`, que la usa *como* criterio: con la columna vacía esa
resolución no falla — corre sobre nada y produce una tabla vacía.

```bash
cd ~/tesis-maestria-etl
nohup docker compose run --rm etl \
  transform --only h3-cell-7 > h3_7.log 2>&1 &
sleep 10
tail -f h3_7.log
```

Idempotente y reanudable: solo toca filas en NULL, así que volver a correrlo
cuesta un conteo. **No acepta `--force`** (el padre de una celda no cambia).

```bash
docker compose exec postgres psql -U etl_rw -d mipymes -c \
"SELECT count(*) AS sin_padre FROM silver.establecimientos
 WHERE h3_cell_9 IS NOT NULL AND h3_cell_7 IS NULL"
```

Tiene que dar **0** antes de seguir.

## 7. Resolución CLEE

Es la etapa más larga: ~1 noche por método. Escribe `silver.clee_<metodo>`,
no `establecimientos`.

```bash
cd ~/tesis-maestria-etl
nohup docker compose run --rm etl transform --only clee-radio > clee.log 2>&1 &
sleep 10
tail -f clee.log
```

`clee-h3` es el track de comparación y cuesta otra noche; solo si hace falta:

```bash
nohup docker compose run --rm etl transform --only clee-h3 > clee_h3.log 2>&1 &
```

```bash
docker compose run --rm tools python scripts/clee_propagation_stats.py
```

```bash
docker compose exec postgres psql -U etl_rw -d mipymes -c \
"SELECT region_group, status, entities_input, entities_matched,
        round((duration_seconds / 3600)::numeric, 1) AS horas
 FROM meta.clee_resolution_tasks ORDER BY created_at DESC LIMIT 10"
```

## 8. Evaluación del matching

```bash
cd ~/tesis-maestria-etl
docker compose run --rm tools python scripts/evaluate_masked_clee.py extract
docker compose run --rm tools python scripts/evaluate_masked_clee.py evaluate
```

Esperado: 97.72% F1.

## 9. Dimensión N3: permanencia

Colapsa las 82.9M observaciones a una fila por establecimiento resuelto.
Horas, y va **después** de la resolución CLEE: lee `silver.clee_<metodo>`.

```bash
cd ~/tesis-maestria-etl
nohup docker compose run --rm etl transform --only permanencia --force > permanencia.log 2>&1 &
sleep 10
tail -f permanencia.log
```

`--only permanencia` corre los dos métodos; `--only permanencia-radio` para
uno solo. **Reanuda por default** (va troceada por corte y por bloque, y
cada vuelta commitea), así que `--force` solo hace falta para rehacerla
desde cero.

```bash
docker compose exec postgres psql -U etl_rw -d mipymes -c \
"SELECT COUNT(*) AS negocios,
        COUNT(*) FILTER (WHERE es_sintetica) AS sinteticos,
        COUNT(*) FILTER (WHERE cortes_tras_ultimo = 0) AS activos
 FROM silver.permanencia_radio"
```

Cobertura de cada campo oficial: qué fracción de los negocios tiene el dato
en al menos un corte. Lo que salga bajo acota qué análisis es posible, y lo
que salga en 100% es una bandera que se puede tirar en la siguiente
migración:

```bash
docker compose exec postgres psql -U etl_rw -d mipymes -c \
"SELECT round(100.0 * avg(tiene_razon_social::int), 1) AS razon_social,
        round(100.0 * avg(tiene_coordenadas::int), 1)  AS coordenadas,
        round(100.0 * avg(tiene_estrato_personal::int), 1) AS estrato,
        round(100.0 * avg(tiene_ageb::int), 1)         AS ageb,
        round(100.0 * avg(tiene_telefono::int), 1)     AS telefono
 FROM silver.permanencia_radio"
```

Los **valores** de cualquier campo, corte por corte, salen del join
(AGENTS.md §N3): `permanencia_radio` → `clee_radio` → `establecimientos`.

Cómo terminó, sin abrir el log:

```bash
docker compose exec postgres psql -U etl_rw -d mipymes -c \
"SELECT r.job_name, t.status, t.rows_extracted, t.rows_loaded,
        round(t.duration_seconds / 60) AS minutos, t.error_message
 FROM meta.process_tasks t JOIN meta.job_runs r USING (run_id)
 WHERE r.job_type = 'PERMANENCIA' ORDER BY t.created_at DESC"
```

Si una segunda corrida sale en `SKIPPED` es correcto: la tabla ya está
construida con la identidad vigente. Si la resolución CLEE se re-corrió
después, reconstruye sola.

## 10. Gold: la edad del negocio y si cerró

La capa de consumo, y existe para una sola cosa: calcular la **edad** de cada
negocio y decidir si **cerró**, sobre los cortes en que INEGI publica el CLEE
(9 cortes, 2021-05 → 2026-05). De ahí sale la muestra que se verifica en línea
con agentes para validar la premisa *"dejó de aparecer en el DENUE ⇒ quebró"*.

**No depende de la resolución CLEE** —lee el `clee` de los propios hechos— así
que se puede correr sin haber terminado las propagaciones.

### Antes de la primera tanda: la base tiene que estar en `b0010`

`b0009` se reescribió en sitio y `b0010gold_sin_snapshots` se retiró de la
cadena (las dos generaban el DDL desde un modelo que ya no existe). Una base
que aplicó cualquiera de las dos tiene en `meta.alembic_version` una revisión
que Alembic ya no encuentra, y `upgrade head` falla con *Can't locate
revision*. Se resuelve declarando la base en `b0008` y subiendo desde ahí. Es
seguro porque lo único que `b0009` tira es `gold`, que se reconstruye; Silver
y `meta` no se tocan.

**Con `--no-deps`, y no es opcional:** `tools` depende de que `migrate`
termine bien, y `migrate` es justamente el `alembic upgrade head` que falla
con la revisión huérfana. Sin `--no-deps`, cualquier `docker compose run
tools` muere con *service "migrate" didn't complete successfully: exit 255*
antes de ejecutar nada. Postgres sí tiene que estar arriba (`docker compose
up -d postgres`).

```bash
cd ~/tesis-maestria-etl
git pull
docker compose --profile manual --profile tools build
docker compose run --rm --no-deps tools alembic current 2>&1 | tail -1
```

Si lo anterior dice `b0010verificacion_agente (head)`, la base ya está al
día. Si dice *Can't locate revision* (o `b0009gold_cohorte_oficial`):

```bash
docker compose run --rm --no-deps tools alembic stamp --purge b0008clee_celda_h3_7
docker compose run --rm --no-deps tools alembic upgrade head
docker compose run --rm --no-deps tools alembic current
```

A partir de aquí `migrate` vuelve a pasar solo y `tools` deja de necesitar
`--no-deps`.

Esperado del `upgrade`: `gold: negocios_clee_oficial creada (38 columnas,
vacia)` y `verificacion: 4 tablas creadas`.

### La tanda

Con `--force`: la tabla de trabajo de una corrida anterior puede seguir en
`gold` con otra forma, y `--force` la tira antes de empezar. La corrida
anterior (misma máquina, 32 bloques) tardó **~1 h 40 min**.

```bash
cd ~/tesis-maestria-etl
nohup docker compose run --rm etl \
  transform --only gold --force > gold.log 2>&1 &
sleep 10
tail -f gold.log
```

Lo primero que hace es **medir la ventana** (un recorrido de los 82.9M
contando `clee` por corte), y la deja en el log y en `meta`. Esperado:

```
✔ ventana oficial: 9 cortes (2021-05-01 .. 2026-05-01)
```

Si da otro número, algo cambió en los datos y **todas las tasas de la tesis
cambian con él**: hay que entenderlo antes de seguir.

Al terminar corre once invariantes; si alguno falla, la corrida queda `FAILED`
en `meta` con el detalle y **la tabla se deja como está** para poder mirar las
filas que lo violan.

La foto de la cohorte:

```bash
docker compose exec postgres psql -U etl_rw -d mipymes -c \
"SELECT count(*) AS negocios,
        count(*) FILTER (WHERE cerrado)            AS cerrados,
        count(*) FILTER (WHERE huecos > 0)         AS volvieron_tras_ausencia,
        count(*) FILTER (WHERE local_reocupado)    AS con_local_reocupado,
        count(*) FILTER (WHERE cluster_sospechoso) AS sospechosos,
        round(avg(edad_anios), 1)                  AS edad_media_anios
 FROM gold.negocios_clee_oficial"
```

**Las dos consultas que deciden el umbral de cierre.** La primera dice cuánto
cuesta cada valor de `CIERRE_MESES_AUSENTE` (la regla es en meses: `cerrado ⟺
meses_sin_aparecer ≥ 12`); la segunda da el piso del error de la regla con
los propios datos, sin gastar una sola búsqueda con agentes (`huecos > 0` son
negocios que desaparecieron **y volvieron**):

```bash
docker compose exec postgres psql -U etl_rw -d mipymes -c \
"SELECT meses_sin_aparecer AS meses_ausente,
        cortes_tras_ultimo AS cortes_ausente,
        count(*) AS negocios,
        count(*) FILTER (WHERE cerrado) AS cerrados,
        round(100.0 * count(*) / sum(count(*)) OVER (), 2) AS pct
 FROM gold.negocios_clee_oficial GROUP BY 1, 2 ORDER BY 1, 2"

docker compose exec postgres psql -U etl_rw -d mipymes -c \
"SELECT count(*) FILTER (WHERE huecos > 0) AS volvieron,
        count(*) AS con_mas_de_un_corte,
        round(100.0 * count(*) FILTER (WHERE huecos > 0) / count(*), 2) AS pct
 FROM gold.negocios_clee_oficial WHERE cortes_observados > 1"
```

En la primera, fíjate en que un mismo `cortes_ausente` aparece con dos
`meses_ausente` distintos: son los intervalos irregulares de la ventana (6,
6, 6, **12**, 6, 6, 6, **12** meses), y es por lo que la regla cuenta meses y
no cortes.

Los valores distintos de un negocio salen de **Silver**, no de Gold — es el
insumo del LLM que unifica el nombre y de los agentes que lo buscan en línea:

```bash
docker compose exec postgres psql -U etl_rw -d mipymes -c \
"SELECT DISTINCT e.nom_estab, e.telefono, e.www
 FROM silver.establecimientos e
 WHERE e.clee = (SELECT clee FROM gold.negocios_clee_oficial
                 WHERE n_nombres > 1 AND NOT cluster_sospechoso LIMIT 1)
   AND e.nom_estab <> ''"
```

Los tamaños de los estratos de la muestra que se verifica en línea
(`verificacion_cierre.md` §4.2), que es lo que decide si las cuotas de 100
alcanzan y con qué peso se repondera cada uno:

```bash
docker compose exec postgres psql -U etl_rw -d mipymes -c \
"SELECT CASE WHEN cerrado AND local_reocupado THEN 'cerrado|reocupado'
             WHEN cerrado                     THEN 'cerrado|no_reocupado'
             WHEN huecos > 0                  THEN 'activo|intermitente'
             ELSE                                  'activo|continuo' END AS estrato,
        count(*) AS negocios
 FROM gold.negocios_clee_oficial
 WHERE NOT cluster_sospechoso AND nombre IS NOT NULL AND cve_geo_final IS NOT NULL
 GROUP BY 1 ORDER BY 1"
```

La muestra en sí **no** es un `ORDER BY random() LIMIT 20`: es una tirada
estratificada y reproducible que queda escrita en `verificacion.muestras`
con su semilla. El SQL completo está en `verificacion_cierre.md` §4.3.

Con qué ventana se construyó cada corrida, sin abrir un log. El `::jsonb` no
es adorno: `meta.job_runs.config` es `text` con JSON dentro, así que
`config->>'clave'` falla con *operator does not exist*:

```bash
docker compose exec postgres psql -U etl_rw -d mipymes -c \
"SELECT start_time, status,
        config::jsonb->>'n_cortes_oficiales' AS cortes,
        config::jsonb->'ventana_cortes'      AS ventana
 FROM meta.job_runs WHERE job_type = 'GOLD' ORDER BY start_time DESC LIMIT 5"
```

## 11. El agente de supervivencia

Con gold construido: la muestra, Ollama, y el agente sobre 1, 10 y los 400.
Diseño en `verificacion_cierre.md`; qué garantiza el código en `AGENTS.md`
N7.

### 11.1 Ollama, nativo y sin sudo

El instalador oficial pide `sudo`; el tarball no. El binario usa la 5090 con
el driver que ya está. **`OLLAMA_HOST=0.0.0.0` no es opcional**: el
contenedor del agente entra por `host.docker.internal`, y en `127.0.0.1` no
lo ve.

```bash
mkdir -p ~/ollama && cd ~/ollama
curl -L https://ollama.com/download/ollama-linux-amd64.tgz -o ollama-linux-amd64.tgz
tar -xzf ollama-linux-amd64.tgz
OLLAMA_HOST=0.0.0.0 OLLAMA_KEEP_ALIVE=1h nohup ~/ollama/bin/ollama serve > ~/ollama/serve.log 2>&1 &
sleep 5
~/ollama/bin/ollama pull qwen3:32b          # ~20 GB, una vez
~/ollama/bin/ollama list
nvidia-smi                                   # la GPU tiene que aparecer en serve.log al cargar
```

`OLLAMA_KEEP_ALIVE=1h` deja el modelo cargado entre negocios (el default
son 5 min, y recargar 20 GB cuesta medio minuto). El puerto 11434 queda
abierto en la red local de la workstation; si eso molesta, en vez de
`0.0.0.0` va la IP del puente de Docker (`docker network inspect bridge`).

Tras un reinicio hay que volver a lanzar el `serve`.

### 11.2 La muestra

```bash
cd ~/tesis-maestria-etl
docker compose run --rm agente muestrear --nombre piloto-2026-11
```

Esperado: cuatro estratos con 100 cada uno (o menos, con aviso, si la
población de alguno no alcanza) y la muestra en `verificacion.muestras`. Es
reproducible: `--semilla` default `MUESTRA_SEMILLA`.

### 11.3 Uno, diez, todos

```bash
docker compose run --rm agente verificar --muestra piloto-2026-11 --limite 1
```

Lo primero que hace es comprobar Ollama (si falta el modelo dice el `pull`)
y el buscador (`searxng` arranca solo con el servicio). El log por negocio
muestra cada búsqueda, cada página leída y el veredicto con su costo:

```
   [1/400] #1 14TAC0001 · 'TACOS PACO' · Guadalajara
      🔎 'TACOS PACO Guadalajara': 8 resultados
      📄 https://www.google.com/maps/... (3120 caracteres)
      ✔ cerrado/alta · 2 evidencias · 3 búsquedas · 5 vueltas · 2.1 min
```

Mira ese primer veredicto completo antes de seguir:

```bash
docker compose exec postgres psql -U etl_rw -d mipymes -c \
"SELECT v.clee, v.estado, v.confianza, v.n_busquedas, v.duracion_s, v.razonamiento
 FROM verificacion.veredictos v ORDER BY v.verificado_en DESC LIMIT 1" -c \
"SELECT e.fuente, e.senal, e.fecha_publicacion, e.url, left(e.extracto, 80) AS extracto
 FROM verificacion.evidencias e
 JOIN verificacion.veredictos v USING (veredicto_id)
 ORDER BY v.verificado_en DESC, e.capturada_en LIMIT 10"
```

Si el razonamiento cita fuentes que no están en las evidencias, o el estado
no cuadra con lo que las evidencias dicen, se ajusta el prompt
(`src/agents/survival/prompts.py`) y sube `AGENTE_VERSION`: los veredictos
viejos quedan con la versión vieja. Luego diez, y los 400 en segundo plano:

```bash
docker compose run --rm agente verificar --muestra piloto-2026-11 --limite 10
nohup docker compose run --rm agente verificar --muestra piloto-2026-11 > agente.log 2>&1 &
tail -f agente.log
```

Se reanuda solo: los pendientes son los que ESTE agente (`modelo/version`)
no ha verificado. Con 400 negocios a ~2 min cada uno son ~13 horas.

### 11.4 Las métricas

```bash
docker compose run --rm agente metricas --muestra piloto-2026-11
```

La tabla de confusión por estrato (VP/FP/FN/VN sobre `cerrado_gold`), la
tasa de indeterminados, y las dos cifras reponderadas por el tamaño real de
cada estrato en gold: la **precisión de `cerrado`** en la población, y la
fracción de `activos` del DENUE que ya cerraron. Con `--agente qwen3:32b/v1`
se filtra un modelo; para comparar dos, se corre `verificar` dos veces con
distinto `OLLAMA_MODEL` y `metricas` dos veces.

## Comandos sueltos

```bash
docker compose run --rm tools pytest -q
```

```bash
docker compose exec postgres psql -U etl_rw -d mipymes
```

```bash
docker compose run --rm tools alembic current --verbose
```

```bash
curl -s localhost:8000/api/v1/ops/health/pipeline | python3 -m json.tool
```

```bash
curl -s localhost:8000/api/v1/ops/runs | python3 -m json.tool
```

```bash
docker compose logs -f postgres
```

```bash
docker compose down
```

```bash
docker compose down -v
```
