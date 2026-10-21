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
  'rm -rf logs/downloads data/bronze extract.log ingest.log clee.log htmlcov coverage.xml'
git pull
docker compose --profile manual --profile tools build
docker compose up -d
docker compose logs migrate --no-log-prefix | tail -20
```

`--profile manual --profile tools` no es opcional: `docker compose build`
sin ellos salta los servicios que viven bajo profile, y `etl` es uno. Se
queda con la imagen vieja sin avisar.

Lo que escribieron corridas anteriores quedó con dueño root. El contenedor
de arriba también es root, así que borra sin `sudo`.

Esperado: `silver: 12 tablas`, `meta: 6 tablas`, y las cuatro líneas de siembra.

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

## 7. Resolución CLEE

Es la etapa más larga.

```bash
cd ~/tesis-maestria-etl
nohup docker compose run --rm etl transform --only clee-radio > clee.log 2>&1 &
sleep 10
tail -f clee.log
```

```bash
docker compose run --rm tools python scripts/clee_propagation_stats.py
```

## 8. Evaluación del matching

```bash
cd ~/tesis-maestria-etl
docker compose run --rm tools python scripts/evaluate_masked_clee.py extract
docker compose run --rm tools python scripts/evaluate_masked_clee.py evaluate
```

Esperado: 97.72% F1.

## 9. Dimensión N3

```bash
cd ~/tesis-maestria-etl
docker compose run --rm etl build-entidades
docker compose exec postgres psql -U etl_rw -d mipymes -c \
"SELECT COUNT(*) AS negocios,
        COUNT(*) FILTER (WHERE es_sintetica) AS sinteticos
 FROM silver.entidades_economicas"
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
