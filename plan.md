# Plan — resolución de identidad CLEE y motor de almacenamiento

Documento de trabajo abierto el **2026-10-06**. Qué se hizo, qué se decidió
y en qué orden sigue. Los hallazgos crudos con sus números viven en
`docs/findings.md` (entrada 2026-10-06); la explicación integrada del
algoritmo en `docs/clee_propagation.md`. Aquí solo va el plan.

---

## 1. Qué se hizo hoy (cerrado)

**Corrección del loop de candidatos: `argmax` en vez de primer match.**

El loop evaluaba vecinos ordenados por distancia y hacía `break` en el
primero que pasaba el umbral de nombre, sin comparar scores. Un vecino a
3 m con ratio 81 le ganaba al registro verdadero a 10 m con ratio 100.

| | Primer match | `argmax` |
|---|---|---|
| Precision / recall / F1 | 95.90% | **97.72%** |
| `falso_match` | 37,601 | 20,619 |
| `falso_nuevo` / `match_espurio` / `nuevo_correcto` | 262 / 363 / 152 | **idénticos** |

16,984 filas arregladas, 2 regresiones. Que las otras tres categorías no
se muevan es la prueba de control: `argmax` cambia contra *quién* matchea
una fila, nunca *si* matchea.

Archivos: `src_etl/utils/matching.py` (score con tiers),
`src_etl/utils/entity_resolution.py` (`_best_candidate`, helper único que
reemplaza el loop duplicado), `tests/unit/test_best_candidate.py` (13
tests). Suite completa: 217 passed. Baseline del eval respaldado en
`data/eval/fase7_masked_clee/baseline_firstmatch_2026-07-21/`.

**Sin commitear. Silver sin tocar.**

---

## 2. Dónde está el error que queda

Error total tras `argmax`: **2.31%** de las 920,699 filas de la muestra.
De los 20,579 `falso_match` sobrevivientes con ambos registros
localizables:

| | Filas | % | Quién lo puede arreglar |
|---|---:|---:|---|
| Features idénticas (gemelos reales) | 11,577 | 56.3% | **Solo asignación 1:1** |
| Alguna feature difiere | 9,002 | 43.7% | Un scorer mejor (modelo aprendido) |

Techo de un scorer mejor: **98.70%** (+0.98 pts). Techo de la asignación:
el 56.3% más las 6.3M colisiones intra-periodo, que es el error más
grande medible *sin* ground truth.

Medición clave para el diseño: la distancia **no desempata** los gemelos
(99.3% están a la misma distancia exacta, 0% tiene al correcto más
cerca), y el 97.8% tiene nombre literalmente idéntico. Ahí no falta
señal, falta **estructura**.

---

## 3. Decisiones tomadas

1. **La asignación 1:1 va antes que el modelo aprendido.** Ataca la
   porción mayor (56.3%), es determinista, no necesita etiquetas ni red,
   y es composable: el modelo aprendido después le alimenta un score
   mejor sin rehacer nada.
2. **La tercera columna es aditiva.** No se toca `clee_propagado_radio`
   ni `clee_propagado_h3` ni sus generadores. Las dos columnas actuales
   existen como artefacto comparativo y perderlas quitaría el contraste.
3. **Nombre: `clee_propagado_asignado`.** La convención es
   `clee_propagado_<técnica>`, donde el sufijo nombra cómo se generan los
   candidatos (`radio`, `h3`). Esta columna rompe esa dimensión: varía la
   *regla de decisión*. Descartado `_optimo` porque el diseño usa greedy
   en los componentes gigantes y sería una etiqueta falsa.
4. **Migración: una tabla, una columna.** Solo
   `establecimientos.clee_propagado_asignado`. Verificado que no hace
   falta tocar `entidades_economicas`, `db/indexes.py` ni metadata.
5. **No se materializa la dimensión por método.** Ver §5.

---

## 4. La tercera columna: asignación global 1:1

### Técnica

Matching bipartito de peso máximo, descompuesto por **componente conexo**
del grafo target↔candidato (la descomposición exacta y natural del
problema). Bajo biyección, una identidad de referencia la reclama a lo
más una fila por corte.

### Lo que la medición obliga: diseño híbrido

Componentes conexos sobre Iztapalapa (09/007, peor caso conocido —
contiene la Central de Abastos). 85,193 targets, 131,634 aristas que
pasan la cascada, 7,944 componentes con aristas:

| Tamaño | Componentes |
|---|---:|
| 2 nodos (1:1 trivial) | 4,972 |
| 3-5 | 2,163 |
| 6-20 | 695 |
| 21-100 | 100 |
| 101-500 | 9 |
| **>500** | **5** |

Mediana 2 nodos, p99 = 26, **máximo 29,356 nodos** (matriz ~14,678²).
`linear_sum_assignment` es O(n³) y la matriz densa sola serían ~1.7 GB:
**el Hungarian global no es viable**. Ese componente es casi con certeza
la Central de Abastos, donde miles de puestos "SIN NOMBRE" del mismo giro
se matchean todos entre sí.

Por eso:

- **Hungarian exacto** para componentes ≤ ~500 nodos → cubre 7,939 de
  7,944.
- **Greedy por score descendente** (ordenar aristas, aceptar si ambos
  extremos están libres, O(E log E), sin matriz) para los 5 gigantes.
  Defendible justo ahí: son gemelos feature-idénticos donde cualquier
  biyección vale lo mismo — el valor es la restricción, no la elección.
- **Desempate estable** (p. ej. por string del CLEE) para que dos
  corridas den bit-a-bit lo mismo. Requisito de reproducibilidad, no
  detalle.

### Plomería

- Entrada en el registry `CLEEResolver.METHODS` con su **`prefix_suffix`
  propio** (p. ej. `"AS"`), para que sus `NEW_*` no colisionen con los de
  las otras dos columnas.
- Beneficio gratis: `scripts/clee_propagation_stats.py:69` deriva
  `DEFAULT_COLUMNS` del registry, así que `make propagation-stats` cubre
  la columna nueva sin tocar el script.
- Migración de alembic para la columna. Sin índice, como
  `clee_propagado_h3`.

### Lo que no es gratis

- Hay que **recolectar todas las aristas** que pasan, no solo la mejor.
- Hay que **extender el harness de eval**:
  `scripts/evaluate_masked_clee.py` llama a `propagate_clees_radio` fijo.
- La asignación necesita todos los targets de un periodo a la vez, no
  fila por fila. Eso cambia el patrón de memoria del loop y hay que
  verificar que quepa por partición.

### Diseño experimental resultante

Pasa de un factor a dos:

| | Candidatos: BallTree | Candidatos: H3 |
|---|---|---|
| Decisión: argmax por fila | `clee_propagado_radio` | `clee_propagado_h3` |
| Decisión: asignación global | **`clee_propagado_asignado`** | (opcional, 4ª) |

El baseline de primer-match queda documentado en `findings.md` con sus
artefactos respaldados, sin gastar una columna en historia.

---

## 5. Por qué no se materializa la dimensión por método

`entidades_economicas` es una **vista materializada**: el `BUILD_SQL` de
`loaders/entidades.py` no tiene un dato que no esté en
`establecimientos` (agregado de vida por identidad + fila representativa
del periodo más reciente + `starts_with(id, 'NEW_')`).

Y recalcularla cuesta **~1.1 s** sobre las 82,896,801 filas (medido,
DuckDB read-only). No hay argumento de rendimiento para materializarla
por método.

Comparar los tres métodos —incluida supervivencia— sale de una consulta
ad-hoc parametrizando la columna, sin tocar el schema:

```sql
SELECT '<columna>' AS metodo,
       COUNT(*)                                  AS identidades,
       SUM(starts_with(k,'NEW_')::INT)           AS sinteticas,
       AVG(n_per)                                 AS prom_cortes,
       SUM((n_per >= 2)::INT) / COUNT(*)::DOUBLE AS frac_2plus_cortes
FROM (
  SELECT <columna> AS k, MIN(period) AS primer, MAX(period) AS ultimo,
         COUNT(DISTINCT period) AS n_per
  FROM establecimientos
  WHERE <columna> IS NOT NULL AND <columna> != ''
  GROUP BY <columna>
) t;
```

**Argumento más fuerte para no invertir ahí todavía**: la semántica de
supervivencia que esa tabla codifica es justo lo que los hallazgos del
2026-10-05 están cuestionando (`primer_periodo`/`ultimo_periodo`/
`n_periodos` asumen que la ausencia significa cierre, y se midió que la
tasa aparente de desaparición es inestable por un factor de 400).
Materializar y multiplicar por tres métodos una definición que está por
cambiar es trabajo que se tira.

**Cuándo sí valdrá**: al escribir resultados, por auditabilidad — una
dimensión fechada y congelada que se pueda citar, porque recalcularla
ad-hoc contra un Silver mutable hace que los números cambien en silencio
cada vez que se re-resuelve. Para entonces la semántica ya estará
decidida. (También es parte del contrato Silver→Gold: "Gold la hereda en
`make load`".)

---

## 6. Motor de base de datos — decisión abierta

El síntoma reportado (el archivo crece sin control) es real, pero la
evidencia apunta a otra causa que "DuckDB no aguanta el volumen".

### Lo medido

| Hecho | Valor |
|---|---|
| Filas en `establecimientos` | 82,896,801 |
| Tamaño de la base | 49.6 GiB |
| `free_blocks` / `total_blocks` | **2 / 203,320** |
| Agregación de vida sobre las 82.9M filas | **~1.1 s** |
| Índices sobre `establecimientos` | **16** |

**`free_blocks` = 2 de 203,320** significa que el archivo está ~99.999%
ocupado: **no hay espacio muerto**, así que el crecimiento no es una fuga
por UPDATEs. Y una agregación completa en ~1.1 s es DuckDB funcionando
exactamente como debe. 82.9M filas es volumen medio para DuckDB, no
límite.

### La causa probable: índices, no volumen

De los 16 índices, hay duplicados genuinos: el `index=True` a nivel
columna en `models.py` crea `ix_establecimientos_clee` y
`ix_establecimientos_cod_postal`, mientras `__table_args__` declara
además `ix_estab_clee` (mismo campo `clee`) e `ix_estab_postal` (mismo
`cod_postal`). También `ix_estab_geo_simple` (`cve_ent, cve_mun`) es
prefijo de `ix_estab_geo` (`cve_ent, cve_mun, cve_loc`), o sea
redundante.

Cada índice es un ART persistido que se mantiene fila por fila — lo que
`db/indexes.py` ya documenta como la causa de que las escrituras masivas
sean "órdenes de magnitud más lentas". El proyecto ya mitiga eso con
`suspended_indexes()`, que los suelta durante la carga y los recrea al
salir; los duplicados duplican ese costo de recreación y su espacio.

### Lo que sí dolió históricamente

No el volumen, sino **el patrón de escritura y la granularidad de
recuperación**:

- La resolución CLEE hace `UPDATE` masivo de `clee_propagado_*` sobre las
  82.9M filas. Un `UPDATE` in-place sobre toda la tabla es lo único que un
  motor columnar OLAP maneja mal.
- El OOM del 2026-07-24 dejó filas parciales, corrompió el ART de la PK y
  el reintento incremental remató la base: la unidad de recuperación fue
  **el warehouse completo** (~8 h de rebuild desde Bronze), que es
  exactamente lo que el commit de ese incidente concluyó que no escala.

### Selectividad medida (lo que decide qué índice sirve)

Sobre las 82,896,801 filas, filas devueltas por valor:

| Columna | Valores distintos | Filas/valor |
|---|---:|---:|
| `clee_propagado_radio` | 10,450,524 | **~8** |
| `clee` | 9,579,860 | **~9** |
| `h3_cell_9` | 256,062 | ~324 |
| `cod_postal` | 36,690 | ~2,259 |
| `codigo_act` | 1,127 | ~73,555 |
| `cve_mun` | 570 | ~145,433 |
| `cve_ent` | 32 | ~2,590,525 |
| `period` | 20 | **~4,144,840** |

Y los tiempos, que son el argumento:

| Consulta | Tiempo |
|---|---|
| Lookup por `clee` (indexado, 9 filas) | **0.6 ms** |
| Filtro por `period` (indexado, 2.68M filas) | 209.7 ms |
| Filtro por `nom_estab` (**sin índice**, 809 de 82.9M) | 222.4 ms |

Un filtro no selectivo **con** índice cuesta lo mismo que un escaneo
**sin** índice: cuando el predicado no discrimina, el ART no aporta porque
DuckDB ya descarta bloques por min/max. El costo de *no* tener índice es
~200 ms, despreciable para carga analítica.

Conclusión: se justifican `clee` y `clee_propagado_radio` (búsqueda
puntual); `h3_cell_9` es discutible; el resto (`period`, `cve_ent`,
`cve_mun`, `codigo_act`, `cod_postal`, coords, los compuestos) no compra
nada medible — pero quitarlos exige perfilar consultas reales primero.

### Opciones, de la más barata a la más cara

1. ~~**Eliminar los índices duplicados**~~ **hecho** (migración
   `a4000idx`): `ix_estab_clee` (duplicaba
   `ix_establecimientos_clee`), `ix_estab_postal` (duplicaba
   `ix_establecimientos_cod_postal`) y `ix_estab_geo_simple` (prefijo de
   `ix_estab_geo`). Eliminados también del ORM, que es la fuente de
   verdad — si no, `suspended_indexes()` los recrearía tras cada carga.
   16 → 13 índices declarados; ninguna columna perdió cobertura.
   **La migración no se ha aplicado** a la Silver actual. Pendiente:
   medir el delta de tamaño cuando se aplique o se reconstruya la base.
2. **Cambiar el patrón de escritura a reescribir-y-cambiar por
   partición**, con `period` como llave (ya indexada, 19 cortes). En vez
   de `UPDATE` sobre toda la tabla, escribir la salida resuelta de cada
   corte como partición nueva y hacer swap. Esto ataca las tres cosas a la
   vez: sin espacio muerto, sin mantenimiento de ART durante la escritura,
   y la **unidad de recuperación pasa a ser un corte** en vez del
   warehouse — el problema que el incidente de julio dejó abierto.
3. **Cambiar de motor.** No se justifica hoy: se perderían los agregados
   de ~1 s y se ganaría complejidad operativa (servidor, más RAM,
   despliegue) sin resolver ninguna de las tres causas reales.

### Criterios que sí dispararían una migración

Anotarlos ahora para no decidir por frustración después:

- Pasar de orden de magnitud: cientos de GB o miles de millones de filas
  en un solo nodo.
- Necesitar **escritura concurrente** de varios procesos (DuckDB es
  single-writer).
- Necesitar UPDATE transaccional por fila como operación normal, no
  excepcional — si el pipeline evoluciona a eso, un motor OLTP o un
  lakehouse (Parquet + Iceberg/Delta) encaja mejor.
- Necesitar servir consultas a usuarios concurrentes desde el API.

Mientras ninguno aplique, la recomendación es **(1) + (2)**, no (3).

---

## 7. Orden de trabajo

1. **Medir el costo en tiempo** del loop sin `break` sobre un corte
   completo. Bloquea la decisión de re-resolver Silver (~17 h por
   columna). Es el único número que falta del trabajo de hoy.
2. **Prototipar la asignación 1:1** sobre la muestra de Fase 7 y medirla
   contra el 97.72%. Sin tocar el schema todavía — si no mejora, no hay
   columna.
3. Si mejora: **migración + registry + `prefix_suffix`**, y extender el
   harness de eval.
4. **Índices duplicados** (§6 opción 1) — independiente, se puede hacer
   en cualquier momento.
5. **Reescritura por partición** (§6 opción 2) — antes de la próxima
   re-resolución completa, porque es lo que la vuelve recuperable.
6. **Normalizar sufijos de forma legal** en razón social (`SA DE CV`,
   `S DE RL DE CV`, `SAPI DE CV`, con y sin puntos). Lo motivan las 2
   regresiones de hoy.
7. **Modelo aprendido de scoring** (GBM/RF) con CV **agrupada** — por
   entidad (CLEE), leave-one-state-out (la muestra trae 09/14/32) y corte
   sostenido (entrenar en 05_2021, probar en 11_2022). Un k-fold
   aleatorio filtraría: el mismo negocio aparece en varios cortes.
   Subsume la ponderación IDF, que pasa a ser una feature.
8. **Adjudicación semántica (LLM)** al final, sobre el error que
   sobreviva a lo anterior.

---

## 8. Riesgos y recordatorios operativos

- **El respaldo se pisa al rearrancar**: nunca lanzar dos `--force`
  seguidos; el segundo respaldo sobrescribe el bueno con la base ya
  reseteada. Hay dos backups en `data/silver/` (`mipymes.backup.duckdb`
  del 26-jul, `mipymes.backup2.duckdb` del 30-jul) — verificar cuál es
  bueno antes de confiar en alguno.
- **`caffeinate -dims`** en toda corrida larga. Un sleep/wake mató un
  rebuild de 27.5 h.
- **Silver quedó desalineado respecto al código**: las columnas tienen
  valores de primer-match, el código ahora hace `argmax`. Son dos estados
  coherentes, no uno roto, pero los números del 2026-07-30 en
  `findings.md` (81.09% de propagación, 6.3M colisiones) describen el
  Silver viejo.
- **`make build-entidades` sigue pendiente** desde la re-resolución de
  julio, y ahora además con lógica de primer-match.
- Los nombres de columna **no dicen** qué regla de decisión usan; esa
  distinción vive en git y en `findings.md`. Si se re-resuelve, `radio` y
  `h3` cambian de significado en silencio. Documentarlo en el `comment=`
  de cada columna.
