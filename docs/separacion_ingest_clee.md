# Separar el ingest de la resolución CLEE

**Estado:** propuesto, sin implementar. Decidido el 2026-10-22 descartar la
alternativa del schema `bronze`; los pasos de abajo están pendientes de
arranque.

**Motivación:** que una falla de la propagación CLEE no pueda dañar la tabla
de hechos ni destruir el resultado bueno de la corrida anterior.

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

## 3. Diseño elegido: el resultado de CLEE en su propia tabla

```
silver.establecimientos   ← crudo tipado + derivadas en vuelo. La escribe SOLO el ingest.
silver.clee_resolucion    ← (id, period) → clee_radio, clee_h3. La escribe SOLO la resolución.
                            LIST (period), mismos límites que establecimientos.
```

La clave de join sale gratis: el PK de `establecimientos` es `(id, period)` y
**`id` es un hash del contenido de la fila**, así que es determinista y
reproducible — re-ingestar un periodo regenera los mismos ids y la tabla de
CLEE sigue válida.

### Qué gana

1. **Aislamiento de fallos.** La resolución no escribe la tabla de hechos.
2. **El resultado bueno sobrevive.** Se construye en staging y se engancha
   con `ATTACH PARTITION`: la partición vieja sigue consultable hasta que la
   nueva está completa. Nunca hay un instante con la columna en NULL.
3. **Angosta.** 82.9M × (hash + 2 CLEEs) son pocos GiB, no 100.
4. **Mata el `UPDATE` masivo** y el churn de VACUUM que arrastra.
5. **Recuperación por periodo** (§2.4): re-resolver un corte no toca los
   otros 19.
6. **Los dos tracks de comparación lado a lado**, que es lo que el diseño
   experimental necesita.

No contradice `plan.md` §5, que argumenta contra materializar **la dimensión**
(`entidades_economicas`) por método. Esto es la resolución **por
observación**, que ya está materializada hoy: solo cambia de tabla.

### Qué cuesta

- **Todo lector de `clee_propagado_radio` necesita un JOIN**: el `BUILD_SQL`
  de `loaders/entidades.py`, el matching, y los scripts de stats y
  evaluación. Es el trabajo real y donde puede aparecer un bug sutil.
- Mitigación: una vista `silver.establecimientos_resueltos` que haga el join,
  para no reescribir cada consulta de golpe. No por retrocompatibilidad
  —este proyecto no la necesita— sino para acotar el radio de explosión.
- El join es partition-wise por `period`, así que escala, pero las
  particiones deben declararse con los mismos límites.
- **Semántica de huérfanos:** si INEGI republica una fila corregida su hash
  cambia y la entrada de CLEE queda huérfana. Es correcto (contenido nuevo =
  observación nueva), pero la reconstrucción tiene que limpiar huérfanos o
  rehacer el periodo entero.

---

## 4. Pasos de implementación

Cada paso es un commit verificable por su cuenta.

- [ ] **1. Modelo y migración.** `silver.clee_resolucion` particionada por
      `LIST (period)`, con su migración. No toca `establecimientos` ni
      `meta.download_tasks`, así que los ZIPs descargados y su metadata
      quedan intactos.
- [ ] **2. La resolución escribe ahí.** Staging + `ATTACH PARTITION`, sin
      `UPDATE` y **sin reset previo**. Aquí muere el borrón de la fase
      determinista.
- [ ] **3. Vista puente y migración de lectores.** `silver.establecimientos_
      resueltos`, y mover `loaders/entidades.py`, el matching y los scripts
      uno por uno, cada cual con sus tests.
- [ ] **4. Migración final.** Tirar `clee_propagado_radio` y
      `clee_propagado_h3` de `establecimientos`, solo cuando ya nadie las
      lea.

**Hacerlo antes de la Fase 5.** Esto reescribe el contrato Silver→Gold, y
Gold todavía no existe: cambiarlo ahora es más barato que después.
