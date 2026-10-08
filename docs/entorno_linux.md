# Entorno de ejecución — workstation Linux

Inventario levantado el **2026-10-07**. **El pipeline corre 100% aquí**
(D9 de `arquitectura.md`): es el único entorno de ejecución, las Mac quedan
fuera.

Toda la configuración derivada de aquí vive en `.env` (regla de
configuración del proyecto) y se resume en §6 de `arquitectura.md`.

---

## 1. Inventario

| | |
|---|---|
| **Distro** | Ubuntu 24.04.3 LTS, kernel 7.0.0-34-generic, x86_64 |
| **CPU** | AMD Ryzen Threadripper 7960X — **24 núcleos / 48 hilos**, 1 socket, 1 nodo NUMA, 5.36 GHz máx |
| **RAM** | **125 GiB** (121 GiB disponibles) + 8 GiB de swap |
| **Disco** | ext4 sobre LVM sobre **NVMe Kingston KC3000 2 TB** — 1.7 TB totales, **844 GB libres** |
| **GPU** | **NVIDIA RTX 5090**, 32 GB VRAM, driver 595.84 |
| **Docker** | 29.2.1 (cliente y servidor), Compose v5.0.2, driver `overlayfs`, **cgroup v2** |
| **Permisos** | El usuario está en el grupo `docker` → **no hace falta `sudo`** para contenedores |

### Lo que esto resuelve de un golpe

- **Sin VM.** Docker nativo sobre cgroup v2: los contenedores ven los 48
  hilos y los 125 GiB sin pedir nada, y el IO de `pgdata` es nativo.
- **`/var/lib/docker` está en `/`**, la partición de 1.7 TB con 844 GB
  libres. No hay que mover el `data-root` ni usar una ruta alterna: los
  volúmenes nombrados (donde vivirá `pgdata`) caen ahí con holgura.
- **ext4, no btrfs ni zfs.** No hay copy-on-write que desactivar para
  `pgdata` — era el riesgo que había que descartar.
- **NVMe**, no disco rotatorio: justifica `random_page_cost = 1.1` y
  `effective_io_concurrency` alto.
- **125 GiB de RAM contra una base de ~130 GiB.** Prácticamente todo el
  warehouse cabe entre `shared_buffers` y el page cache del kernel, así que
  el IO deja de ser el factor dominante tras la primera pasada.

---

## 2. Configuración de Postgres derivada

Calculada para 125 GiB de RAM, 24 núcleos físicos y NVMe. Va en el
`command:` del servicio `postgres` o en un `postgresql.conf` montado.

```conf
# ── Memoria ──────────────────────────────────────────────────────────
shared_buffers = 32GB                  # 25% de la RAM
effective_cache_size = 90GB            # lo que el kernel probablemente cachea
maintenance_work_mem = 16GB            # CREATE INDEX / VACUUM por partición
work_mem = 256MB                       # POR nodo de plan y POR conexión
temp_buffers = 256MB

# ── Paralelismo (24 núcleos físicos, 48 hilos) ───────────────────────
max_worker_processes = 48
max_parallel_workers = 24
max_parallel_workers_per_gather = 8
max_parallel_maintenance_workers = 8   # acelera CREATE INDEX

# ── NVMe ─────────────────────────────────────────────────────────────
random_page_cost = 1.1
effective_io_concurrency = 200
maintenance_io_concurrency = 200

# ── WAL: optimizado para cargas masivas ──────────────────────────────
wal_buffers = 64MB
max_wal_size = 16GB                    # menos checkpoints durante el COPY
min_wal_size = 2GB
checkpoint_completion_target = 0.9
checkpoint_timeout = 15min

# ── Autovacuum sobre la tabla grande ─────────────────────────────────
autovacuum_max_workers = 6
autovacuum_vacuum_cost_limit = 2000
default_statistics_target = 200        # 82.9M filas: mejores planes

# ── Huge pages: aprovecha si están, no falla si no ───────────────────
huge_pages = try
```

**Por sesión, solo durante las cargas masivas** (no global, porque relaja la
durabilidad):

```sql
SET synchronous_commit = off;
SET maintenance_work_mem = '32GB';     -- cabe: hay 125 GiB
SET work_mem = '2GB';
```

Nota sobre `work_mem`: es por **nodo de plan** y por **conexión**, no global.
Con 256 MB y, digamos, 20 conexiones del API ejecutando planes con 3 nodos de
orden, el techo teórico son ~15 GiB. Cabe, pero es la razón de no subirlo
globalmente a 2 GB y hacerlo por sesión donde haga falta.

---

## 3. Reparto de recursos entre contenedores

Con 48 hilos y 125 GiB **no hay escasez**. Los límites son generosos y
sirven de contención —que un proceso desbocado no tumbe la máquina— no de
racionamiento.

| Servicio | `cpus` | `mem_limit` | `shm_size` | Notas |
|---|---:|---:|---:|---|
| `postgres` | 32 | 48g | **1g** | El default de 64 MB rompe consultas paralelas grandes (§4) |
| `etl` / `scheduler` | 16 | 32g | 2g | `POLARS_MAX_THREADS=16`; los 2g son por Chromium en el scraping |
| `api` | 4 | 4g | — | I/O bound |
| `prometheus` + `grafana` + exporter | 2 | 4g | — | Scrape cada 15 s |

Suma: 54 de 48 hilos y 88 de 125 GiB. El oversubscribe de CPU es
intencional y sano — `etl` y `postgres` se alternan (mientras uno
transforma, el otro espera), y ningún servicio llega a su techo a la vez.

---

## 4. Qué pasa si no hay acceso root

**Nada que afecte al plan.** Lo único que de verdad hacía falta ya está
resuelto: el usuario pertenece al grupo `docker`, y con eso se controla todo
lo que importa — los parámetros de Postgres, los límites de cgroup por
servicio, el `shm_size`, los volúmenes. Los tres ajustes de sysctl que
siguen son optimizaciones marginales para **este** workload, no requisitos.

| Ajuste | Estado | Costo real de no cambiarlo |
|---|---|---|
| CPU governor | `powersave` con driver **`amd-pstate-epp`** (verificado 2026-10-07) | **Ninguno — asunto cerrado.** Con `amd-pstate-epp` el governor `powersave` no regula frecuencia por software: delega en el gestor de P-states del hardware (CPPC) y sostiene frecuencias altas bajo carga. No es el `powersave` penalizador de los `intel_pstate` viejos. Habría costado algo solo con `acpi-cpufreq`, que no es el caso. |
| `vm.swappiness = 60` | 60 | **Ninguno.** Con 125 GiB de RAM y un techo proyectado de ~88 GiB entre contenedores, nunca se toca la swap. `swappiness` solo actúa bajo presión de memoria, y aquí no la hay. Los `mem_limit` por servicio (que sí se pueden declarar sin root) ya acotan el crecimiento. |
| Huge pages | 0 | **Bajo.** `huge_pages = try` hace que Postgres use páginas normales sin fallar. El costo es algo de presión de TLB con `shared_buffers = 32GB` — unos pocos por ciento. Dato relevante: para Postgres se recomienda **desactivar** Transparent Huge Pages, no activarlas, así que `HugePages_Total = 0` no es el problema que parece. |

**Conclusión: los tres sysctl quedan descartados como pendientes.** El
governor por verificación directa del driver, la swap por tener 125 GiB, y
las hugepages porque `huge_pages = try` degrada limpiamente. No queda nada
que pedirle a un administrador.

Si alguna vez se quiere confirmar en vivo (sin root): durante una carga real,
`grep MHz /proc/cpuinfo | sort -u`. Con frecuencias sostenidas cerca del
máximo, no hay nada que ajustar.

### Lo que sí se controla sin root, y pesa mucho más

En orden de impacto real sobre este pipeline:

1. **Todo `postgresql.conf`** (§2) — vive dentro del contenedor. Es la
   palanca grande: `shared_buffers`, `maintenance_work_mem`,
   `max_parallel_workers`, `max_wal_size`.
2. **`shm_size` del servicio `postgres`.** Importante y fácil de pasar por
   alto: el default de Docker para `/dev/shm` es **64 MB**, y Postgres usa
   memoria compartida dinámica (`dynamic_shared_memory_type = posix`) para
   las consultas paralelas. Con 64 MB, un plan paralelo grande falla con
   `could not resize shared memory segment … No space left on device`.
   **Verificado en esta máquina el 2026-10-07**: el default efectivo es
   64.0M y `--shm-size=1g` lo sube a 1.0G sin root. Va como `shm_size: 1gb`
   en el servicio.
3. **Particionado, `COPY` binario, staging `UNLOGGED`, `CREATE INDEX
   CONCURRENTLY`** — decisiones de diseño del pipeline, donde está la
   mayor parte del rendimiento.
4. **`cpus` y `mem_limit` por servicio** (§3) — disponibles por estar en el
   grupo `docker`.
5. **`nice` / `ionice`** para bajar la prioridad del ETL si hace falta
   convivir con otros usuarios de la máquina. Bajar prioridad no requiere
   root; subirla sí.

**Conclusión operativa:** no gastes esfuerzo en conseguir root. Si algún día
lo hay, los tres sysctl son un extra de un dígito porcentual; el plan no los
asume en ningún punto.

---

## 5. La GPU

**RTX 5090, 32 GB VRAM, driver 595.84** (el inventario inicial la suponía una
5060; es bastante más).

Para el ETL **no participa**: Postgres no usa GPU, y Polars, rapidfuzz, H3 y
`COPY` son CPU puro (§2.3.3 de `arquitectura.md`). El `etl` no debe cargar
CUDA para hacer un `COPY`.

Donde sí entra es en `plan.md`: el scorer aprendido que cerraría la brecha de
97.72% → 98.70% proyectado, y los embeddings de nombres para los casos donde
el nombre difiere pero el negocio es el mismo. Con 32 GB de VRAM eso es
cómodo. Cuando se llegue a esa fase:

- Contenedor **separado** para entrenamiento, no el `etl`.
- `nvidia-container-toolkit` (hoy ausente) + `gpus: all` en el servicio.
- La 5090 es Blackwell **sm_120**; el driver 595.84 es suficientemente nuevo,
  pero las builds de PyTorch/XGBoost deben ser recientes (CUDA 12.8+) o no
  reconocen la arquitectura.

---

## 6. Un solo entorno

Decidido el 2026-10-07: **el pipeline corre 100% aquí.** Las Mac quedan fuera
de la ejecución; a lo sumo se edita código en ellas y se ejecuta por SSH o
tras un `git pull`.

Consecuencias de haber cerrado esto:

- **Nada de multi-arch.** Un solo target `amd64`: no hace falta
  `buildx --platform`, y `google-chrome-stable` del `Dockerfile.etl` funciona
  tal cual (cambiarlo por `chromium` queda como mejora opcional de
  mantenimiento, no como arreglo).
- **Límites de recursos sin parametrizar por máquina.** Los valores de §2 y
  §3 son *los* valores; siguen en `.env` por la regla de configuración del
  proyecto, pero no hay `docker-compose.override.yml` por entorno.
- **`pgdata` nunca se mueve.** No hay otro entorno al que portarlo, así que
  la incompatibilidad de `pgdata` entre arquitecturas no es un tema.
- **Los datos tienen que llegar aquí.** Bronze (9.1 GiB), `data/eval/`
  (584 MB) y `data/seeds/` viven hoy en la Mac y son el insumo de toda
  reconstrucción — es el primer punto de la Fase 0 en `arquitectura.md`.
- **Corridas largas con `nohup` o `systemd-run --user`**, no con
  `caffeinate`: la regla de macOS de `AGENTS.md` deja de aplicar, pero el
  motivo (una corrida de horas no debe morir con la sesión) sigue igual.
