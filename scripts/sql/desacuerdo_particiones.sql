-- ¿Coinciden las particiones de identidad de radio y H3?
--
-- `clee_propagation_stats.py` compara VALORES de identidad fila a fila. Esta
-- consulta compara las PARTICIONES: si un cluster de radio se corresponde
-- con mas de uno de h3, radio unio lo que h3 separo. Es la medicion que
-- decide si el reconciliador LLM de `normalizacion_gold.md` §1 se construye.
--
-- Uso (desde la raiz del repo, redirigiendo desde el HOST para que el SQL no
-- pase por el portapapeles):
--
--   docker compose exec -T postgres psql -U etl_rw -d mipymes \
--     < scripts/sql/desacuerdo_particiones.sql 2>&1 | tee desacuerdo.log
--
-- Solo lectura. La temporal muere con la sesion.

\timing on

-- SIN PARALELISMO, a proposito.
--
-- La memoria compartida que pide una consulta paralela es aproximadamente
-- `max_parallel_workers_per_gather x work_mem`, y tiene que caber en el
-- /dev/shm del contenedor (`PG_SHM_SIZE`). Con 8 workers y los 256 MB que
-- el servidor trae por default eso ya son 2 GB, y una maquina con 1 GB de
-- shm muere con "could not resize shared memory segment ... No space left
-- on device" -- medido en indominus el 2026-11-07.
--
-- En 0 workers el problema no existe: `work_mem` pasa a ser memoria PRIVADA
-- del backend y puede ser grande sin tocar la shm. Esta consulta se corre
-- una vez, asi que la robustez vale mas que la velocidad.
--
-- Si /dev/shm esta en 4 GB o mas (`docker compose exec postgres df -h
-- /dev/shm`), subir a `max_parallel_workers_per_gather = 4` y
-- `work_mem = '512MB'` la acelera varias veces.
SET max_parallel_workers_per_gather = 0;
SET work_mem = '2GB';

-- ---------------------------------------------------------------------------
-- La pasada cara, una sola vez. El join es partition-wise --las dos tablas
-- estan particionadas por `period` y lo llevan en la PK-- asi que son 20
-- joins de ~4M y no uno de 82.9M x 82.9M.
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE pares AS
SELECT r.clee_resuelto AS c_radio,
       h.clee_resuelto AS c_h3,
       count(*)        AS obs
FROM silver.clee_radio r
JOIN silver.clee_h3 h USING (id, period)
GROUP BY 1, 2;

CREATE INDEX ON pares (c_radio);
CREATE INDEX ON pares (c_h3);
ANALYZE pares;

\echo ''
\echo '===== VEREDICTO ====='
\echo ''

-- `count(DISTINCT c_radio)` y no `count(*)`: un cluster de radio partido en
-- tres de h3 aporta TRES filas a `pares`, y contar filas lo contaria tres
-- veces.
--
-- `obs_comparadas` tiene que dar 82,896,801. Si da menos, el JOIN interno se
-- esta comiendo observaciones que estan en una tabla y no en la otra --que
-- tambien son desacuerdo, solo que invisible-- y eso se arregla antes de
-- interpretar el resto.
WITH grados AS (
    SELECT c_radio, c_h3, obs,
           count(*) OVER (PARTITION BY c_radio) AS h3_por_radio,
           count(*) OVER (PARTITION BY c_h3)    AS radio_por_h3
    FROM pares
)
SELECT
    sum(obs)                                                 AS obs_comparadas,
    count(DISTINCT c_radio)                                  AS clusters_radio,
    count(DISTINCT c_h3)                                     AS clusters_h3,
    count(DISTINCT c_radio) FILTER (WHERE h3_por_radio > 1)  AS radio_unio_mas,
    count(DISTINCT c_h3)    FILTER (WHERE radio_por_h3 > 1)  AS h3_unio_mas,
    sum(obs) FILTER (WHERE h3_por_radio > 1
                        OR radio_por_h3 > 1)                 AS obs_en_disputa,
    round(100.0 * sum(obs) FILTER (WHERE h3_por_radio > 1
                                      OR radio_por_h3 > 1)
          / nullif(sum(obs), 0), 4)                          AS pct_disputa
FROM grados;

\echo ''
\echo '===== CUANTO SE PARTEN LOS CLUSTERS QUE NO COINCIDEN ====='
\echo ''

-- La forma del desacuerdo: casi todo en 2 suele ser una frontera de
-- blocking; colas largas son fusiones indebidas de un metodo.
SELECT h3_por_radio AS clusters_h3_por_cluster_radio,
       count(*)     AS cuantos_clusters_radio,
       sum(obs)     AS observaciones
FROM (
    SELECT c_radio, count(*) AS h3_por_radio, sum(obs) AS obs
    FROM pares GROUP BY 1
) t
GROUP BY 1 ORDER BY 1;

\echo ''
\echo '===== LOS 10 CLUSTERS EN DISPUTA MAS GRANDES, CON SUS NOMBRES ====='
\echo ''

-- Esto es lo que convierte el numero en veredicto: si sale el patron del
-- ejemplo --varios nombres de la misma taqueria y algo que no pertenece-- el
-- reconciliador tiene caso concreto. Si sale vacio, no lo tiene.
WITH disputados AS (
    SELECT c_radio FROM pares GROUP BY 1 HAVING count(*) > 1
    ORDER BY sum(obs) DESC LIMIT 10
)
SELECT r.clee_resuelto AS c_radio,
       h.clee_resuelto AS c_h3,
       e.period, e.nom_estab, e.codigo_act, e.latitud, e.longitud
FROM silver.clee_radio r
JOIN silver.clee_h3 h USING (id, period)
JOIN silver.establecimientos e ON e.id = r.id AND e.period = r.period
WHERE r.clee_resuelto IN (SELECT c_radio FROM disputados)
ORDER BY r.clee_resuelto, e.period;
