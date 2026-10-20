# Findings

Hallazgos empíricos del proyecto que no son evidentes del código ni del
git log — el "qué encontramos" al correr las cosas, no el "qué se
construyó" (eso vive en AGENTS.md/todo.md).

---

## 2026-07-20 — Fase 7: precisión de la cascada CLEE con CLEE enmascarado

**Método**: `scripts/evaluate_masked_clee.py` (protocolo en `todo.md`).
Periodo objetivo `05_2021` (920,699 filas, estados 09/14/32), CLEE oficial
enmascarado, resuelto con la maquinaria de producción
(`build_single_period_kb` + `propagate_clees_radio`, ventana newest-first)
contra las 7 referencias oficiales futuras (2021-11 a 2025-05), y
comparado contra la verdad.

### Resultado global

| Métrica | Valor |
|---|---|
| Evaluables | 920,184 / 920,699 |
| Precision | 71.3% |
| Recall | 71.3% |
| F1 | 71.3% |
| Tasa de match espurio (no-evaluables) | 70.5% (363 / 515) |

### El hallazgo: la precisión depende fuertemente de qué referencia resolvió la fila

Segmentando por la referencia que efectivamente resolvió cada fila
(`resolved_by_ref` en `results.parquet`):

| Referencia | Distancia temporal | Filas | Precision |
|---|---|---|---|
| 2021-11 | 6 meses | 344 | 99.7% |
| 2022-05 | 1 año | 730 | 96.9% |
| 2022-11 | 1.5 años | 6,533 | 97.2% |
| 2023-11 | 2.5 años | 3,856 | 88.5% |
| 2024-05 | 3 años | 168,537 | 98.2% |
| 2024-11 | 3.5 años | 11,719 | **62.2%** |
| 2025-05 | 4 años | **728,566** | **64.9%** |

(Tabla corregida el 2026-07-21: la versión original salía de un
`resolved_by_ref` corrupto por atribución posicional — ver entrada
2026-07-21. Las métricas globales no cambiaron.)

**Causa raíz**: la ventana deslizante de producción prueba las
referencias del `resolved_periods` **más nuevas primero**, no las más
cercanas en el tiempo al target. Para un target de 2021, la referencia
"más nueva" (2025) es la MÁS LEJANA temporalmente — donde más negocios ya
cambiaron de nombre, cerraron o se mudaron. Esa referencia "se come" el
79% del volumen (728,566 de 920,699 filas) antes de que las referencias
temporalmente más cercanas (y mucho más precisas) tengan oportunidad de
intentar el match.

**Por qué no es un problema de calibración**: con los MISMOS umbrales
(`ACTIVITY_GATE_THRESH`, `BUSINESS_NAME_MATCH_THRESH`, etc.) y solo
invirtiendo el orden de referencias, la precisión global sube de 71.3% a
95.9% (experimento nearest-first, entrada 2026-07-21). El problema es de
**orden de prioridad de referencias**, no de la cascada de matching en sí.

**Implicación para la tesis**: cuantifica empíricamente que la distancia
temporal target-referencia es el driver dominante de precisión — más
relevante que cualquier ajuste fino de umbrales. Abre una pregunta de
diseño no resuelta: ¿debería la ventana probar referencias por cercanía
temporal en vez de por novedad? Respondida empíricamente en la entrada
2026-07-21 (nearest-first: 95.9% vs 71.3%); la decisión de llevarlo a
producción sigue pendiente por el caveat del encadenamiento transitivo.

**Limitación de esta corrida**: target único (05_2021, el caso con mayor
distancia temporal posible a favor de la producción real, que sí procesa
2015-2020 contra el mismo pool de referencias 2021+). El protocolo
contempla repetir con `11_2022` (referencias a ambos lados) para medir
sensibilidad — pendiente.

**Artefactos**: `data/eval/fase7_masked_clee/{results,manifest}.parquet`
+ `metrics.json` (no committed, regenerable con
`docker compose run --rm tools python scripts/evaluate_masked_clee.py extract && docker compose run --rm tools python scripts/evaluate_masked_clee.py evaluate`).

---

## 2026-07-21 — Fase 7: bug de atribución `resolved_by_ref` + experimento nearest-first

### Bug: la atribución por posición se desalineaba

`propagate_clees_radio` devuelve las filas REORDENADAS (resueltas
primero: `df_resolved.vstack(df_pending)` en `entity_resolution.py`), y
el script atribuía `resolved_by_ref` con una máscara posicional
pre/post-propagación — desalineada desde la segunda referencia en
adelante. Evidencia en el log: para 2024-11 el motor reportó 11,719
matches reales y el script anotó "+156,697 resueltas"; el segmento
`sin_match` de metrics.json tenía 13,999 filas cuando solo 414 son
realmente `NEW_*`. Las métricas GLOBALES nunca estuvieron afectadas (la
clasificación por categoría se hace vía join por `id`); solo la
segmentación por referencia. Fix: atribución por columnas — "recién
resuelta por esta ref" = identidad no nula Y ref aún nula. La tabla del
2026-07-20 quedó corregida con la re-corrida.

### Experimento: nearest-first vs newest-first (`--ref-order nearest`)

Misma muestra, mismos umbrales, solo cambia el orden de referencias:

| Métrica | newest (producción) | nearest |
|---|---|---|
| Precision | 71.3% | **95.9%** |
| Recall | 71.3% | **95.9%** |
| F1 | 71.3% | **95.9%** |
| falsos_match | 263,536 | 37,601 |
| falso_nuevo | 262 | 262 (idénticos) |
| match_espurio | 363 | 363 (idénticos) |

Con nearest-first, la referencia a 6 meses (2021-11) resuelve el 99.97%
del volumen (920,193 filas) a 95.9% de precisión y las demás referencias
solo reciben residuos. Los falsos nuevos y espurios son IDÉNTICOS en
ambos órdenes: el orden no cambia si una fila matchea, solo CONTRA QUIÉN
— todo el delta (+24.6 pts) es falsos match convertidos en correctos.

**Confirma la hipótesis de mortalidad**: consistente con ~50% de
mortalidad empresarial en los primeros 2 años — a 4 años de distancia la
mayoría de los negocios de 2021 ya no existe y su local/zona está
ocupado por otro (falso match); a 6 meses siguen vivos y matchean bien.

**Runtime**: nearest-first NO fue más lento (~2 min ambas corridas): la
primera referencia agota casi todo el pending, así que las siguientes
procesan poquísimas filas.

**Caveat (resuelto abajo)**: este experimento usa referencias 100%
oficiales. En producción (targets 2015-2020), nearest-first encadenaría
a través de periodos resueltos probabilísticamente, acumulando error
transitivo que este experimento no mide — medido en el experimento de
dos saltos siguiente.

### Experimento de dos saltos: el encadenamiento casi no cuesta

`docker compose run --rm tools python scripts/evaluate_masked_clee.py chain` (subcomando `chain`): se enmascara TAMBIÉN
el intermedio 2021-11, se resuelve nearest-first contra las oficiales
2022-05+ (sus no-match reciben `NEW_CHAIN-*`), y el target 05_2021 se
resuelve usando ese intermedio RESUELTO como primera referencia — el
target nunca ve el clee oficial de 2021-11. Es el escenario de
producción: 2019 matchea contra el 2020 recién resuelto.

| Estrategia | Precision | Recall |
|---|---|---|
| newest-first (orden legado) | 71.3% | 71.3% |
| nearest-first directo (refs oficiales) | 95.9% | 95.9% |
| nearest-first encadenado (via intermedio resuelto) | **95.7%** | **95.7%** |

El salto extra por el intermedio probabilístico cuesta **~0.15 puntos**
(95.87% → 95.72%). Desglose del encadenado: 880,561 correctos directos +
344 correctos vía identidad sintética heredada; de los errores, 32,033
son heredados del paso 1 (el target matcheó la fila correcta del
intermedio, pero el intermedio la había resuelto mal) y solo 6,984 son
errores propios del salto target→intermedio. Es decir: el error NO se
amplifica al encadenar — se hereda casi 1:1 (el paso 1 tuvo 37,462
falsos match propios, comparables a los 37,601 del directo).

**Decisión (implementada 2026-07-21)**: producción cambia a
nearest-first — `usable_refs` en `clee_resolver.py` ordena las
referencias futuras al target por cercanía temporal ascendente. El orden
de TARGETS se mantiene newest-first (dirección futuro→pasado del
diseño). El default de `evaluate` pasó a `--ref-order nearest` (ahora es
el análogo de producción); `--ref-order newest` queda para comparación.
Requiere re-correr `clee-resolution-radio --force` (y H3) — las columnas
propagadas actuales quedaron generadas con el orden legado.

**Nota para la tesis**: la elección de referencia importa más que
cualquier umbral — misma cascada, mismos umbrales, +24.6 pts de
precisión solo por probar primero la referencia temporalmente más
cercana; y la propagación transitiva (identidades `NEW_*` heredadas
hacia atrás) es viable: cuesta ~0.15 pts por salto en vez de los ~31 pts
que cuesta saltar 4 años de distancia temporal.

---

## 2026-07-30 — Línea base de cobertura de la propagación (post re-resolución nearest-first)

**Método**: `scripts/clee_propagation_stats.py` (`docker compose run --rm tools python scripts/clee_propagation_stats.py`),
lectura read-only del Silver real tras terminar las dos re-corridas con
`--force` y nearest-first: radio (2026-07-25 10:38 → 2026-07-26 03:19) y
H3 (2026-07-26 → 2026-07-27 04:56). Mide **cobertura** sobre las 82.9M
filas reales; complementa la **precisión** de Fase 7, que se mide contra
ground truth sobre una muestra.

### El número: 81.09% de éxito de propagación

De las **31,392,164** filas de los 11 cortes SIN CLEE oficial
(2015-01 … 2020-11), **25,456,020 (81.09%)** terminaron con un CLEE real
de INEGI y 5,936,144 (18.91%) con identidad sintética `NEW_*`.
H3 da 81.09% (25,454,867) — mismo agregado, distinto detalle (ver abajo).

| Corte | Filas | Vinculadas | Sintéticas | `NEW_*` nacidos | Filas en colisión | % éxito |
|---|---|---|---|---|---|---|
| 2015-01 | 2,683,667 | 1,966,948 | 716,719 | 30,080 | 685,630 | 73.29% |
| 2016-01 | 2,736,201 | 2,006,727 | 729,474 | 15,687 | 685,124 | 73.34% |
| 2016-10 | 2,734,953 | 2,004,416 | 730,537 | 12,887 | 666,734 | 73.29% |
| 2017-03 | 2,726,730 | 2,004,329 | 722,401 | 7,163 | 649,539 | 73.51% |
| 2017-11 | 2,729,456 | 2,005,946 | 723,510 | 3,236 | 629,179 | 73.49% |
| 2018-03 | 2,731,413 | 2,007,215 | 724,198 | 6,336 | 610,781 | 73.49% |
| 2018-11 | 2,736,090 | 2,012,091 | 723,999 | 18,868 | 568,072 | 73.54% |
| 2019-04 | 2,763,135 | 2,045,458 | 717,677 | 688,160 | 516,852 | 74.03% |
| 2019-11 | 3,143,215 | 3,063,609 | 79,606 | 41,319 | 535,788 | 97.47% |
| 2020-04 | 3,183,180 | 3,140,730 | 42,450 | 21,355 | 448,820 | 98.67% |
| 2020-11 | 3,224,124 | 3,198,551 | 25,573 | 25,573 | 324,576 | 99.21% |

Los 9 cortes con CLEE oficial (2021-05 … 2026-05, 51.5M filas) están al
100% por construcción (Fase 1 determinística) y no entran en la métrica.

### El escalón 2019-11 (97.5%) → 2019-04 (74.0%): un corte manda sobre todo lo demás

El corte 2019-04 aporta **688,160 de los 870,664 `NEW_*` nacidos** (79%):
una de cada cuatro de sus filas no encontró pareja en NINGUNA referencia
futura, empezando por 2019-11 que está a 7 meses. Esa masa de identidades
sintéticas se hereda hacia atrás y es la razón de que todos los cortes
2015-2018 se estacionen en ~73.5%. Sin ese corte, la propagación estaría
en ~97-99% en toda la serie.

Descartado que sea un hueco de datos: en 2019-04 `nom_estab`,
`codigo_act`, `cve_ent` y `h3_cell_9` están al 100%, y `raz_social`
(27.9%) y `telefono` (44.4%) están *mejor* que en sus vecinos. Descartado
también que sea local: la tasa de sintéticas es uniforme en los 32
estados (21.3% a 33.0%, contra 2-7% en 2019-11).

Que sea uniforme y nacional apunta a una discontinuidad del propio corte
(cambio de universo, re-geocodificación o metodología de INEGI entre
04/2019 y 11/2019 — el universo crece +380K filas hacia 2019-11), no a
mortalidad empresarial ni a fallas de la cascada. **Queda abierto**: la
prueba directa es cruzar 2019-04 con 2019-11 por nombre+municipio y ver
la distribución de distancias (si hay una cola >500 m, la cascada nunca
tuvo oportunidad porque el pre-filtro espacial la descarta antes).

### Identidades y cadenas

- **10,450,524 identidades**: 9,579,860 reales (exactamente las mismas
  que trae el `clee` oficial — la propagación reusa identidades, no
  inventa) + 870,664 sintéticas.
- **85.52% cubren 2+ cortes**, promedio **7.33 cortes por identidad** (el
  `clee` oficial solo alcanza 5.38 porque existe desde 2021): la
  propagación agrega ~2 años de historia por negocio, que es justo lo que
  necesita el análisis de supervivencia.

### Colisiones intra-periodo: el error que sí se puede medir sin ground truth

Un establecimiento aparece UNA vez por corte, así que una identidad
repetida dentro del mismo corte es error duro. Comprobado que la fuente
no lo trae: el `clee` oficial tiene **0 colisiones** en 9.58M identidades
(`docker compose run --rm tools python scripts/clee_propagation_stats.py ARGS="--column clee --no-agreement"`).

| Columna | Identidades con colisión | Filas excedentes |
|---|---|---|
| `clee` (oficial) | 0 | 0 |
| `clee_propagado_radio` | 595,419 (5.70%) | 6,321,095 |
| `clee_propagado_h3` | 606,137 (5.80%) | 6,391,400 |

**6.32M filas excedentes = 20.1% de las filas propagadas**: la cascada es
greedy y no impone matching 1:1, así que varias filas del target pueden
heredar el mismo CLEE de referencia. Aparece desde el primer target
(2020-11: 324,576) y crece hacia atrás. Es el candidato natural a
siguiente mejora (matching mutuo/asignación óptima por bloque) y una
métrica de calidad reportable en la tesis sin ground truth.

### Radio vs H3 NO son idénticos sobre datos reales

La paridad verificada en tests (1,500 filas sintéticas, 800 end-to-end)
no se sostiene a escala: acuerdo semántico **95.78%**, con **3,398,748
filas (4.1%) con CLEE real DISTINTO** y 97,747 real-vs-sintética. La
divergencia arranca en el primer target (2020-11: 6.7% de sus filas) y
crece hacia atrás (2015-01: 13.9%), consistente con amplificación por la
ventana deslizante. Los agregados coinciden (mismo total de `NEW_*`
nacidos, mismas 10,450,524 identidades), así que el desacuerdo es
*a quién* se asigna cada fila, no *cuántas* se resuelven — el mismo
patrón que producen los empates resueltos por orden de candidatos.
Pendiente: aislar el caso concreto (comparar vecinos de un target
completo, no de una muestra sintética).

### Antes vs después (dato histórico, para no perderlo)

El estado previo de las columnas (orden legado newest-first) **ya no es
recuperable**: ambas se reescribieron con `--force` y el respaldo
`data/silver/establecimientos_backup.parquet` (2026-07-23) tiene las dos
columnas de identidad en NULL. Lo que sí sobrevive es lo siguiente.

| Métrica | Antes (newest-first) | Ahora (nearest-first) |
|---|---|---|
| Precision/recall (Fase 7, muestra 09/14/32, target 05_2021) | **71.3%** | **95.9%** directo · **95.7%** encadenado |
| Identidades `NEW_*` nacidas (11 cortes propagados, nacional) | **933,577** | **870,664** (−62,913, −6.7%) |
| Cobertura por fila (% con CLEE real) | no medida | **81.09%** |
| Colisiones intra-periodo | no medida | 6.32M filas excedentes |

`NEW_*` nacidos por corte, corrida legada (log H3 del 2026-07-18/19) vs
actual (idéntica en radio y H3, 2026-07-25/27):

| Corte | Legado | Actual | Δ |
|---|---|---|---|
| 2020-11 | 25,475 | 25,573 | +98 |
| 2020-04 | 22,079 | 21,355 | −724 |
| 2019-11 | 42,029 | 41,319 | −710 |
| 2019-04 | 690,046 | 688,160 | −1,886 |
| 2018-11 | 38,777 | 18,868 | **−19,909** |
| 2018-03 | 19,949 | 6,336 | **−13,613** |
| 2017-11 | 12,399 | 3,236 | **−9,163** |
| 2017-03 | 13,302 | 7,163 | −6,139 |
| 2016-10 | 18,116 | 12,887 | −5,229 |
| 2016-01 | 18,970 | 15,687 | −3,283 |
| 2015-01 | 32,435 | 30,080 | −2,355 |

Nearest-first no solo asigna mejor (los +24.6 pts de precisión de Fase
7): además **fragmenta menos** — 62,913 negocios que antes arrancaban una
identidad sintética nueva ahora se enganchan a una cadena existente, y el
ahorro se concentra en los cortes 2017-2018, los que más saltos de cadena
acumulan. Lección de método: la cobertura por fila (81.09%) y las
colisiones (6.32M) son ahora la línea base contra la que se compara
cualquier cambio futuro de la propagación — se regeneran con
`docker compose run --rm tools python scripts/clee_propagation_stats.py`.

---

## 2026-10-05 — El CLEE oficial no es estable entre ediciones: la ausencia no mide cierre

**Método**: consultas read-only sobre `data/silver/mipymes.duckdb`
(82,896,801 filas, 20 cortes, 2015-01 … 2026-05). Identidad =
`coalesce(clee, clee_propagado_radio)` para la serie completa, y `clee`
puro para los 8 cortes oficiales (2021-05+), que es la prueba que aísla
la fuente de la propagación. Sin script todavía; las consultas están
transcritas abajo en prosa y son reproducibles en una sesión de `duckdb`.

Surgió al verificar para el protocolo de tesis la afirmación de que el
DENUE "solo registra atributos observables"; el salto de volumen entre
2020-11 y 2021-05 llevó a medir el evento de cierre y lo que apareció es
un problema más serio.

### 1. El universo del DENUE cambia de tamaño por escalones

| Corte | Filas | Corte | Filas |
|---|---|---|---|
| 2015-01 | 2,683,667 | 2021-05 | **5,515,863** |
| 2019-04 | 2,763,135 | 2022-11 | 5,530,924 |
| 2019-11 | 3,143,215 | 2024-05 | 5,564,607 |
| 2020-11 | **3,224,124** | 2024-11 | **6,058,534** |

De 2020-11 a 2021-05, seis meses, el universo crece **+2,291,739 filas
(+71%)**. El 95.3% del aumento está en un solo estrato:

| Estrato | 2020-11 | 2021-05 | Δ |
|---|---|---|---|
| 0 a 5 personas | 2,608,677 | 4,792,616 | **+83.7%** |
| 6 a 10 personas | 294,393 | 353,970 | +20.2% |
| 11 a 30 personas | 214,690 | 248,151 | +15.6% |
| 251 y más | 12,368 | 14,083 | +13.9% |

Por sector, el comercio al por menor (46) pasa de 1,138,338 a 2,229,285
(**+95.8%**), otros servicios (81) +60%, alojamiento y alimentos (72)
+65%, industria alimentaria (31) +127%. Ninguna dinámica empresarial
duplica el comercio al menudeo en un semestre: es ampliación de universo.

**Confirmación independiente del identificador**: de las 2,645,966
identidades que aparecen en 2021-05 y no estaban en 2020-11,
**2,588,924 (97.8%) traen `fecha_alta_date` anterior a 2020-11**. Los
registros mismos declaran haber ingresado al directorio antes del corte
en el que aparecen por primera vez.

### 2. La tasa aparente de desaparición es inestable por un factor de 400

Identidad `coalesce(clee, clee_propagado_radio)`, pares de cortes
consecutivos. `identidades_prev` es distinct de identidad, no filas (de
ahí que sea menor que el conteo de filas en los cortes propagados: son
las colisiones ya documentadas en la entrada 2026-07-30).

| Par | identidades_prev | Desaparecen | % |
|---|---|---|---|
| 2017-11 → 2018-03 | 2,100,277 | 21,267 | 1.0 |
| 2018-11 → 2019-04 | 2,168,018 | 42,039 | 1.9 |
| **2019-04 → 2019-11** | 2,246,283 | **922,285** | **41.1** |
| 2020-11 → 2021-05 | 2,899,548 | 29,651 | 1.0 |
| **2021-05 → 2021-11** | 5,515,863 | 4,642 | **0.1** |
| 2022-11 → 2023-11 | 5,530,924 | 307,428 | 5.6 |
| **2024-05 → 2024-11** | 5,564,607 | **1,993,179** | **35.8** |
| 2024-11 → 2025-05 | 6,058,534 | 320,551 | 5.3 |
| **2025-05 → 2026-05** | 6,097,662 | **1,146,688** | **18.8** |

Los dos extremos son igual de implausibles. **41.1% y 35.8% semestral**
no es mortalidad empresarial, y **0.1%** tampoco: con ~50% de mortalidad
a dos años (`ramirez2024nacimiento`) un semestre debería estar en el
orden de varios puntos. Ambos extremos dicen lo mismo, que el CLEE es
una clave de directorio y no un identificador de negocio.

### 3. No es culpa de la propagación: pasa con CLEE oficial

Los cortes 2021-05+ tienen CLEE oficial al 100% (Fase 1 determinística).
Restringiendo a `clee` puro, los números no cambian en una sola unidad
respecto a la tabla anterior: 2024-05 → 2024-11 pierde **1,993,179 CLEE
oficiales (35.8%)** y gana 2,487,106 que no existían. Es un reencadenado
masivo de claves dentro de la fuente, no un artefacto de
`propagate_clees_radio`.

Simétricamente, los cortes con mucha desaparición son los mismos con
mucha aparición (2019-11: 1,366,193 altas / 922,285 bajas; 2024-11:
2,487,106 / 1,993,179; 2026-05: 1,187,100 / 1,146,688). Las mismas
unidades cambiando de clave.

### 4. Las desapariciones no se revierten en cortes posteriores

De los 1,993,179 CLEE que desaparecen en 2024-11, **solo 159,746 (8.0%)
reaparecen en algún corte posterior**. La discontinuidad es permanente,
no una omisión de una edición.

### 5. No es un defecto de formato ni de ingesta

El CLEE está bien formado a ambos lados del salto: longitud 28 en
6,058,534 de 6,058,534 filas de 2024-11 y en 5,564,604 de 5,564,607 de
2024-05 (3 filas con 27). Los prefijos de entidad están presentes y
crecen de forma uniforme en los 32 estados (010: 62,017 → 71,238; 020:
127,929 → 138,139; 050: 115,832 → 125,187). No hay truncamiento, ni
cambio de esquema, ni estados faltantes.

### 6. `fecha_alta_date` tampoco sirve como fecha de nacimiento

Cuatro valores concentran el **94.1%** de las 82.9M filas, y tres de
ellos son fechas de corte o de arranque del propio directorio:

| fecha_alta_date | Filas | Qué es |
|---|---|---|
| 2010-07-01 | 31,479,088 | arranque del DENUE |
| 2019-11-01 | 20,517,257 | corte 2019-11 |
| 2014-12-01 | 19,423,254 | previo a la serie |
| 2024-11-01 | 6,594,051 | corte 2024-11 |

Es fecha de incorporación al directorio, reasignada en oleadas, no fecha
de inicio de operaciones. Para el 38% de los registros la antigüedad
derivada de este campo es un artefacto del registro.

### Implicación para la tesis: la regla de dos ausencias no alcanza

El protocolo define cierre como "no aparecer en dos cortes consecutivos
sin reaparición posterior". Esa regla protege contra la omisión puntual
de una edición, pero es indefensa ante lo medido aquí: en 2024-11 la
discontinuidad abarca dos cortes y es permanente en el 92% de los casos,
así que la regla clasificaría ~1.83M establecimientos vivos como
cerrados, concentrados en un solo intervalo. Un modelo entrenado sobre
eso aprende el calendario de reencadenado del INEGI, no el riesgo de
cierre, y lo hará con un C-index engañosamente alto porque el patrón es
muy regular.

Consecuencias de diseño, en orden de urgencia:

1. **El emparejamiento no puede descansar en el CLEE.** Hay que correr
   la cascada de resolución de entidades también entre cortes oficiales
   (hoy solo se usa para 2015-2020), o al menos sobre los pares con
   discontinuidad masiva. La maquinaria ya existe.
2. **Excluir o marcar los intervalos contaminados.** Los pares
   2019-04→2019-11, 2024-05→2024-11 y 2025-05→2026-05 no son utilizables
   como ventanas de observación del evento sin tratamiento previo.
3. **El salto de universo de 2021-05 impide usar 2015-2020 y 2021+ como
   una sola serie** sin acotar a un subuniverso comparable. La opción
   limpia es restringir a los estratos y sectores cuya cobertura no
   cambia, o arrancar la ventana de análisis en 2021-05.
4. **La antigüedad debe medirse desde el primer corte observado**, no
   desde `fecha_alta_date`, con un indicador de ingreso en la edición
   inicial para capturar el truncamiento por la izquierda.

### Pendientes

- Confirmar contra la nota metodológica del DENUE qué cambió en 2021-05
  (hipótesis: incorporación del universo de los Censos Económicos 2019)
  y en 2024-11. Es la diferencia entre "la fuente cambió de universo" y
  "la fuente reasigna claves", que tienen tratamientos distintos.
- Medir cuánto recupera la cascada de resolución sobre el par
  2024-05→2024-11: cuántos de los 1,993,179 "cerrados" se reencuentran
  por nombre, actividad y cercanía. Es el análogo de Fase 7 pero entre
  cortes oficiales, y da la cota superior del error de la regla actual.
- Encapsular estas consultas en `scripts/` (análogo a
  `clee_propagation_stats.py`) para que las tasas de aparición y
  desaparición por par de cortes sean una métrica regenerable y no un
  hallazgo de una sesión.
- Revisar si el 0.1% de 2021-05→2021-11 indica lo contrario del problema
  anterior, que el CLEE se arrastra sin verificar permanencia, lo que
  subestimaría cierres reales.

---

## 2026-10-05 (b) — `fecha_alta` corrobora las discontinuidades, y acota la hipótesis de reencadenado

Extiende la entrada anterior del 2026-10-05. Dos aportes: confirma la
semántica del campo contra la fuente oficial, y **corrige parcialmente**
la lectura de que las desapariciones masivas son reasignación de claves a
los mismos establecimientos.

### Semántica oficial del campo (confirmada, no inferida)

El diccionario de datos que viene en el propio Bronze
(`bronze/denue/11_2021/comercio_al_por_menor_2_de_4.zip`,
`diccionario_de_datos/denue_diccionario_de_datos.csv`) define:

> `fecha_alta` — "Fecha en la que la unidad económica se integró al
> Directorio Estadístico Nacional de Unidades Económicas y al Registro
> Estadístico de Negocios de México (RENEM)."

El catálogo RNM del INEGI para DENUE 2023 (variable `FECHA_ALTA`,
<https://www.inegi.org.mx/rnm/index.php/catalog/924/variable/F10/V412>)
añade el dato que cierra cualquier duda: es **"variable de control
predeterminada, que no es objeto de llenado por parte del informante"**.
La asigna el sistema al incorporar la unidad; el negocio nunca declara
cuándo se fundó.

Comportamiento verificado en el Silver, consistente con esa definición:

- **9,549,609 de 9,579,860 CLEE (99.68%) tienen una sola `fecha_alta` en
  los 20 cortes** (30,215 tienen dos, 36 tienen tres). El campo se
  arrastra, no se recalcula por edición.
- **78,994,130 filas (95.3%) la traen anterior a su corte**, 3,901,029
  (4.7%) igual al corte, y solo 1,641 posterior (0.002%, anomalía a
  revisar). Que sea anterior al periodo es el comportamiento esperado.

Consecuencia: para las unidades ya operando en julio de 2010 la
antigüedad derivada del campo está truncada por la izquierda; para las
incorporadas después es una cota inferior de existencia, y se aproxima a
la fundación solo si el negocio entró al directorio siendo nuevo.

### Las oleadas de `fecha_alta` son los mismos eventos de discontinuidad

Las cuatro fechas que concentran el 94.1% de las filas (2010-07,
2019-11, 2014-12, 2024-11) no son todas explicables por el arranque del
directorio. La prueba directa, sobre el par 2024-05 → 2024-11:

De los **2,487,106 CLEE que aparecen** en 2024-11 sin estar en 2024-05,
**2,219,060 (89.2%) traen `fecha_alta` = 2024-11-01**. Es decir, el
directorio los registra como incorporaciones nuevas en esa misma
edición. Los demás se reparten entre 2019-11 (93,239), 2010-07 (89,032)
y 2014-12 (49,769).

Las dos anomalías medidas en la entrada anterior son por tanto un solo
evento visto desde dos campos: en la edición 2024-11 el directorio da de
baja ~2.0M claves y da de alta ~2.2M unidades como nuevas.

### Corrección: el reencadenado explica como máximo una fracción

La entrada anterior leyó esto como "reencadenado masivo de claves dentro
de la fuente", sugiriendo que son los mismos establecimientos con clave
distinta. La prueba de gemelos lo acota y no lo sostiene en esa
magnitud.

Cruzando las 1,993,179 bajas de 2024-05 contra las 2,219,060 altas
nuevas de 2024-11 por **coincidencia exacta de `nom_estab` + `cve_ent` +
`cve_mun` + `codigo_act`**: solo **297,105 bajas (14.9%) tienen gemelo
exacto** entre las altas.

Lectura correcta:

- El 14.9% es una **cota inferior**, porque la coincidencia exacta de
  nombre es un criterio estricto (abreviaturas, erratas, variantes de
  "SIN NOMBRE", cambios de razón comercial). La cascada de resolución,
  que usa similitud de nombre más cercanía espacial, encontraría más.
- Pero está lejos de explicar el total. El grueso de las ~2.0M bajas
  **no reaparece con el mismo nombre en el mismo municipio y la misma
  clase de actividad**, lo que es compatible con rotación real de
  universo (altas y bajas de unidades distintas) y no solo con un cambio
  de clave.

Es decir: la edición 2024-11 combina al menos tres cosas que hoy no se
pueden separar — reasignación de clave a la misma unidad (≥14.9%),
incorporación de unidades que el directorio no cubría, y cierres reales.
**Mientras no se separen, el 35.8% de "desaparición" de ese par no es
utilizable como tasa de cierre**, que es la conclusión operativa de la
entrada anterior y no cambia.

### Pendientes (reemplazan al segundo punto de la entrada anterior)

- Correr la cascada de resolución (`propagate_clees_radio` /
  `_h3`) sobre el par 2024-05 → 2024-11 tratando 2024-11 como target y
  2024-05 como referencia. Da el reparto real entre "misma unidad con
  clave nueva" y "unidad distinta", y convierte el 14.9% en una cifra
  defendible. Es el experimento que decide si ese par se puede usar.
- Repetir la prueba de gemelos en 2019-04 → 2019-11 y 2025-05 → 2026-05,
  los otros dos pares con discontinuidad masiva, para ver si los tres
  tienen la misma composición o son fenómenos distintos.
- Revisar las 1,641 filas con `fecha_alta_date` posterior a su propio
  corte: son imposibles por definición del campo y apuntan a un defecto
  de parseo en la ingesta.
- El RENEM aparece en la definición oficial del campo. Es el registro
  estadístico de negocios de acceso restringido, y es la vía por la que
  existirían variables financieras enlazables (el análogo del cruce
  SIRENE-Diane del caso francés). Vale documentar qué se puede solicitar.

---

## 2026-10-06 — Los `falso_match` no eran un problema de nombres: la cascada elegía al primer candidato, no al mejor (95.90% → 97.72%)

**Pregunta de origen**: si se agregara una tercera columna
`clee_propagado_llm` con un adjudicador semántico (un LLM que lee los dos
nombres y decide si son el mismo negocio), ¿cuánto del error corregiría?
La respuesta resultó ser "casi nada, porque el problema es otro".

**Método**: sobre `data/eval/fase7_masked_clee/results_nearest.parquet` se
toman las 37,601 filas con `categoria == "falso_match"` y se reconstruye
el **triple** completo de cada error:

- **T** — la fila objetivo real, de `target.parquet` (corte `05_2021`).
- **K** — el registro *impostor* del que heredó el CLEE equivocado: se
  busca `clee_propagado_radio` en `ref_<resolved_by_ref>.parquet`.
- **C** — el registro *correcto*: se busca `clee_verdadero` en **esa
  misma** referencia.

Reconstruir **C** es lo que destapó el hallazgo; la primera pasada solo
había mirado el par (T, K) y llevaba a una conclusión equivocada.

### Los tres registros no viven en el mismo corte — y eso importa

T es de **mayo 2021**. K y C son de la referencia que resolvió la fila,
que en el 99.9% de los casos es **noviembre 2021** (37,580 de 37,601):
nearest-first prueba primero la referencia a 6 meses y ahí se agota
prácticamente todo el pendiente.

| Referencia que resolvió | Filas | Distancia temporal |
|---|---:|---|
| 2021-11 | 37,580 | 6 meses |
| 2024-11 | 13 | 3.5 años |
| 2022-11 | 4 | 1.5 años |
| 2023-11 | 3 | 2.5 años |
| 2025-05 | 1 | 4 años |

Así que la comparación es entre cortes distintos (mayo → noviembre de
2021), como corresponde al diseño. **Pero K y C son del mismo corte**: el
impostor y el registro correcto estaban los dos en noviembre 2021, a
metros uno del otro. Eso convierte el error en un problema de *elección
entre candidatos simultáneos*, no de deriva temporal.

### El hallazgo: el registro correcto estaba disponible, con nombre idéntico

| Medición sobre los 37,601 `falso_match` | Valor |
|---|---|
| El CLEE verdadero existía en la **misma** referencia que resolvió la fila | **37,561 (99.9%)** |
| Mediana de `token_sort_ratio(T, C)` | **100** |
| Filas con `ratio(T, C) >= 95` | **100%** |

Es decir: en prácticamente todos los casos la cascada tenía enfrente al
registro correcto, con el nombre **exactamente igual**, y aun así se
quedó con otro. Ejemplos del par (T, C) — nótese que son idénticos:

```
T = C : 'CONSULTORIO DENTAL'
T = C : 'CLINICA SANTA SOFIA'
T = C : 'CONSULTA DE OFTALMOLOGIA'
T = C : 'CONSULTORIO DE GASTROENTEROLOGIA'
T = C : 'INNOVARE'
```

### La causa: `break` en el primer match, sin comparar scores

`src/etl/utils/entity_resolution.py:215-221`, dentro del loop de vecinos:

```python
if is_match:
    if strategy in ("NAME_MATCH", "RAZON_MATCH"):
        best_clee = kb_clees[n_idx]
        break              # <-- el PRIMERO que pasa el umbral gana
    if best_clee is None:  # <-- y el primer COMBINED se conserva
        best_clee = kb_clees[n_idx]
```

Los vecinos llegan ordenados **por distancia ascendente**, y la cascada
devuelve en el primero que supera el umbral (`> 80` razón social, `> 75`
nombre comercial). No hay comparación de scores. Entonces un vecino a 3 m
con ratio 81 gana sobre el registro verdadero a 10 m con ratio 100,
simplemente porque se evaluó antes. El comentario del código
("un match directo lo reemplaza") describe la precedencia entre
estrategias, no un argmax.

### Cuánto arregla elegir el mejor en vez del primero

Contrastando, para cada `falso_match`, el score del impostor que ganó
contra el score del registro correcto que estaba disponible:

| Resultado si la cascada hiciera `argmax(score)` | Filas | % |
|---|---:|---:|
| El correcto puntea **más alto** → se arregla | 25,487 | 67.9% |
| **Empate** de score → lo decide el desempate | 12,074 | 32.1% |
| El impostor puntea más alto → no se arregla | **0** | **0.0%** |

**En ninguna de las 37,561 filas el impostor gana legítimamente por
score.** De ahí salió una predicción de 98.64% de precisión, que resultó
optimista — ver la sección siguiente con el resultado medido.

### Resultado medido: `argmax` implementado y evaluado (mismo día)

Implementación: `_best_candidate()` en `entity_resolution.py` (helper
único que reemplaza el loop duplicado en `_match_chunk` y
`_run_matching_sequential`), y `calculate_match_score()` ahora devuelve un
tercer elemento `score` con tiers que replican la precedencia previa de
los paths (`RAZON_TIER` 2000 > `NAME_TIER` 1000 > combinado ≤ 600).
Baseline preservado en
`data/eval/fase7_masked_clee/baseline_firstmatch_2026-07-21/`.

| Métrica | Primer match (baseline) | `argmax` | Δ |
|---|---|---|---|
| **Precision / recall / F1** | 95.90% | **97.72%** | **+1.82 pts** |
| `falso_match` | 37,601 | 20,619 | −16,982 |
| `falso_nuevo` | 262 | 262 | 0 |
| `match_espurio` | 363 | 363 | 0 |
| `nuevo_correcto` | 152 | 152 | 0 |

Que `falso_nuevo`, `match_espurio` y `nuevo_correcto` queden **idénticos**
es la prueba de control del cambio: `argmax` altera contra *quién*
matchea una fila, nunca *si* matchea. Igual que el hallazgo de
nearest-first en 2026-07-21, todo el delta son falsos match convertidos en
correctos.

Transición fila por fila contra el baseline:

| Transición | Filas |
|---|---:|
| `falso_match` → `correcto` | **16,984** |
| `falso_match` → `falso_match` | 20,617 |
| `correcto` → `falso_match` (**regresión**) | **2** |

**La predicción de 98.64% estaba mal, y el porqué importa.** Se calculó
comparando al registro correcto contra el impostor *del baseline*. Pero
bajo `argmax` el ganador puede ser **otro** registro equivocado que
empata con el correcto: la condición para que un `falso_match` sobreviva
al argmax es, casi por definición, que exista un empate. Verificado
llamando al scorer de producción sobre los 20,579 sobrevivientes con
ambos registros localizables:

| Por qué sobrevive al `argmax` | Filas | % |
|---|---:|---:|
| **Empate exacto de score** | **20,527** | **99.7%** |
| El correcto puntea más alto pero no era candidato | 50 | 0.2% |
| El impostor gana por el tier de razón social | 1 | 0.0% |
| El impostor puntea más alto | 1 | 0.0% |

Las 50 filas donde el correcto puntea más alto y aun así perdió no son un
fallo del argmax: el registro correcto **no estaba en el conjunto de
candidatos** (fuera del radio de 500 m o del disco H3). Eso apunta a
generación de candidatos, no a la elección entre ellos.

Lección metodológica: una predicción derivada de un contrafactual
("¿qué pasaría si…?") sobre el resultado de *otro* algoritmo no es una
predicción del algoritmo nuevo, porque el nuevo cambia el conjunto de
comparación. El número medido (97.72%) es el que vale; la banda de
empates real es **20,527, no 12,074**, y es la que hay que atacar.

### Las 2 regresiones: ruido de sufijo legal en la razón social

Ambas tienen el mismo mecanismo, y conviene documentarlo porque es un
defecto de datos que el argmax dejó a la vista:

```
T         nom='CARRETERA DE COAHUILA Y SAN LUIS'
          raz='CARRETERAS DE COAHUILA Y SAN LUIS S.A.P.I. DE C.V. SA DE CV'
correcto  nom='CARRETERA DE COAHUILA Y SAN LUIS'   <- nombre IDENTICO
          raz='CARRETERAS DE COAHUILA Y SAN LUIS SAPI DE CV'
          -> NAME_MATCH  score=1100
asignado  nom='OPERADORADE CARRETERAS DE COAHUILA Y SAN LUIS'
          raz='OPERADORA DE CARRETERAS DE COAHUILA Y SAN LUIS SA DE CV'
          -> RAZON_MATCH score=2082   <- gana por tier
```

La razón social del target trae el **sufijo legal duplicado**
(`S.A.P.I. DE C.V. SA DE CV`), así que sus tokens incluyen `SA` y `CV`
dos veces. Eso hace que la razón del impostor (`... SA DE CV`) puntee más
alto que la del correcto (`... SAPI DE CV`, donde `S.A.P.I.` con puntos
es un token distinto de `SAPI`), y Path A —el tier más alto— se lo lleva.
La otra regresión es idéntica: `S DE RL DE CV SA DE CV` en el target.

Son 2 filas de 882,321 previamente correctas (0.0002%) contra 16,984
arregladas, así que el cambio es netamente positivo. Pero señala un
pendiente real: **normalizar sufijos de forma legal**
(`SA DE CV`, `S DE RL DE CV`, `SAPI DE CV`, con y sin puntos) antes de
comparar razón social.

### Los empates: nombre y distancia están agotados

Pregunta natural: si el nombre empata, ¿desempata la distancia? Medido
sobre los 12,074 empates estimados antes de implementar el argmax (la
banda real medida después resultó ser de 20,527, pero la composición no
cambia: son el mismo fenómeno de gemelos), contrastando dónde está el
correcto respecto del impostor:

| Posición del registro correcto | Filas | % |
|---|---:|---:|
| A la **misma** distancia que el impostor | 11,989 | 99.3% |
| Más lejos que el impostor | 85 | 0.7% |
| Más cerca que el impostor | **0** | **0.0%** |

**La distancia no desempata ni una sola fila**, y en 85 casos apuntaría
al registro equivocado. De los 11,989 con nombre y distancia empatados,
el **97.8%** tiene el nombre del impostor *literalmente idéntico* al del
correcto. Las únicas señales que a veces difieren: teléfono (27.6%) y CP
(0.8%).

```
T   = 'CLINICA ONCOLOGICA'   [Consultorios de medicina especializada]
imp = 'CLINICA ONCOLOGICA'   d=0.0m  tel=3338849530
cor = 'CLINICA ONCOLOGICA'   d=0.0m  tel=3338482100

T   = 'SMILE'                [Consultorios dentales del sector privado]
imp = 'SMILE'                d=0.0m  tel=None
cor = 'SMILE'                d=0.0m  tel=None

T   = 'ESCUELA SECUNDARIA TECNICA 163 ADOLFO LOPEZ MATEOS'
imp = 'ESCUELA SECUNDARIA TECNICA 163 ADOLFO LOPEZ MATEOS'  d=428.6m
cor = 'ESCUELA SECUNDARIA TECNICA 163 ADOLFO LOPEZ MATEOS'  d=428.6m
```

Son tres registros con el mismo nombre, el mismo giro y las mismas
coordenadas. **Ninguna cantidad de inteligencia extrae información que no
está en el registro**: cualquier método —umbral, score ponderado o
modelo de lenguaje— está adivinando en estas filas.

Y hay una lectura más incómoda: varios de estos pares parecen ser **el
mismo establecimiento cargando dos CLEE distintos en la referencia**
(`CLINICA ONCOLOGICA` a 0 m con dos teléfonos; `NUCLEO MEDICO SIN
NOMBRE` duplicado a 0 m). Eso enlaza con la entrada 2026-10-05: el CLEE
oficial no es estable entre ediciones. Si el par es el mismo negocio con
dos claves, **cuál de las dos es "la correcta" es un artefacto de
etiquetado, no un hecho del mundo**, y el `falso_match` que cuenta el
evaluador es en parte espurio.

Implicación para la tesis: en estas filas lo que importa no es acertar
*qué clave* gana, sino que el mapeo sea **biyectivo y consistente** — un
candidato reclamado por un solo target. Con asignación 1:1 los conteos de
supervivencia y mortalidad (la unidad de análisis) salen bien aunque la
identidad individual quede intercambiada entre dos gemelos.

**Costo**: quitar el `break` obliga a evaluar todos los vecinos de cada
target en vez de cortar temprano. Sobre 82.9M filas eso es tiempo real y
hay que medirlo antes de prometer la re-resolución completa (hoy son
~17h por columna). Mitigación obvia: conservar el corte temprano solo
ante un score perfecto (ratio 100 + gate 100), que es el caso mayoritario.

### Anatomía de los impostores (secundario, ya no es la causa)

La primera pasada clasificó los pares (T, K) por qué distingue al
impostor del objetivo. Se conserva porque describe *qué tipo de vecino*
se cuela, pero **no** es el diagnóstico: en todos estos casos el registro
correcto también estaba disponible.

| Qué diferencia al impostor | Filas | % |
|---|---:|---:|
| `SIN NOMBRE` / nombre ausente | 11,598 | 30.8% |
| Nombre idéntico al objetivo | 9,227 | 24.5% |
| Un nombre contiene al otro | 4,947 | 13.2% |
| Una palabra distinta | 4,826 | 12.8% |
| Ortografía / morfología | 2,650 | 7.0% |
| Clave o número (CCT, folio) | 2,277 | 6.1% |
| Sin solape de tokens | 2,076 | 5.5% |

Clasificación sobre `nom_estab` normalizado (mayúsculas, puntuación a
espacio) y tokenizado sin stopwords ni tokens de ≤2 caracteres;
`ortografia` = el mejor `fuzz.ratio` entre los tokens exclusivos de cada
lado es ≥ 75 (misma palabra mal escrita); `palabra distinta` = ese ratio
es < 75 y ambos lados aportan un token alfabético de ≥ 5 caracteres.
Reparto sectorial de `palabra distinta` (SCIAN 2 dígitos): 46 comercio al
por menor 2,700 (56%), 62 salud 593, 61 educación 477, 72
alimentos/cafeterías 252, 93 administración pública 237, 52 financiero
186, 81 otros servicios 145, 43 comercio al por mayor 136.

Cinco ejemplos reales del tipo "una palabra distinta", que es el que
parecía justificar una capa semántica:

```
# 1. Misma unidad médica, especialidad distinta — SCIAN idéntico
T: CONSULTORIO MEDICO UNIDAD MEDICA PRADOS PROVIDENCIA
K: CONSULTORIO DENTAL UNIDAD MEDICA PRADOS PROVIDENCIA
   act 621211 en ambos · ratio=90 · dist=0m · CP 44670 · 14/039

# 2. La similitud de caracteres apunta al revés del significado
T: UNIDAD MEDICA INDEPENDENCIA CONSULTORIO MEDICO EN GINECOLOGIA
K: UNIDAD MEDICA INDEPENDENCIA CONSULTORIO MEDICO EN GENETICA
   ratio=92 · fuzz.ratio(GINECOLOGIA, GENETICA)≈60 · dist=0m · 14/039

# 3. Dos turnos de la misma escuela: mismo CCT y mismo teléfono
T: ESCUELA SECUNDARIA JAIME TORRES BODET TURNO VESPERTINO 14DES0032L
K: ESCUELA SECUNDARIA JAIME TORRES BODET TURNO MATUTINO  14DES0032L
   act 611132 en ambos · ratio=84 · dist=0m · tel 3213872747 idéntico

# 4. Dependencias municipales con domicilio y conmutador compartidos
T: ASEO PÚBLICO    DELEGACION MUNICIPAL DE CAPILLA DE GUADALUPE
K: OBRAS PUBLICAS  DELEGACION MUNICIPAL DE CAPILLA DE GUADALUPE
   act 931210 · ratio=86 · dist=0m · tel 3787121260 idéntico · 14/093
   (el mismo K se quedó también con REGISTRO CIVIL y EGRESOS)

# 5. Dos personas morales del mismo grupo financiero, mismo piso
T: PENSIONES BBVA BANCOMER, S.A. DE C.V., GRUPO FINANCIERO BBVA BANCOMER
K: SEGUROS   BBVA BANCOMER, S.A. DE C.V., GRUPO FINANCIERO BBVA BANCOMER
   act 524110 · ratio=89 · dist=0m · CP 06600 · 09/015
```

El caso 4 es el más instructivo: como el teléfono es exacto, **Path C
también dispara** (nombre parcial + teléfono = 2 señales). Agregar señales
deterministas no ayuda si la elección entre candidatos sigue siendo "el
primero que pase".

### Implicación: el orden de los pendientes

1. ~~**`argmax` sobre los candidatos**~~ **hecho** (`_best_candidate` en
   `entity_resolution.py`): 95.90% → **97.72%** medido. Pendiente derivado:
   medir el costo en tiempo del loop sin `break` antes de comprometer la
   re-resolución de Silver.
2. **Asignación 1:1 para los 20,527 empates**, que es el pendiente #5 de
   `docs/clee_propagation.md` (las 6.3M colisiones intra-periodo). Los dos
   problemas son el mismo visto desde dos lados — si un candidato solo
   puede ser reclamado por un target, el empate se resuelve globalmente en
   vez de por fila. **No es un problema de señal sino de estructura**: ya
   se midió que nombre y distancia están agotados ahí, así que la única
   salida es la restricción de biyección, no una señal mejor. Ahora es el
   **99.7% del error restante**, así que es el único pendiente que mueve
   la aguja de forma material.
3. **Normalizar sufijos de forma legal** en razón social antes de
   comparar (`SA DE CV`, `S DE RL DE CV`, `SAPI DE CV`, con y sin puntos).
   Lo motivan las 2 regresiones documentadas arriba; es chico en volumen
   pero es higiene de datos y elimina un modo de falla conocido.
4. **Ponderación IDF del nombre**, útil sobre todo para los empates:
   `CONSULTORIO`, `UNIDAD MEDICA`, `DELEGACION MUNICIPAL` y `GRUPO
   FINANCIERO` son altísima frecuencia en este corpus y `DENTAL`,
   `EGRESOS`, `PENSIONES` son los discriminantes raros.
5. **Adjudicación semántica (LLM)**: queda al final y con alcance mucho
   menor del que parecía. No es que no sirva — el caso 2 muestra pares
   donde la similitud de caracteres contradice al significado — pero su
   techo se recalcula sobre el error que sobreviva a 1-4, no sobre los
   37,601 originales.

**La columna `clee_propagado_llm` no se descarta, se posterga**: con el
error medido en 2.28% en vez de 4.10%, y con el 99.7% de ese resto siendo
empates que el texto no puede resolver, la banda que valdría la pena
mandar a un modelo es mucho más chica de lo que parecía al inicio.

### Pendientes

- **Medir el costo en tiempo** del loop sin `break` sobre un corte
  completo antes de comprometer una re-resolución de ~17h por columna. El
  corte temprano al techo (`PERFECT_RAZON_SCORE` / `PERFECT_NAME_SCORE`)
  debería absorber buena parte, pero no está medido.
- **Re-resolver Silver** con `argmax` (ambas columnas) y re-correr
  `docker compose run --rm tools python scripts/clee_propagation_stats.py`. Ojo con la trampa del respaldo: no lanzar dos
  `--force` seguidos. Pendiente derivado: `docker compose run --rm etl build-entidades`.
- Las **50 filas donde el correcto puntea más alto pero no era candidato**
  apuntan a generación de candidatos (radio de 500 m / disco H3), no a la
  cascada. Vale aislar si son mudanzas de más de 500 m o un defecto del
  blocking.
- Revisar si `assign_new_clees` sufre el mismo sesgo de orden. (Los
  `match_espurio` y `falso_nuevo` ya se confirmaron idénticos con
  `argmax`, así que el sesgo no los afecta por esta vía.)
- Repetir la reconstrucción del triple (T, K, C) sobre
  `results_chain.parquet` **con argmax**: el experimento encadenado puede
  tener otra anatomía, porque ahí la referencia intermedia ya viene con
  error.
- Promover a `scripts/` los scripts de esta entrada si se van a citar en
  la tesis; hoy la lógica está documentada arriba pero no versionada.
