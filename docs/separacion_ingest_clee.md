# Separar el ingest de la resolución CLEE

**Estado:** en curso. Pasos 1 y 2 hechos; 3 y 4 pendientes. **La
verificación contra base real no ha ocurrido**: la Mac no tiene Docker, así
que los 154 tests marcados `postgres` solo corren en indominus. El ingest y la
resolución **no se corren hasta terminarlos**: no tiene sentido gastar horas
de CLEE sobre un esquema que va a cambiar.

Decidido el 2026-10-22: se descarta el schema `bronze` y se va a **una tabla
por método de resolución**.

**Motivación:** que una falla de la propagación CLEE no pueda dañar la tabla
de hechos, ni destruir el resultado bueno de la corrida anterior, ni
corromper el resultado de los otros métodos de resolución.

---

## 1. Diagnóstico: qué pasa hoy

`silver.establecimientos` contiene, en la misma tabla:

| | |
|---|---|
| Crudo tipado de INEGI | `nombre`, `latitud`, `longitud`, `codigo_act`, `clee`, … |
| Derivadas en vuelo por el ingest | `fecha_alta_date`, `h3_cell_9`, `h3_cell_5` |
| Derivadas por la resolución CLEE | `clee_propagado_radio`, `clee_propagado_h3` |

Dos procesos escriben la misma tabla, y el segundo lo hace con `UPDATE`.

### Lo que NO es un problema

**Una falla de CLEE no obliga a releer los ZIPs.** La propagación solo
escribe sus dos columnas, con tabla temporal + `UPDATE ... FROM` acotado por
periodo (`transformers/clee_resolver.py`, `_write_period_results`). Las
columnas crudas quedan intactas: si CLEE revienta se pierde el resultado de
CLEE, no los datos del censo.

### Lo que sí es un problema

1. **Destruir antes de reconstruir.** Con `--force`, la fase determinista
   ejecuta `UPDATE establecimientos SET <columna> = NULL` sobre la tabla
   entera antes de recalcular (`_resolve_deterministic_periods`). Una corrida
   que muere a mitad deja el resultado anterior ya borrado. Así se perdió una
   resolución radio completa de 31,392,164 filas el 2026-07-26 (1h48 de
   cómputo).
2. **`UPDATE` masivo sobre una tabla ancha.** Actualizar una columna derivada
   en 82.9M filas reescribe tuplas completas y deja muertos para VACUUM.
   Contradice `arquitectura.md` §4.2, que manda `INSERT` + swap.
3. **Dominios de fallo acoplados.** El ingest y la resolución comparten
   tabla, así que nada estructural impide que un bug de uno afecte al otro.

---

## 2. Alternativa descartada: schema `bronze` en la base

Leer los CSV/ZIP a un `bronze.establecimientos` con el crudo tipado, y que
silver derive la propagación desde ahí.

**Por qué no:**

- `silver.establecimientos` menos las dos columnas de CLEE **ya es** el crudo
  tipado. Un bronze en la base duplicaría 82.9M filas (~100+ GiB) para
  protegerse de una pérdida que no ocurre.
- La capa bronze ya existe y es inmutable: **los 484 ZIPs en disco**.
  Releerlos cuesta tiempo, no información.
- No arregla el problema de fondo: con bronze, un `--force` de CLEE seguiría
  nulificando la columna en silver antes de recalcular.

---

## 3. Diseño elegido: una tabla por método de resolución

```
silver.establecimientos   ← crudo tipado + derivadas en vuelo. La escribe SOLO el ingest.
silver.clee_radio         ← (id, period) → clee_resuelto + evidencia del método radio/BallTree
silver.clee_h3            ← (id, period) → clee_resuelto + evidencia del blocking H3
silver.clee_llm           ← (futura) lo mismo para la resolución por LLM
```

Todas particionadas por `LIST (period)` con los mismos límites que
`establecimientos`, para que el join sea partition-wise y la recuperación
siga siendo por corte.

### La clave: por qué `(id, period)` si `id` ya es único

`id` **es** único globalmente, y no por suerte: `period` es uno de los cinco
componentes del hash (`clee`, `nom_estab`, `raz_social`, `codigo_act`,
`period`, en `transformers/processor.py`). El mismo negocio censado en dos
cortes produce dos `id` distintos por construcción; sin `period` dentro del
hash, dos filas idénticas en cortes distintos colisionarían.

Y como el hash es determinista, re-ingestar un periodo regenera los mismos
ids y las tablas de CLEE siguen válidas.

`period` va en la clave por dos razones **físicas**, no de unicidad:

1. **Postgres lo exige.** En una tabla particionada la clave de partición
   tiene que estar contenida en toda PK/UNIQUE, y no admite un índice único
   global que la excluya (ver el comentario del PK en
   `shared/db/models/silver.py` y `arquitectura.md` Fase 3.2).
2. **Sin `period` no hay partition-wise join.** Unir por `id` solo obligaría
   al planeador a cruzar las 20 particiones de un lado contra las 20 del
   otro sin emparejarlas: un hash join de 82.9M × 82.9M en vez de 20 joins
   de ~4M. Y una consulta de un corte no podría hacer pruning en ambos
   lados.

Particionar es lo que da el reemplazo atómico por `ATTACH PARTITION`, que es
el mecanismo central de este diseño. Sin particionar, re-resolver un corte
sería `DELETE WHERE period = X` + `INSERT`: ventana sin datos, muertos para
VACUUM, y de vuelta al problema del que se huye.

Costo de llevar la columna: ~4 bytes por fila (`DATE`), unos 330 MB por tabla
de método. Y no es redundancia dañina — `period` está determinado
funcionalmente por `id`, así que almacenarlo es desnormalización por layout
físico, no ambigüedad de modelo.

### Por qué una tabla por método y no columnas en una sola

Decidido el 2026-10-22. Con `clee_radio` y `clee_h3` como columnas de una
misma tabla, re-resolver un método exige construir la partición nueva
**acarreando** los valores del otro: un read-modify-write donde un bug en el
acarreo pisa el método que no se estaba tocando. El swap atómico protege
contra una corrida a medias, no contra eso.

Y hay una razón más fuerte que el aislamiento: **los métodos no tienen la
misma evidencia.** Radio guarda distancia y score de similitud; H3 guarda
celda y anillo; un método LLM guardaría modelo, versión del prompt,
confianza y costo. En una sola tabla eso es ancho lleno de NULLs, o la
pérdida de la procedencia por método — que es justo lo que la comparación de
la tesis tiene que poder defender.

### Qué gana

1. **Aislamiento por construcción.** Cada escritor toca solo su tabla. No hay
   lógica de acarreo que pueda clobberear a un método vecino.
2. **El resultado bueno sobrevive.** Staging + `ATTACH PARTITION`: la
   partición vieja sigue consultable hasta que la nueva está completa. Nunca
   hay un instante con la columna en NULL.
3. **Agregar un método es aditivo**: tabla nueva + migración. Con columnas
   sería `ALTER TABLE` sobre una particionada y reconstruir lo existente.
4. **Tirar un método es `DROP TABLE`**, sin tocar los otros.
5. **Evidencia tipada por método**, no un JSONB genérico.
6. **Recuperación por periodo y por método**: re-resolver radio en `05_2024`
   no toca ni los otros 19 cortes ni H3.
7. **Mata el `UPDATE` masivo** sobre la tabla de hechos y el churn de VACUUM
   que arrastra.

### Qué cuesta

- **Duplicación de esquema**: el núcleo `(id, period, clee_resuelto)` se
  repite. Se declara una vez en un mixin de SQLAlchemy y cada tabla agrega
  sus columnas de evidencia.
- **Más particiones**: 3 métodos × 20 cortes = 60. Postgres lo lleva, y
  `PG_MAX_LOCKS_PER_TRANSACTION=256` ya está subido por los planes que tocan
  muchas particiones, pero hay que recordarlo si los métodos crecen.
- **Todo lector de `clee_propagado_radio` necesita un JOIN**: el `BUILD_SQL`
  de `loaders/entidades.py`, el matching, y los scripts de stats y
  evaluación. Es el trabajo real y donde puede aparecer un bug sutil.
- Mitigación: una vista por método (`silver.establecimientos_radio`) que haga
  el join, para no reescribir cada consulta de golpe. No por
  retrocompatibilidad —este proyecto no la necesita— sino para acotar el
  radio de explosión.
- **Semántica de huérfanos:** si INEGI republica una fila corregida su hash
  cambia y la entrada de CLEE queda huérfana. Es correcto (contenido nuevo =
  observación nueva), pero la reconstrucción tiene que limpiar huérfanos o
  rehacer el periodo entero.

### La comparación entre métodos

Una vista `UNION ALL` deja la consulta de `plan.md` §5 igual que hoy: en vez
de parametrizar la columna, se filtra por `metodo`.

```sql
CREATE VIEW silver.clee_resuelto AS
  SELECT 'radio' AS metodo, id, period, clee_resuelto FROM silver.clee_radio
  UNION ALL
  SELECT 'h3'    AS metodo, id, period, clee_resuelto FROM silver.clee_h3;
```

Las columnas de evidencia quedan fuera de la vista, en su tabla, donde se
consultan cuando se quiere auditar un método concreto.

No contradice `plan.md` §5, que argumenta contra materializar **la dimensión**
(`entidades_economicas`) por método. Esto es la resolución **por
observación**, que ya está materializada hoy: solo cambia de tabla.

---

## 4. Pasos de implementación

Cada paso es un commit verificable por su cuenta.

- [x] **1. Mixin y las dos tablas** (2026-10-22, revisión
      `b0002clee_por_metodo`). El núcleo `(id, period,
      clee_resuelto)` en un mixin de SQLAlchemy; `silver.clee_radio` y
      `silver.clee_h3` particionadas por `LIST (period)`, cada una con sus
      columnas de evidencia. No toca `establecimientos` ni
      `meta.download_tasks`, así que los ZIPs descargados y su metadata
      quedan intactos.

      Las columnas de evidencia quedaron **fuera** a propósito: hoy
      `_best_candidate` calcula el score y lo descarta, así que declararlas
      ahora serían columnas condenadas a NULL. Entran en el paso 2, con el
      código que las llena.

      Lo que sí entró y hoy se pierde: `origen` (`OrigenClee`: DIRECTO,
      PROPAGADO, NUEVO). Separa lo que INEGI publicó de lo que el pipeline
      infirió por similitud y de lo sintético — hoy solo se podía adivinar
      con un `starts_with(id, 'NEW_')`. Para una tesis que compara métodos
      de resolución, esa distinción es el dato.
- [x] **2. Cada resolución escribe su tabla** (2026-10-22). Staging + `ATTACH PARTITION`,
      sin `UPDATE` y **sin reset previo**. Aquí muere el borrón de la fase
      determinista.
- [ ] **3. Vistas puente y migración de lectores.** Una vista por método
      para el join, más `silver.clee_resuelto` para la comparación; y mover
      `loaders/entidades.py`, el matching y los scripts uno por uno, cada
      cual con sus tests.
- [ ] **4. Migración final.** Tirar `clee_propagado_radio` y
      `clee_propagado_h3` de `establecimientos`, solo cuando ya nadie las
      lea.

**Hacerlo antes de la Fase 5.** Esto reescribe el contrato Silver→Gold, y
Gold todavía no existe: cambiarlo ahora es más barato que después.
