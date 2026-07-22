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
`make eval-masked-clee-extract && make eval-masked-clee`).

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

`make eval-masked-clee-chain` (subcomando `chain`): se enmascara TAMBIÉN
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
