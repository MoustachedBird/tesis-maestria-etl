# ETL Core — supervivencia de MiPyMEs

Pipeline de datos y API para el análisis de mortalidad y supervivencia de micro,
pequeñas y medianas empresas en México, sobre microdatos del DENUE (INEGI)
cruzados con incidencia delictiva municipal (SESNSP).

El diseño completo está en [`arquitectura.md`](arquitectura.md); las reglas
permanentes del repo, en [`AGENTS.md`](AGENTS.md).

## Cómo corre

**Todo en contenedor.** No hay Makefile ni ejecución nativa: `uv` y el entorno
virtual existen solo dentro de las imágenes, y `docker-compose.yml` es la única
interfaz. Lo único que hace falta en la máquina es Docker y git.

```bash
git clone <url> && cd etl-core
cp .env.example .env     # y poner POSTGRES_PASSWORD y POSTGRES_RO_PASSWORD

docker compose build
docker compose up -d     # Postgres → migraciones → API
```

`up` deja la base creada, migrada y sembrada: el servicio `migrate` espera el
healthcheck de Postgres, aplica Alembic y sale. Es idempotente, así que
re-levantar el stack no hace nada.

La API queda en `http://localhost:8000/docs`.

### Comandos

```bash
# ETL (one-shot)
docker compose run --rm etl extract                      # → Bronze
docker compose run --rm etl transform                    # Bronze → Silver
docker compose run --rm etl transform --only clee-radio  # aislar un paso
docker compose run --rm etl pipeline                     # todo

# Herramientas (one-shot)
docker compose run --rm tools pytest
docker compose run --rm tools python -m scripts.smoke_fase3
docker compose run --rm tools alembic current --verbose
docker compose run --rm tools ruff check src/ tests/ scripts/ alembic/

# Base de datos
docker compose exec postgres psql -U etl_rw -d mipymes
docker compose logs -f postgres
docker compose down          # conserva los datos
docker compose down -v       # BORRA el volumen
```

Los comandos globales (`extract`, `transform`, `load`, `pipeline`) corren
**todo** por default; se aíslan secciones con `--only` o `--no-*`. El
inventario completo de flags está en `AGENTS.md`.

## Stack

| | |
|---|---|
| Lenguaje | Python 3.12 |
| Almacenamiento | PostgreSQL 17 — una base, tres schemas (`silver`, `gold`, `meta`) |
| Driver | psycopg3; escrituras masivas con `COPY ... FROM STDIN` |
| DataFrames | Polars |
| API | FastAPI |
| Migraciones | Alembic (multi-schema) |
| Orquestación | Docker Compose |

`silver.establecimientos` está particionada por `period`: un corte del DENUE es
una partición, lo que hace del periodo la unidad de recuperación — un corte mal
cargado se recarga sin tocar los demás.

## Imágenes

Dos targets desde un solo `Dockerfile.etl`:

| Target | Lleva | Para qué | Arquitectura |
|---|---|---|---|
| `tools` | deps + pytest/ruff | migraciones, tests, scripts, API | portable (arm64 y amd64) |
| `etl` | lo anterior + Chrome | el pipeline | amd64 |

Chrome solo está en `etl` porque lo usa el scraping de DENUE (Selenium, dos
archivos). Así los tests y las migraciones no cargan un navegador de ~500 MB
que no usan, y `tools` construye en cualquier arquitectura.

## Estructura

```
src/
├── shared/      # config, logging, enums, capa de BD (Base, sesión, modelos,
│                #   copy, staging, partitions)
├── etl/         # extractores, transformadores, loaders, CLI
├── meta/        # linaje y metadatos de corridas
└── api/         # FastAPI
alembic/         # migraciones (baseline único sobre los 3 schemas)
scripts/         # evaluación del matching CLEE, smoke tests
tests/
data/seeds/      # catálogos versionados en git (9 parquet)
```

Bronze, Silver y Gold no están en el repo: Bronze se re-descarga con
`extract`, y Silver y Gold viven en Postgres.

## Datos

Los microdatos no se versionan. `data/` entra por bind mount y solo
`data/seeds/` está en git — son los catálogos que siembra la migración.
