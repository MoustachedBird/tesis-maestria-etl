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

Esperado (la cadena son ocho revisiones; `b0000` crea y las demás ajustan):

```
   Schemas creados: silver, gold, meta
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
```

Al final: **15 tablas en `silver`** (11 del baseline + 2 de CLEE + 2 de
permanencia), **5 en `meta`** (más `alembic_version`, que es de Alembic) y
`gold` vacío: todavía no hay tabla que promover.

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

## 10. Gold: la cohorte con CLEE oficial (2021+)

La capa de consumo: una fila por negocio de los cortes en que INEGI publica
el CLEE (9 cortes, 2021-05 → 2026-05). **No depende de la resolución CLEE**
—lee el `clee` de los propios hechos— así que se puede correr sin haber
terminado las propagaciones.

```bash
cd ~/tesis-maestria-etl
nohup docker compose run --rm etl \
  transform --only gold > gold.log 2>&1 &
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

Al terminar corre siete invariantes; si alguno falla, la corrida queda
`FAILED` en `meta` con el detalle y **la tabla se deja como está** para poder
mirar las filas que lo violan.

```bash
docker compose exec postgres psql -U etl_rw -d mipymes -c \
"SELECT count(*) AS negocios,
        count(*) FILTER (WHERE cluster_sospechoso) AS sospechosos,
        count(*) FILTER (WHERE nombre IS NULL)     AS sin_nombre,
        count(*) FILTER (WHERE codigo_act IS NULL AND tiene_actividad) AS scian_cruza_rama,
        count(*) FILTER (WHERE cortes_tras_ultimo = 0) AS presentes_al_final
 FROM gold.negocios_clee_oficial"
```

El canónico y sus alternativas, para un negocio cualquiera:

```bash
docker compose exec postgres psql -U etl_rw -d mipymes -c \
"SELECT campo, valor, n_cortes, primer_periodo, ultimo_periodo, es_canonico
 FROM gold.negocios_clee_oficial_variantes
 WHERE clee = (SELECT clee FROM gold.negocios_clee_oficial
               WHERE n_nombres > 1 AND NOT cluster_sospechoso LIMIT 1)
 ORDER BY campo, n_cortes DESC"
```

Búsqueda difusa, que es para lo que existe el índice trigram — "¿qué negocio
es *tacos paco*?":

```bash
docker compose exec postgres psql -U etl_rw -d mipymes -c \
"SELECT clee, valor, round(similarity(valor, 'TACOS PACO')::numeric, 3) AS sim
 FROM gold.negocios_clee_oficial_variantes
 WHERE campo = 'nom_estab' AND valor % 'TACOS PACO'
 ORDER BY sim DESC LIMIT 10"
```

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
