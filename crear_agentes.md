# Plan — adjudicador LLM para la resolución CLEE

**Estado:** propuesto, nada implementado. Decidido el 2026-10-26 escribirlo
antes de tocar código.

**Qué entrega:** una tercera tabla de resolución, `silver.clee_llm`, poblada
con un modelo corriendo en Ollama sobre la RTX 5090 de indominus, comparable
contra `clee_radio` y `clee_h3` con el mismo arnés de evaluación.

---

## 1. El techo está medido, y es acotado

Antes de diseñar nada: `plan.md` §2 ya cuantificó dónde está el error que
queda, y eso decide el alcance.

Error total tras `argmax`: **2.31%** de las 920,699 filas de la muestra
enmascarada. De los 20,579 `falso_match` con ambos registros localizables:

| | Filas | % | Quién lo puede arreglar |
|---|---:|---:|---|
| Features idénticas (gemelos reales) | 11,577 | 56.3% | **Solo asignación 1:1** |
| Alguna feature difiere | 9,002 | 43.7% | Un scorer mejor |

**Un LLM no puede tocar el 56.3%.** Esos casos tienen nombre literalmente
idéntico (97.8%) y están a la misma distancia exacta (99.3%): no hay señal
que interpretar, los inputs son iguales. Ningún modelo —LLM, XGBoost o
humano— distingue dos registros idénticos. Eso lo arregla la asignación
global 1:1 de `plan.md` §4, que es otro problema.

El LLM compite por el **43.7%**, y el techo proyectado de un scorer mejor es
**98.70% contra 97.72% actual: +0.98 puntos.**

### Por qué un LLM y no XGBoost

`arquitectura.md` §2.3.3 ya nombra al candidato natural para ese hueco: un
scorer aprendido (XGBoost/LightGBM con CUDA) más *embeddings de nombres de
negocio* "para los casos donde el nombre difiere pero el negocio es el mismo
— es la señal que al scorer actual le falta".

Esa frase es exactamente el caso de uso del LLM:

```
nom_estab A: "TAQUERIA EL FOGON"
nom_estab B: "TACOS EL FOGON SA DE CV"
rapidfuzz: ratio bajo  →  no match
humano:    obviamente el mismo negocio
```

| | LLM vía Ollama | XGBoost sobre features |
|---|---|---|
| Datos de entrenamiento | **ninguno** | hace falta etiquetar |
| Semántica de nombres | sí, nativa | solo vía embeddings aparte |
| Costo por decisión | ~50–200 ms | microsegundos |
| Reproducibilidad | hay que forzarla | determinista |
| Defendible en la tesis | sí, si se versiona todo | sí |

La ventaja decisiva del LLM es que **no necesita ground truth para
arrancar**, y el ground truth es justo lo que no tienes para 82.9M filas.
La desventaja es el costo por decisión, y eso obliga a targetear.

**Los dos no son excluyentes.** El LLM puede generar las etiquetas con las
que después se entrena el scorer barato. Esa es probablemente la ruta más
fuerte para la tesis, pero es la fase 2 de esto.

---

## 2. Es un adjudicador, no un agente

Pediste "agentes", y el nombre de la carpeta se queda, pero lo que sirve
aquí **no** es un agente en el sentido de tool-use y pasos libres:

- Un agente que puede decidir cuántas herramientas llama y en qué orden
  **no es reproducible**. Para una tesis eso es fatal: el revisor pide
  re-correr y los números cambian.
- El trabajo es una clasificación binaria con contexto corto: dado un
  registro y 1..N candidatos vecinos, ¿cuál es el mismo negocio, o ninguno?
- Sin pasos, sin herramientas, sin memoria entre llamadas: prompt fijo,
  salida estructurada, `temperature=0`, `seed` fijo.

**El LLM no genera candidatos.** Eso ya lo hacen BallTree y H3 en
microsegundos; pedirle a un modelo que busque en 82.9M filas es absurdo. El
LLM solo **decide** entre candidatos que la etapa espacial ya acotó.

### Dónde se inserta

```
_propagate_single_kb(df_pending, kb, tol, columna, neighbor_finder)
  └─ por fila: vecinos = neighbor_finder(...)      ← no cambia
       └─ _best_candidate(vecinos)                 ← AQUÍ
            ├─ calculate_match_score (rapidfuzz)   ← decide la mayoría
            └─ si queda en la banda ambigua  →  adjudicador LLM
```

El punto de inserción es `_best_candidate` de
`src/etl/utils/entity_resolution.py`, no el generador de vecinos. Hoy esa
función calcula `best_score` y lo descarta (ver
`docs/separacion_ingest_clee.md` §4, paso 1): hay que empezar por
devolverlo, porque **el score es lo que define la banda ambigua**.

### La banda ambigua

El LLM solo se invoca cuando el scorer determinista no decide con holgura:

```
score >= UMBRAL_ALTO   →  match, sin LLM
score <= UMBRAL_BAJO   →  no match, sin LLM
en medio               →  LLM adjudica
```

Los dos umbrales son calibración (`.env`), y su ancho controla
directamente el costo de la corrida. **Elegirlos es una medición, no una
corazonada** — es el paso 1 de la §6.

---

## 3. Organización de carpetas

Sí, va en `src/`. Dos paquetes, por una razón concreta:

```
src/
├── shared/
│   └── llm/                      ← infraestructura: no sabe qué es un CLEE
│       ├── __init__.py
│       ├── client.py             ← HTTP a Ollama, deadline total, reintentos
│       ├── schema.py             ← salida estructurada + validación
│       └── cache.py              ← memoización por hash del prompt
└── etl/
    └── agents/                   ← deciden sobre datos del ETL
        ├── __init__.py
        ├── clee_adjudicador.py   ← (registro, candidatos) -> decisión
        └── prompts/
            └── clee_v1.txt       ← versionado; su hash va en la tabla
```

**Por qué `shared/llm` y no `etl/llm`:** el cliente es infraestructura, como
`shared/db`. Si mañana el API quiere un endpoint "explica este match",
necesita el cliente sin importar `src.etl` — y eso está prohibido por
`tests/unit/test_layer_boundaries.py`.

**Por qué `etl/agents` y no `src/agents`:** el adjudicador usa las utilidades
de matching de `src.etl.utils`. Un paquete de primer nivel tendría que
importar de `src.etl` (prohibido en la dirección contraria) o duplicarlas.
Dentro de `etl/` respeta las fronteras que ya existen.

**Detalle útil:** si creas `src/agents/` sin declarar sus fronteras, el test
`test_los_paquetes_declarados_cubren_src` **falla a propósito** — obliga a
decidir qué puede importar antes de escribir código. Si eliges ese camino,
hay que agregar la entrada a `PROHIBIDOS`.

---

## 4. La tabla `silver.clee_llm`

`src/shared/db/models/clee.py` ya la anticipa. Hereda `ResolucionCleeMixin`
—`(id, period)`, `clee_resuelto`, `origen`, `resuelto_en`, `LIST (period)`—
y agrega **solo lo que el método produce**:

| columna | por qué |
|---|---|
| `modelo` | `qwen2.5:7b`, etc. Sin esto el resultado no es citable. **`NULL` = lo decidió la cascada determinista, no el LLM** |
| `prompt_version` | hash o etiqueta del prompt. Cambiar el prompt cambia los resultados |
| `confianza` | lo que el modelo reporta; permite curvas precisión/cobertura |
| `candidatos_evaluados` | cuántos vecinos vio. Un 1 no es lo mismo que un 8 |
| `latencia_ms` | el costo real, para reportarlo |

El `origen` **no necesita un valor nuevo**: sigue siendo `PROPAGADO` (heredó
una identidad) o `NUEVO` (sintética). Quién decidió se lee en `modelo IS NOT
NULL`, que es más preciso que un enum y no toca `OrigenClee`.

Registro en `CLEEResolver.METHODS`:

```python
"llm": {"table": "clee_llm", "propagate": propagate_clees_llm, "prefix_suffix": "LLM"}
```

Con eso `transform --only clee-llm` y `clee_propagation_stats --method llm`
funcionan sin tocar el orquestador ni el script de métricas — los dos se
derivan del registro.

---

## 5. Ollama en indominus

Ya hay un Ollama corriendo ahí (`wearchai-ollama-1`), de otro proyecto. **No
reusarlo:** acopla dos proyectos y su ciclo de vida no lo controlamos.

Servicio propio en `docker-compose.yml`, con la URL por `.env` para poder
apuntar a otro si hace falta:

```yaml
  ollama:
    image: ollama/ollama:latest
    profiles: ["llm"]            # no entra con un `up -d` normal
    volumes:
      - ollama_models:/root/.ollama
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

```bash
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_MODEL=qwen2.5:7b
OLLAMA_TIMEOUT_SECONDS=30
OLLAMA_MAX_BATCH=16
```

### Esto cambia una decisión de arquitectura

`arquitectura.md` §2.3.3 dice **"la GPU no participa en el ETL"**, con
razones válidas: Postgres no la usa, Polars/rapidfuzz/H3 son CPU puro, y el
cuello es IO y RAM. Eso sigue siendo cierto **para el ETL como está**. Un
adjudicador LLM es la primera etapa que sí la usa, así que hay que
actualizar esa sección en vez de dejarla contradiciendo al código.

**Nota de hardware que ya está documentada y aplica aquí:** la RTX 5090 es
Blackwell `sm_120` y requiere CUDA 12.8+. Ollama trae sus propios binarios,
así que debería estar bien, pero es lo primero a verificar — un contenedor
viejo de Ollama no reconoce la arquitectura y cae a CPU **en silencio**,
que es el peor modo de fallo: funciona, pero 50× más lento.

Verificación obligatoria antes de medir nada:

```bash
docker compose --profile llm run --rm ollama ollama list
docker compose exec ollama nvidia-smi        # ¿ve la GPU?
# y durante una llamada: nvidia-smi debe mostrar el proceso
```

---

## 6. Pasos

Cada uno es un commit verificable, y los tres primeros **no escriben nada en
la base**.

- [ ] **1. Medir la banda ambigua.** Antes de cualquier código de LLM:
      hacer que `_best_candidate` devuelva `best_score` y volcar la
      distribución de scores de los casos que hoy acaban en `NUEVO` o en
      desacuerdo radio/h3.

      **Esto decide si el proyecto vale la pena.** Entregable: cuántas
      decisiones caen en la banda para cada par de umbrales. Si son 300k,
      son horas; si son 6M, el diseño no sirve y hay que targetear más fino.

- [ ] **2. Cliente Ollama** (`src/shared/llm/`). Con las lecciones que ya
      costaron caro en este repo:
      - **Deadline total por llamada**, no solo timeout entre tokens — es
        el mismo bug que colgó las descargas 16 horas.
      - `temperature=0` y `seed` fijo, ambos registrados.
      - Salida estructurada validada; una respuesta que no parsea es un
        fallo, no un `None` silencioso.
      - **Cache por hash del prompt.** Los nombres de negocio se repiten
        muchísimo entre cortes: la misma pareja no se pregunta dos veces.
        Es lo que puede bajar el costo un orden de magnitud.

- [ ] **3. Adjudicador** (`src/etl/agents/clee_adjudicador.py`). Función
      pura sobre `(registro, candidatos) -> decisión`, probable sin servidor
      con un cliente de mentiras. Batch de hasta `OLLAMA_MAX_BATCH`.

- [ ] **4. Evaluar contra el ground truth que YA tienes.**
      `scripts/evaluate_masked_clee.py` enmascara CLEEs reales y mide
      aciertos. Correrlo con el adjudicador activo y comparar contra el
      97.72% actual, **sobre la misma muestra**.

      Criterio de aceptación: si no mejora el 97.72% de forma medible, el
      método no entra a Silver. Mejor saberlo con 920k filas de muestra que
      con 82.9M.

- [ ] **5. Tabla y método** (`silver.clee_llm` + migración + entrada en
      `METHODS`). Solo después del paso 4.

- [ ] **6. Corrida completa** y comparación de los tres métodos con
      `clee_propagation_stats --method radio --method h3 --method llm`.

- [ ] **7. Actualizar `arquitectura.md`** §2.3.3 (la GPU ya participa) y
      `docs/separacion_ingest_clee.md` (tercer método, como se anticipó).

---

## 7. Riesgos

| Riesgo | Mitigación |
|---|---|
| **Cae a CPU sin avisar** y la corrida tarda 50× | Verificar `nvidia-smi` durante una llamada real, no solo que el contenedor arranque. Registrar `latencia_ms` por decisión: un salto delata el fallback |
| **No reproducible**: re-correr da otros números | `temperature=0`, `seed` fijo, `modelo` y `prompt_version` en cada fila. Cambiar el prompt obliga a re-correr, y la tabla lo dice |
| **El volumen no cierra**: la banda es de millones | El paso 1 lo mide antes de escribir el adjudicador. Palancas: estrechar la banda, o limitar el LLM a los desacuerdos radio/h3 |
| **El modelo alucina un CLEE** | La salida es un **índice del candidato** o "ninguno", nunca un CLEE libre. El CLEE lo pone el código desde la KB |
| **No mejora nada** | Es un resultado válido y publicable: acota el techo del 43.7% y refuerza que lo que falta es estructura (asignación 1:1), no señal |
| Modelo de 7B demasiado débil | La comparación del paso 4 es barata; probar 2–3 tamaños antes de comprometerse |

---

## 8. Lo que este plan NO hace

- **No toca `clee_radio` ni `clee_h3`.** Es una tabla nueva: ese es
  justamente el punto del diseño de `docs/separacion_ingest_clee.md`.
- **No persigue el 56.3% de gemelos reales.** Eso es asignación 1:1
  (`plan.md` §4), y ningún scorer lo resuelve.
- **No reemplaza el método de referencia.** `entidades_economicas` sigue
  derivándose de `clee_radio` (`IDENTITY_TABLE` en
  `src/etl/loaders/entidades.py`). Cambiar la unidad de análisis es una
  decisión aparte, y se toma **con** los resultados de la comparación.
