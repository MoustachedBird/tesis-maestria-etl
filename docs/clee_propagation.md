# Propagación de CLEE — resolución de identidad longitudinal

Cómo el pipeline asigna una identidad de negocio estable
(`clee_propagado_radio` / `clee_propagado_h3`) a los ~77M de
observaciones DENUE, los experimentos que validaron el diseño (Fase 7) y
los cambios derivados. Complementa `AGENTS.md` (referencia operativa) y
`docs/findings.md` (hallazgos crudos con fecha); este documento es la
explicación integrada.

## El problema

INEGI publica el CLEE (Clave Estadística de Establecimiento) solo desde
los cortes de 2021; los cortes 2015-2020 no traen identificador de
negocio. Sin identidad longitudinal no hay análisis de supervivencia/
mortalidad (la unidad de análisis de la tesis). La solución: **propagar
la identidad del futuro hacia el pasado** — un negocio observado en 2020
se busca en los cortes 2021+ que sí traen CLEE; si se encuentra, hereda
ese CLEE; si no, recibe una identidad sintética `NEW_*` que a su vez se
propaga a cortes más viejos.

## Cómo funciona el código actual

Orquestado por `CLEEResolver` (`src_etl/transformers/clee_resolver.py`).
Existen dos métodos paralelos que solo difieren en el generador de
candidatos espaciales y llenan columnas separadas para compararse:
`clee_propagado_radio` (BallTree, baseline) y `clee_propagado_h3`
(blocking H3). La cascada de matching es la misma; la paridad de
asignación está verificada (0 diferencias).

### Fase 1 — determinística

Los periodos con cobertura de `clee` oficial completa (2021+) se
"siembran" directo: `clee_propagado_* = clee`. Sin matching. Estos
periodos forman el pool inicial de referencias (`resolved_periods`).

### Fase 2 — probabilística (ventana deslizante)

```
FOR each target P sin CLEE (targets NEWEST-FIRST, ej. 2020 → 2015):
    usable_refs = referencias ya resueltas FUTURAS a P,
                  ordenadas NEAREST-FIRST (más cercana temporalmente primero)
    FOR each ref R in usable_refs:
        1. construir KB efímera de R (build_single_period_kb)
        2. propagar identidades R → P (candidatos espaciales + cascada)
        3. liberar KB; si P no tiene pendientes → break
    filas sin match → NEW_<prefijo>-uuid (assign_new_clees)
    escribir P a DuckDB; P entra al pool de referencias
```

Dos órdenes distintos que no hay que confundir:

- **Targets: newest-first.** Es la dirección futuro→pasado del diseño —
  2020 se resuelve antes que 2019 para que 2019 pueda usar al 2020
  recién resuelto (con CLEEs heredados + `NEW_*`) como referencia. Esto
  produce la propagación transitiva "en cadena" hasta poblar toda la DB.
- **Referencias dentro de un target: nearest-first.** Para cada target
  se prueban primero las referencias temporalmente más cercanas. Este
  orden era newest-first hasta 2026-07-21; el cambio lo motivó la
  evidencia de Fase 7 (ver abajo) y es la decisión de diseño central de
  este documento.

### Generación de candidatos (pre-filtro espacial)

Solo se evalúan en la cascada los negocios de la KB a menos de
`SPATIAL_RADIUS_M` (500 m, cubre mudanzas dentro del barrio):

- **Radio**: BallTree con métrica haversine (`find_spatial_neighbors`).
- **H3**: blocking por celda res 9 (~174 m de arista) + `grid_disk` k=3
  + post-filtro haversine exacto (`find_h3_neighbors`). k=3 garantiza
  cubrir 500 m en el peor caso; un guard en
  `clee_resolution_config.py` impide subir el radio sin recalibrar k.

### Cascada de matching (`calculate_match_score`, `src_etl/utils/matching.py`)

Por cada par target-candidato, en orden:

1. **GATE obligatorio** — actividad SCIAN fuzzy > `ACTIVITY_GATE_THRESH`
   (60). Si el giro no se parece, el candidato se descarta sin evaluar
   nada más.
2. **Path A** — razón social fuzzy (token_sort) >
   `LEGAL_NAME_MATCH_THRESH` (80) → match directo.
3. **Path B** — nombre comercial fuzzy >
   `BUSINESS_NAME_MATCH_THRESH` (75) → match directo.
4. **Path C (combinado)** — si ningún campo basta solo, se cuentan
   señales débiles: nombre parcial (> `PARTIAL_NAME_THRESH`, 60),
   teléfono exacto (≥7 dígitos), email exacto, web exacta, CP igual.
   Con `MIN_COMBINED_SIGNALS` (2) o más → match.

Todos los umbrales son calibración vía `.env` (documentados en
`.env.example`); los datos son de encuesta, así que cualquier campo
puede venir vacío o faltar en un periodo entero (se trata como "").

### Recursos (M4, RAM acotada)

- KB de referencia con más de `KB_MAX_ROWS_PER_PARTITION` (1.5M) filas
  se particiona y procesa secuencialmente — mismo resultado, menos RAM.
- Matching paralelo con `MATCH_WORKERS` (8) procesos vía fork+COW
  (`gc.freeze()` para minimizar copias); secuencial debajo de
  `MATCH_PARALLEL_THRESHOLD` (50k filas pendientes).
- Una sola referencia en RAM a la vez; KBs cacheadas a parquet y
  la caché se invalida cuando el periodo cambia de identidad.

## Fase 7 — los experimentos (2026-07-20/21)

Protocolo completo en `todo.md`; script
`scripts/evaluate_masked_clee.py` (targets de make
`eval-masked-clee-extract`, `eval-masked-clee`, `eval-masked-clee-chain`).

**Idea**: usar el CLEE oficial como ground truth. Se toma `05_2021`
(el corte más viejo con CLEE al 100%, muestra: CDMX/Jalisco/Zacatecas,
920,699 filas), se le **enmascara** el CLEE y se resuelve con la
maquinaria de producción contra las 7 referencias oficiales futuras
(2021-11 … 2025-05). Todo 100% aislado de Silver (parquets en
`data/eval/`, lectura read-only).

### Qué cuenta como acierto y cómo se mide el error

INEGI asigna el mismo CLEE al mismo establecimiento en todos los
cortes, así que el CLEE oficial escondido es verdad absoluta —
**acierto = el CLEE asignado a ciegas es exactamente el CLEE oficial
que la fila tenía escondido**. No hay juicio subjetivo. (En el
experimento encadenado hay un acierto adicional: una identidad
sintética `NEW_*` heredada del intermedio cuenta como correcta solo si
pertenece — según el CLEE oficial del intermedio — al mismo negocio.)

La clasificación separa primero dos mundos, porque "lo correcto" es
distinto en cada uno (esta partición es la que evita inflar métricas):

| Mundo | Categoría | Significado |
|---|---|---|
| **Evaluables** (la verdad SÍ existe en alguna referencia — había respuesta que encontrar) | `correcto` | le asignó su CLEE verdadero |
| | `falso_match` | le asignó el CLEE de OTRO negocio — el error grave: contamina supervivencia (dos negocios distintos cuentan como uno que "sobrevivió") |
| | `falso_nuevo` | dijo `NEW_*` cuando la respuesta estaba disponible — fragmenta un negocio real en dos identidades |
| **No evaluables** (la verdad NO existe en ninguna referencia — el negocio murió antes; lo único correcto es "nuevo") | `nuevo_correcto` | asignó `NEW_*`: no inventó nada |
| | `match_espurio` | matcheó algo de todas formas — mide cuánto "alucina" la cascada cuando no hay nada que encontrar |

Métricas derivadas:

- **Precision** = `correcto / (correcto + falso_match + match_espurio)` —
  de las identidades que asignaste, ¿qué fracción apunta al negocio
  correcto? Es la métrica que le importa a la calidad de la DB.
- **Recall** = `correcto / evaluables` — de los negocios que sí estaban
  ahí para encontrarse, ¿qué fracción encontraste?
- **Tasa de espurios** = `match_espurio / no_evaluables` — 70.5% en
  todas las corridas (alta, pero sobre solo 515 filas); es la métrica a
  vigilar si algún día se relajan umbrales.

### Bug encontrado en el camino (corregido)

La primera corrida atribuía "qué referencia resolvió cada fila" por
posición de fila, pero `propagate_clees_radio` devuelve el DataFrame
reordenado (resueltas primero) — la atribución quedaba barajada desde la
segunda referencia (el motor reportaba 11,719 matches y el script
anotaba +156,697). Las métricas globales nunca estuvieron afectadas (la
clasificación se hace por join en `id`); la segmentación por referencia
sí. Fix: atribución por columnas ("recién resuelta" = identidad no nula
y referencia aún nula). Lección para cualquier código sobre este motor:
**nunca asumir que la propagación preserva el orden de filas**.

### Experimento 1 — newest-first (el orden legado de producción)

Precision/recall global: **71.3%**. Segmentado por referencia que
resolvió cada fila:

| Referencia | Distancia | Filas | Precision |
|---|---|---|---|
| 2021-11 | 6 meses | 344 | 99.7% |
| 2022-05 | 1 año | 730 | 96.9% |
| 2022-11 | 1.5 años | 6,533 | 97.2% |
| 2023-11 | 2.5 años | 3,856 | 88.5% |
| 2024-05 | 3 años | 168,537 | 98.2% |
| 2024-11 | 3.5 años | 11,719 | 62.2% |
| 2025-05 | 4 años | **728,566** | **64.9%** |

La referencia más lejana (2025-05, probada primero por ser la más
nueva) acapara el 79% del volumen con ~65% de precisión. Consistente
con la mortalidad empresarial (~50% en los primeros 2 años): a 4 años
la mayoría de los negocios de 2021 ya no existe y su local/zona está
ocupado por otro del mismo giro → falso match; a 6 meses siguen vivos.

### Experimento 2 — nearest-first directo

Mismos umbrales, solo se invierte el orden de referencias:
precision/recall global **95.9%**. La referencia a 6 meses resuelve el
99.97% del volumen. Los `falso_nuevo` (262) y `match_espurio` (363) son
idénticos a los de newest-first: el orden no cambia si una fila
matchea, solo contra quién — todo el delta (+24.6 pts) son falsos match
convertidos en correctos.

### Experimento 3 — nearest-first encadenado (dos saltos)

El caveat del experimento 2: en producción las referencias cercanas de
un target 2015-2020 son periodos resueltos *probabilísticamente*, no
oficiales. Para medir el error transitivo se enmascaró TAMBIÉN el
intermedio 2021-11, se resolvió nearest-first contra 2022-05+, y el
target se resolvió usando ese intermedio resuelto como primera
referencia (nunca vio el CLEE oficial de 2021-11):

| Estrategia | Precision | Recall |
|---|---|---|
| newest-first (legado) | 71.3% | 71.3% |
| nearest-first directo | 95.9% | 95.9% |
| nearest-first encadenado | **95.7%** | **95.7%** |

El salto por el intermedio probabilístico cuesta **~0.15 puntos**. El
error se hereda casi 1:1, no se amplifica: 32,033 de los errores del
encadenado venían mal resueltos del paso 1 (el target matcheó la fila
correcta del intermedio, que estaba mal resuelta) y solo 6,984 fueron
errores propios del salto. Las identidades sintéticas también encadenan
bien: 344 filas quedaron correctamente vinculadas vía `NEW_*` heredado.

### Conclusión

**Ganador: nearest-first.** El mejor número absoluto es el directo
(95.9%), pero el resultado que manda para producción es el
**encadenado (95.7%)**, porque replica el escenario real: los targets
2015-2020 matchean contra periodos resueltos probabilísticamente, no
contra oficiales. A 0.15 pts del directo y +24.4 pts sobre newest-first,
la decisión no tiene ambigüedad.

El porqué es la mortalidad empresarial: contra la referencia a 6 meses
el negocio sigue vivo y matchea consigo mismo; a 4 años, ~la mitad ya
murió y la cascada matchea al "inquilino nuevo" del local. La prueba
limpia de que el orden solo cambia *contra quién* se matchea (no *si*
se matchea): `falso_nuevo` (262) y `match_espurio` (363) son idénticos
en los tres experimentos — todo el delta son falsos match convertidos
en correctos.

En general: la **distancia temporal target-referencia domina la
precisión** — más que cualquier calibración de umbrales (misma cascada:
+24.6 pts solo por el orden). Y la propagación transitiva del diseño es
viable: ~0.15 pts por salto contra ~31 pts por saltar 4 años directo.

## El algoritmo ganador explicado con negocios reales

Cómo funciona nearest-first encadenado, paso a paso, con casos reales
de DENUE tomados del experimento (muestra CDMX/Jalisco/Zacatecas; CLEEs
abreviados por legibilidad).

### El caso feliz: el negocio sigue vivo

Un mayorista de frutas y verduras de Jalisco observado en 05_2021 (CLEE
enmascarado para el experimento). El algoritmo busca dentro de 500 m en
la referencia MÁS CERCANA en el tiempo (2021-11, 6 meses después):
encuentra un negocio con el mismo giro (pasa el gate SCIAN) y el mismo
nombre → hereda su CLEE `14039431130006281…U6`. Al destapar la verdad:
era exactamente su CLEE oficial → `correcto`. Así se resolvió el 99.97%
del volumen en el experimento, contra la primera referencia.

### El error típico: el "inquilino nuevo" (falso_match)

Caso real: un **CONSULTORIO DE GASTROENTEROLOGÍA** (consultorios de
medicina especializada) matcheó contra un **CONSULTORIO DE UROLOGÍA**
(mismo giro SCIAN, mismo edificio médico, a metros de distancia). Otro:
el **CONSULTORIO DE NUTRICIÓN de la UNIDAD MÉDICA SAN ÁNGEL** matcheó
contra el **CONSULTORIO DENTAL de la misma UNIDAD MÉDICA SAN ÁNGEL**
(el nombre comparte casi todas las palabras → el fuzzy lo deja pasar).

Anatomía del error: misma zona (pasa el filtro de 500 m), mismo giro
(pasa el gate), nombre parecido (pasa la cascada) — pero es OTRO
negocio. Este error explota con la distancia temporal: si el
gastroenterólogo cerró hace 4 años y hoy ahí atiende el urólogo, la
referencia lejana solo ofrece al urólogo como candidato. Con la
referencia a 6 meses, el gastroenterólogo sigue ahí y matchea consigo
mismo antes de que nadie más pueda "robarse" la fila. Por eso
nearest-first: 95.9% vs 64.9% contra la referencia de 4 años.

### El negocio que murió: NEW_* correcto

Caso real: el **CENTRO INTERACTIVO MOLINO DE SANTO DOMINGO** (CDMX)
existe en 05_2021 pero en NINGUNA referencia posterior — cerró antes de
2021-11. El algoritmo no encuentra match y le asigna `NEW_EVAL-d299…`.
Eso es `nuevo_correcto`: la identidad sintética ES la respuesta
correcta, no un fallo. (El fallo inverso también existe y es raro —
262 de 920k: la **IGLESIA BAUTISTA LLUVIAS DE GRACIA** sí seguía viva
en las referencias pero la cascada no la encontró → `falso_nuevo`.)

### La cadena: identidad sin CLEE oficial jamás

El caso que motiva todo el diseño (ilustrativo, con la mecánica exacta
de producción). Una taquería que vivió 2015-2019 y cerró en 2020 —
nunca existió en los cortes 2021+ que traen CLEE oficial:

1. Se resuelve el target 2020 (el más nuevo sin CLEE): la taquería no
   está (ya cerró) — no aparece en este paso.
2. Se resuelve 2019: su primera referencia es el 2020 recién resuelto,
   luego 2021-05, etc. La taquería no matchea en ninguna → recibe
   `NEW_MX-abc123`. El 2019 completo (CLEEs heredados + sintéticos)
   entra al pool de referencias.
3. Se resuelve 2018: primera referencia = 2019 (la más cercana). La
   taquería de 2018 matchea contra su fila de 2019 → hereda
   `NEW_MX-abc123`.
4. 2017, 2016 y 2015 repiten el paso 3, cada uno contra su vecino
   inmediato.

Resultado: las 5 observaciones (2015-2019) comparten UNA identidad
(`NEW_MX-abc123`) con primer periodo 2015 y último 2019 — exactamente
lo que la dimensión `entidades_economicas` necesita para calcular
supervivencia y mortalidad, aunque INEGI nunca le haya dado CLEE. El
experimento de dos saltos midió que cada eslabón de esta cadena cuesta
~0.15 pts de precisión (los 344 `correcto_new_heredado` del experimento
son exactamente este mecanismo funcionando).

### El recorrido con los periodos reales

Silver tiene 19 cortes DENUE. Fase 1 siembra los 8 con CLEE oficial
(`2021-05 … 2025-05`). Fase 2 **empieza por 2020-11 y termina en
2015-01**, y cada target prueba sus referencias de la más cercana a la
más lejana:

| # | Target | 1ª referencia (más cercana) | Distancia | Y luego… |
|---|---|---|---|---|
| 1 | **2020-11** | 2021-05 (oficial) | 6 meses | 2021-11 … 2025-05 |
| 2 | 2020-04 | 2020-11 (recién resuelto) | 7 meses | 2021-05 … |
| 3 | 2019-11 | 2020-04 (resuelto) | 5 meses | 2020-11 … |
| 4 | 2019-04 | 2019-11 (resuelto) | 7 meses | … |
| 5 | 2018-11 | 2019-04 (resuelto) | 5 meses | … |
| 6 | 2018-03 | 2018-11 (resuelto) | 8 meses | … |
| 7 | 2017-11 | 2018-03 (resuelto) | 4 meses | … |
| 8 | 2017-03 | 2017-11 (resuelto) | 8 meses | … |
| 9 | 2016-10 | 2017-03 (resuelto) | 5 meses | … |
| 10 | 2016-01 | 2016-10 (resuelto) | 9 meses | … |
| 11 | **2015-01** (final) | 2016-01 (resuelto) | 12 meses | …hasta 2025-05 (18 refs) |

Dos observaciones clave:

- **Solo el primer target (2020-11) matchea directo contra un corte
  oficial**; del #2 en adelante la referencia más cercana siempre es un
  periodo resuelto probabilísticamente — el escenario que validó el
  experimento de encadenamiento. Los saltos reales (4-12 meses) son
  incluso más cortos que el del experimento (6 meses).
- **El contraste con el orden legado es máximo en los periodos viejos**:
  con newest-first, una fila de 2015-01 se ofrecía primero a la
  referencia 2025-05 — 10 años de distancia, el régimen donde se midió
  ~65% de precisión. Con nearest-first su primera oportunidad está a 12
  meses, y como la primera referencia agota ~99.9% del pending, las
  restantes casi no procesan filas (de ahí que el `--force` deba correr
  además más rápido).

## Cambios

### Implementados (2026-07-21)

1. **Nearest-first en producción** (`clee_resolver.py`): `usable_refs`
   ordena las referencias futuras al target por cercanía temporal
   ascendente. Además filtra referencias más antiguas que el target y
   re-ordena el pool en cada target (antes, un periodo que entraba al
   pool fuera de orden se consultaba al final).
2. **Fix del bug de atribución** en el script de evaluación (por
   columnas, no por posición) + opción `--ref-order` (default `nearest`
   = análogo de producción) + subcomando `chain`.
3. Documentación: `AGENTS.md` (pseudocódigo, tips, sección Fase 7),
   `docs/findings.md` (tabla corregida + entradas 2026-07-20/21),
   targets de make.

### Sugeridos / pendientes

1. **Re-resolución completa** (requisito, no opcional): las columnas
   `clee_propagado_*` actuales se generaron con el orden legado.
   `make clee-resolution-radio ARGS="--force"` (y el equivalente H3),
   después `make build-entidades`. Bonus esperado: el force debería ser
   más rápido — con nearest-first la primera referencia agota ~99.9%
   del pending y las demás casi no procesan filas.
2. **Repetir el protocolo con target `11_2022`** (referencias a ambos
   lados) para medir sensibilidad del hallazgo a la posición del target
   — contemplado en el protocolo original, no corrido.
3. **Cadenas más profundas**: el experimento midió 1 salto intermedio
   (~0.15 pts). Para targets 2015 la cadena real tendrá 5+ saltos; si
   el costo es aditivo (~0.8 pts acumulados) sigue siendo despreciable,
   pero puede verificarse extendiendo `chain` a N intermedios.
4. **Optimización de RAM/tiempo aún abierta** (independiente del
   orden): particionado de KB por `h3_cell_5` (ya materializada) para
   podar particiones enteras fuera del disco k=1 del target — diseñada
   en `clee_resolution_config.py` (docstring de `H3_PARTITION_RES`),
   no implementada.
