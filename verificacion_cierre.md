# Verificación del cierre — la muestra, el agente y el schema `verificacion`

**Estado:** implementado. El schema (`b0010`), la regla de cierre en `gold`,
y el agente en `src/agents/survival` con su CLI (`muestrear`, `verificar`,
`metricas`; `AGENTS.md` N7, `ARRANQUE.md` §11). El modelo corre en Ollama
sobre la RTX 5090 (`qwen3:32b` por default). Lo que sigue aquí es el diseño
que el código implementa; el SQL de §4.3 y §6 es el que ejecutan
`muestreo.py` y `metricas.py`. Escrito el 2026-11-11, actualizado el
2026-11-14.

**La pregunta, en una línea:** cuando un negocio deja de aparecer en el
DENUE, ¿cerró? `gold.negocios_clee_oficial.cerrado` lo afirma por regla; este
documento describe cómo se mide si esa afirmación se sostiene, buscando una
muestra de negocios en línea con un agente y guardando lo que encontró.

---

## 1. La hipótesis: cómo `gold` decide `cerrado`

```
cerrado  ⟺  meses_sin_aparecer ≥ CIERRE_MESES_AUSENTE      (default 12)
```

donde `meses_sin_aparecer` es la distancia de calendario entre el último
corte oficial en que el negocio apareció y el último corte de la ventana
(hoy 2026-05).

**Por qué en meses y no en cortes.** Los cortes oficiales no son
equidistantes: 2021-05, 2021-11, 2022-05, 2022-11, **2023-11**, 2024-05,
2024-11, 2025-05, **2026-05** — intervalos de 6, 6, 6, 12, 6, 6, 6, 12
meses. "Un corte ausente" significa 12 meses de evidencia para quien
desapareció en 2022-11 y 6 para quien desapareció en 2024-05. La regla
cuenta tiempo, que es lo comparable.

**Por qué 12.** Es la regla preliminar del protocolo de la tesis ("se
considerará cerrado el establecimiento que deja de aparecer en dos cortes
consecutivos") expresada en la cadencia semestral del DENUE. Bajarlo detecta
más cierres con más falsos positivos; subirlo exige más evidencia a cambio
de perder los cierres recientes por censura. **Es calibración** y vive en
`.env` (`CIERRE_MESES_AUSENTE`), queda en `version_reglas` de cada fila y en
`meta.job_runs.config` de cada corrida.

Tres lecturas de una fila de `gold`:

| Lectura | Condición | Qué significa |
|---|---|---|
| **activo** | `cortes_tras_ultimo = 0` | Presente en el último corte: censurado por la derecha |
| **ausente reciente** | `cortes_tras_ultimo > 0 AND NOT cerrado` | Lleva menos meses ausente que el umbral: no se afirma nada todavía. Con la ventana de hoy está vacío (el último intervalo son 12 meses); aparece en cuanto entre un corte a 6 meses |
| **cerrado** | `cerrado` | La hipótesis que se verifica |

**Lo que la regla no sabe, y la tabla conserva para medirlo:**

- `huecos > 0`: el negocio **ya desapareció y volvió**. Son casos probados,
  con los propios datos, de que ausencia ≠ cierre. Es el piso del error de la
  regla, gratis:

  ```sql
  SELECT count(*) FILTER (WHERE huecos > 0) AS volvieron, count(*) AS total
  FROM gold.negocios_clee_oficial WHERE cortes_observados > 1;
  ```

- `local_reocupado`: otro CLEE apareció en la misma celda H3 res 9 (~174 m)
  en un corte posterior. Corrobora, no prueba. Solo se calcula para los
  cerrados.
- `cluster_sospechoso`: más nombres que cortes, o sea una identidad que
  agrupó más de un negocio. **No entra a la muestra**: no hay un negocio
  que buscar.

---

## 2. Alcance: primero la cohorte oficial, después lo demás

La primera tanda es **`gold.negocios_clee_oficial` completa**: todos los
negocios con CLEE de INEGI (9 cortes, 2021-05 → 2026-05, ~9.58M). Ahí la
identidad es observada, no inferida, así que un veredicto del agente mide
**solo** la regla de cierre y no se confunde con el error de propagación.

Si la verificación resulta fructífera, lo que sigue es otra tabla y otra
muestra —nunca una columna `cohorte`—: `gold.negocios_clee_propagado` sobre
los 20 cortes con identidad resuelta, donde el agente mediría dos cosas a la
vez (la regla y el matching) y el diseño de la muestra tendría que
separarlas. Eso se decide con los números de la primera tanda en la mano.

---

## 3. El schema `verificacion`

Cuatro tablas, del diseño al detalle (`src/shared/db/models/verificacion.py`):

```
verificacion.muestras           una tirada: tamaño, semilla, estratos, filtro, y
                                las reglas de gold con que se sacó
verificacion.muestra_negocios   los negocios que salieron, con cerrado_gold,
                                meses_sin_aparecer_gold y huecos_gold CONGELADOS
verificacion.veredictos         activo | cerrado | indeterminado, confianza
                                alta | media | baja, razonamiento, agente
verificacion.evidencias         una fila por pieza: fuente, url, fecha,
                                extracto citado, señal cerrado | activo | neutral
```

**Por qué un schema y no tablas en `gold`.** Todo lo de `gold` es derivable
y se trunca con `--force`. Un veredicto no: costó búsquedas y tiempo de
agente, y rehacerlo no daría lo mismo porque la web cambia. Tiene que
sobrevivir a cualquier reconstrucción de `gold`, y la forma estructural de
decirlo es un schema aparte **sin FK hacia `gold`** (una FK impediría truncar
la cohorte mientras exista un veredicto). El join es lógico, por `clee`. Lo
escribe además **otro actor** —el agente, no el ETL—, igual que cada
`silver.clee_<metodo>` la escribe solo su método.

**La hipótesis se congela en `muestra_negocios`.** No contradice "las tablas
derivadas no guardan snapshots" (`b0007`, `b0010gold`): el valor que estaba
en `gold` al muestrear **es el objeto de estudio**. Si `gold` se reconstruye
con otro umbral, el veredicto sigue siendo sobre el negocio tal como se le
presentó al agente; la nueva versión de la regla se verifica con una muestra
nueva. `muestras.nombre` es único por eso: dos tiradas son dos filas.

**El vocabulario es cerrado y sale de enums** (`src/shared/enums/verificacion.py`):
los `CHECK` de `estado`, `confianza` y `senal` se generan de
`EstadoVerificado`, `Confianza` y `SenalEvidencia`. Un agente que escriba
`closed` o `CERRADO ` falla el `INSERT` en vez de romper el conteo en
silencio.

**`indeterminado` es un resultado legítimo.** El negocio no deja huella en
línea, o la que deja no decide. Forzarlo a `activo` o `cerrado` contaminaría
la medida con adivinanzas, y la proporción de indeterminados es un dato por
sí misma: dice qué tan verificable es la cohorte (y, por estrato, dónde).

**`api_ro` lee el schema**, igual que `gold`: los veredictos son datos de
consumo.

---

## 4. La muestra

### 4.1 Población

`gold.negocios_clee_oficial` con este filtro, que queda grabado en
`muestras.filtro` porque **define a qué población se extrapola**:

```sql
NOT cluster_sospechoso          -- no hay UN negocio que buscar
AND nombre IS NOT NULL          -- sin nombre no se puede buscar
AND cve_geo_final IS NOT NULL   -- sin municipio tampoco
```

### 4.2 Estratos y cuotas

La muestra **no es aleatoria simple**: los cerrados son minoría y la regla
es más frágil en subgrupos concretos, así que se estratifica y las tasas se
reponderan (§6). Cuatro estratos, 100 cada uno:

| Estrato | Condición | Qué mide |
|---|---|---|
| `cerrado\|reocupado` | `cerrado AND local_reocupado` | La hipótesis con evidencia corroborante: debería ser donde más acierta |
| `cerrado\|no_reocupado` | `cerrado AND NOT coalesce(local_reocupado, false)` | La hipótesis sola |
| `activo\|intermitente` | `NOT cerrado AND huecos > 0` | Los que ya desaparecieron y volvieron: donde "ausente ⇒ cerrado" ya falló una vez |
| `activo\|continuo` | `NOT cerrado AND huecos = 0` | Control: ¿cuántos "activos" del DENUE están en realidad cerrados? (el lado de los falsos negativos) |

**Tamaño.** 400 en total. Para la proporción de aciertos dentro de un
estrato, 100 da un intervalo de ±10 puntos al 95%; para la precisión global
de `cerrado` (los dos estratos cerrados juntos, 200) son ±7. Es un piloto:
si los indeterminados pasan del 30% en un estrato, ese estrato se amplía
antes de seguir. `huecos_gold` y `meses_sin_aparecer_gold` quedan en cada
fila, así que cualquier otro corte del análisis (por antigüedad de la
ausencia, por intermitencia dentro de los cerrados) se hace después sin
volver a muestrear.

### 4.3 La tirada, reproducible

Una sola transacción. `setseed` fija la secuencia de `random()`; **sin
paralelismo** porque con workers en paralelo la secuencia deja de ser
reproducible. El `ORDER BY random()` sobre ~9.58M filas es un sort de un
minuto: es una operación de una vez por muestra, no una consulta.

```sql
BEGIN;
SET LOCAL max_parallel_workers_per_gather = 0;
SELECT setseed(0.42);

INSERT INTO verificacion.muestras
    (muestra_id, nombre, semilla, tamano_pedido, n_negocios, estratos, filtro,
     version_reglas, ventana_cortes, cierre_meses_ausente)
SELECT gen_random_uuid()::text, 'piloto-2026-11', 0.42, 400, 0,
       '{"cerrado|reocupado": 100, "cerrado|no_reocupado": 100,
         "activo|intermitente": 100, "activo|continuo": 100}'::jsonb,
       'NOT cluster_sospechoso AND nombre IS NOT NULL AND cve_geo_final IS NOT NULL',
       version_reglas, ventana_cortes, 12
FROM gold.negocios_clee_oficial LIMIT 1;

WITH poblacion AS (
    SELECT clee, cerrado, meses_sin_aparecer, huecos,
           CASE
             WHEN cerrado AND local_reocupado THEN 'cerrado|reocupado'
             WHEN cerrado                     THEN 'cerrado|no_reocupado'
             WHEN huecos > 0                  THEN 'activo|intermitente'
             ELSE                                  'activo|continuo'
           END AS estrato
    FROM gold.negocios_clee_oficial
    WHERE NOT cluster_sospechoso AND nombre IS NOT NULL AND cve_geo_final IS NOT NULL
),
numerada AS (
    SELECT p.*, row_number() OVER (PARTITION BY estrato ORDER BY random()) AS rn
    FROM poblacion p
)
INSERT INTO verificacion.muestra_negocios
    (muestra_id, clee, orden, estrato, cerrado_gold, meses_sin_aparecer_gold, huecos_gold)
SELECT m.muestra_id, n.clee,
       row_number() OVER (ORDER BY n.estrato, n.rn),
       n.estrato, n.cerrado, n.meses_sin_aparecer, n.huecos
FROM numerada n
CROSS JOIN verificacion.muestras m
WHERE m.nombre = 'piloto-2026-11' AND n.rn <= 100;

UPDATE verificacion.muestras m
SET n_negocios = (SELECT count(*) FROM verificacion.muestra_negocios WHERE muestra_id = m.muestra_id)
WHERE nombre = 'piloto-2026-11';
COMMIT;
```

`version_reglas` y `ventana_cortes` se copian de la propia tabla: son
iguales en todas sus filas por construcción (invariante de `gold`).

Es lo que ejecuta `docker compose run --rm agente muestrear --nombre ...`
(`src/agents/survival/muestreo.py`), con la cuota y la semilla en `.env` y
el registro en `meta`. Vive con el agente y no en `src/etl/` porque
muestrear → verificar → métricas es un solo flujo y gold es su insumo.

---

## 5. El agente

### 5.1 Lo que recibe por negocio

Todo lo que el DENUE publicó de él, en todos los cortes. Los valores
distintos salen de **Silver** (no hay tabla de variantes en `gold`):

```sql
WITH g AS (
    SELECT g.clee, g.nombre, g.codigo_act, g.cve_geo_final,
           g.lat_final, g.lon_final, g.primer_periodo, g.ultimo_periodo,
           g.periodos, mn.estrato, mn.orden
    FROM verificacion.muestra_negocios mn
    JOIN gold.negocios_clee_oficial g USING (clee)
    WHERE mn.muestra_id = :muestra_id AND mn.clee = :clee
)
SELECT g.*,
       (SELECT array_agg(DISTINCT e.nom_estab)  FILTER (WHERE e.nom_estab  <> '') FROM silver.establecimientos e WHERE e.clee = g.clee) AS nombres,
       (SELECT array_agg(DISTINCT e.raz_social) FILTER (WHERE e.raz_social <> '') FROM silver.establecimientos e WHERE e.clee = g.clee) AS razones_sociales,
       (SELECT array_agg(DISTINCT e.telefono)   FILTER (WHERE e.telefono   <> '') FROM silver.establecimientos e WHERE e.clee = g.clee) AS telefonos,
       (SELECT array_agg(DISTINCT e.www)        FILTER (WHERE e.www        <> '') FROM silver.establecimientos e WHERE e.clee = g.clee) AS sitios,
       (SELECT array_agg(DISTINCT concat_ws(' ', e.nom_vial, e.numero_ext, e.nomb_asent, e.cod_postal))
          FROM silver.establecimientos e WHERE e.clee = g.clee) AS domicilios,
       (SELECT a.nombre_act FROM silver.denue_actividades_economicas a WHERE a.codigo_act = g.codigo_act) AS actividad,
       (SELECT m.municipio FROM silver.denue_municipios m
         WHERE m.cve_ent = LEFT(g.cve_geo_final, 2) AND m.cve_mun = SUBSTR(g.cve_geo_final, 3, 3)) AS municipio
FROM g;
```

**El agente NO recibe `cerrado_gold`.** Si supiera la hipótesis la
confirmaría: el veredicto tiene que ser ciego a lo que se está midiendo.
Recibe `ultimo_periodo` porque sí lo necesita ("hay actividad después de
esa fecha?"), y es un dato del DENUE, no de la regla.

### 5.2 Protocolo de búsqueda

En este orden, y se detiene cuando tiene evidencia suficiente para una
confianza `alta`:

1. **Google Maps / Google Business**: nombre + municipio, y nombre + domicilio.
   Es la fuente con estado explícito ("Cerrado permanentemente", "Cerrado
   temporalmente", horarios vigentes) y con fecha de la última reseña.
2. **Redes sociales** (Facebook, Instagram): página del negocio, fecha de la
   última publicación, avisos de cierre o de cambio de domicilio.
3. **Sitio web** (`www` del DENUE): si responde, qué dice; si no, cuándo dejó
   de responder (Wayback Machine).
4. **Directorios y marketplaces** (Sección Amarilla, Cylex, Mercado Libre,
   Didi/Uber Eats/Rappi para restaurantes): fichas con fecha.
5. **Noticias locales**: cierres, incendios, clausuras.
6. **El local**: Street View con fecha de captura, si el punto es preciso.

Un negocio **homónimo en otro domicilio no es evidencia**: la identidad es
nombre + lugar. Cadenas (`nom_cencom`, OXXO, farmacias) exigen el domicilio
exacto; de lo contrario, `indeterminado`.

### 5.3 Reglas de decisión

| Estado | Confianza | Cuándo |
|---|---|---|
| `cerrado` | alta | Estado explícito de cierre en una fuente con fecha (Maps "cerrado permanentemente", aviso propio), o el local con otro ocupante verificable |
| `cerrado` | media | Ninguna señal de actividad después de `ultimo_periodo` en ≥2 fuentes que sí tenían actividad antes |
| `cerrado` | baja | Solo silencio: no aparece en ningún lado, pero tampoco aparecía antes |
| `activo` | alta | Actividad fechada **posterior al último corte de la ventana** (reseña, publicación, horario vigente, pedido en marketplace) |
| `activo` | media | Actividad fechada posterior a `ultimo_periodo` pero anterior al último corte de la ventana |
| `activo` | baja | Ficha vigente sin fecha |
| `indeterminado` | — | Sin huella, huella ambigua, homónimos imposibles de separar, o cadena sin domicilio exacto |

`fecha_cierre_estimada` se llena cuando la evidencia la da (último rastro de
actividad, aviso). Comparada con `gold.ultimo_periodo` dice si el DENUE lo
dejó de listar **antes o después** de que cerrara, que es un dato sobre la
latencia del directorio y no solo sobre la regla.

### 5.4 Lo que escribe

Un `veredictos` por negocio y pasada, y **una `evidencias` por pieza** con
su `senal` propia. Las piezas contradictorias se guardan las dos: lo que
vuelve auditable al agente es ver cómo las pesó en `razonamiento`.
`n_busquedas` y `duracion_s` son el costo del veredicto, para proyectar lo
que costaría una muestra más grande.

El agente **no** toca `gold`, **no** decide el umbral y **no** reescribe un
veredicto anterior: una segunda pasada es otra fila con otro `agente`.

---

## 6. Las métricas

Todo sale de un join, por estrato, sobre el veredicto más reciente de cada
negocio:

```sql
WITH ultimo AS (
    SELECT DISTINCT ON (muestra_id, clee) muestra_id, clee, estado, confianza
    FROM verificacion.veredictos
    ORDER BY muestra_id, clee, verificado_en DESC
)
SELECT mn.estrato,
       count(*)                                                    AS n,
       count(*) FILTER (WHERE u.estado = 'indeterminado')          AS indeterminados,
       count(*) FILTER (WHERE mn.cerrado_gold     AND u.estado = 'cerrado') AS vp,
       count(*) FILTER (WHERE mn.cerrado_gold     AND u.estado = 'activo')  AS fp,
       count(*) FILTER (WHERE NOT mn.cerrado_gold AND u.estado = 'cerrado') AS fn,
       count(*) FILTER (WHERE NOT mn.cerrado_gold AND u.estado = 'activo')  AS vn
FROM verificacion.muestra_negocios mn
JOIN verificacion.muestras m USING (muestra_id)
LEFT JOIN ultimo u USING (muestra_id, clee)
WHERE m.nombre = 'piloto-2026-11'
GROUP BY mn.estrato ORDER BY mn.estrato;
```

- **Precisión de `cerrado`** = vp / (vp + fp), sobre los determinados. Es
  la pregunta de la tesis. Se reporta por estrato y global **reponderada**
  por el tamaño real de cada estrato en la población (la muestra los
  igualó a 100; la población no).
- **Tasa de falsos negativos** = fn / (fn + vn) en los activos: cuántos
  "presentes en el DENUE" ya cerraron. Acota la latencia del directorio.
- **Tasa de indeterminados** por estrato: la verificabilidad. Si es alta en
  `cerrado|no_reocupado` y baja en `cerrado|reocupado`, la propia
  verificabilidad está correlacionada con el cierre y hay que decirlo.
- **Latencia**: distribución de `fecha_cierre_estimada - ultimo_periodo`.
- **Por `huecos_gold` y `meses_sin_aparecer_gold`** dentro de los cerrados:
  si la precisión sube con los meses, el umbral de 12 se puede defender o
  mover con datos.

---

## 7. Orden de construcción

1. **La tanda de `gold`** sobre la cohorte oficial, con la regla en meses
   (`ARRANQUE.md` §10). Reporta la foto: cuántos cerrados, cuántos
   intermitentes, cuántos con local reocupado.
2. **El muestreo**: `agente muestrear` (§4.3, implementado).
3. **El agente**: `agente verificar --limite 1`, luego `--limite 10`, luego
   los 400 (`ARRANQUE.md` §11). Con los diez se ajusta el prompt de §5.2 y
   sube `AGENTE_VERSION` antes de gastar en los 400.
4. **Las métricas** de §6 y la decisión: si la precisión de `cerrado` es
   defendible, la regla pasa al capítulo de método con su intervalo; si no,
   se mueve el umbral o se agregan señales (local reocupado como condición,
   no como estrato) y se verifica otra muestra.
5. **Solo entonces** la cohorte propagada (§2).
