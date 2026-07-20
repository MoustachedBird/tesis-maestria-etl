# Cruce DENUE ↔ SESNSP: diseño metodológico (borrador)

> Borrador de trabajo para el capítulo metodológico (2026-07-20).
> Insumo: Silver DuckDB con `establecimientos` (DENUE, 76.8M observaciones,
> 19 periodos 2015–2026), `entidades_economicas` (dimensión N3, una fila
> por negocio resuelto — pendiente de construir tras la re-resolución) y
> `sesnsp_incidencia_municipal` (30.8M filas mensuales 2015–2025).

## 1. Pregunta y unidad de análisis

**Pregunta**: ¿cómo se relaciona la evolución de la incidencia delictiva
municipal con la supervivencia y mortalidad de las MiPyMEs en México?

La unidad de análisis es el **negocio resuelto** (fila de
`entidades_economicas`, identidad `clee_propagado_radio` construida por la
propagación CLEE) y su **ventana de vida observada**
(`primer_periodo`..`ultimo_periodo`). El contexto delictivo se mide en el
**municipio** del negocio, el nivel más fino que publica el SESNSP.

## 2. Las dos fuentes y sus granularidades

| | DENUE (INEGI) | SESNSP |
|---|---|---|
| Naturaleza | *Snapshot*: foto del universo de negocios | Flujo: conteo mensual de carpetas de investigación |
| Frecuencia | 1–2 cortes/año (2015–2026) | Mensual (2015–2025) |
| Unidad | Establecimiento (micro-dato georreferenciado) | Municipio × delito (98 categorías) × mes |
| Identidad temporal | CLEE oficial desde 2021; propagado hacia atrás por este pipeline | No aplica (agregado) |
| Clave de cruce | `cve_ent`, `cve_mun` (+ coordenadas/H3) | `cve_ent`, `cve_mun` |

La asimetría snapshot-vs-flujo es la decisión metodológica central: los
negocios NO se observan continuamente, así que la exposición al crimen
debe agregarse a la ventana entre cortes DENUE.

## 3. Definiciones operativas

- **Supervivencia entre cortes**: un negocio presente en el corte `t`
  sobrevive al corte `t+1` si su identidad aparece en `t+1`
  (`ultimo_periodo >= t+1`).
- **Muerte observada**: `ultimo_periodo = t` con cortes posteriores
  disponibles. *Censura por la derecha*: los presentes en el último corte
  no se clasifican (aún vivos hasta donde se observa).
- **Nacimiento observado**: `primer_periodo = t` con cortes anteriores
  disponibles (con la salvedad del recall del matching: un falso NEW_* se
  ve como nacimiento — el protocolo de Fase 7 cuantifica esa tasa).
- **Exposición al crimen del intervalo (t, t+1]**: suma (o promedio
  mensual, para intervalos de longitud distinta) de carpetas del
  municipio del negocio entre ambos cortes, total y por familias de
  delito relevantes para negocios:
  - *Depredatorios directos*: robo a negocio (subtipo propio en el
    catálogo), extorsión.
  - *Violencia general*: homicidio doloso, lesiones dolosas.
  - *Contexto amplio*: total de carpetas del municipio.

## 4. Query patrón (exposición por intervalo)

```sql
-- Exposición delictiva del municipio de cada entidad en el intervalo
-- entre su corte t y el corte siguiente t1 (fechas parametrizadas).
WITH exposicion AS (
    SELECT i.cve_ent, i.cve_mun,
           SUM(i.carpetas) AS carpetas_total,
           SUM(CASE WHEN d.subtipo = 'Robo a negocio' THEN i.carpetas END) AS robo_negocio,
           SUM(CASE WHEN d.subtipo = 'Extorsión' THEN i.carpetas END) AS extorsion,
           SUM(CASE WHEN d.subtipo = 'Homicidio doloso' THEN i.carpetas END) AS homicidio_doloso
    FROM sesnsp_incidencia_municipal i
    JOIN sesnsp_delitos d ON d.id = i.delito_id
    WHERE i.fecha > DATE '2021-05-01' AND i.fecha <= DATE '2022-11-01'
    GROUP BY 1, 2
)
SELECT e.clee_propagado_radio,
       (e.ultimo_periodo >= DATE '2022-11-01') AS sobrevive,
       e.codigo_act, x.carpetas_total, x.robo_negocio, x.extorsion, x.homicidio_doloso
FROM entidades_economicas e
JOIN exposicion x USING (cve_ent, cve_mun)
WHERE e.primer_periodo <= DATE '2021-05-01'
  AND e.ultimo_periodo >= DATE '2021-05-01';   -- vivos en t
```

Sobre esta tabla negocio-intervalo se estiman los modelos (p. ej. logit
de supervivencia o riesgos proporcionales discretos, con efectos fijos de
municipio y sector SCIAN).

## 5. Limitaciones (reportar todas)

1. **Cifra negra**: las carpetas de investigación miden delitos
   *denunciados e investigados*, no incidencia real. La ENVIPE estima que
   >90% de delitos no llega a carpeta; el sesgo varía por municipio y
   tipo de delito (extorsión, el más relevante para negocios, es de los
   menos denunciados). Las carpetas son un *proxy* con error de medición
   correlacionado con capacidad institucional local.
2. **Sin denominador poblacional**: con carpetas absolutas, municipios
   grandes parecen más violentos. Para tasas por 100k hab se requiere
   población municipal (CONAPO, proyecciones anuales) — pendiente como
   tercera fuente. Alternativa parcial: normalizar por número de
   establecimientos del propio DENUE (tasa por negocio).
3. **Municipios sintéticos SESNSP** (`998` No Especificado, `999` Otros):
   ~8 claves en 6 estados sin contraparte real en DENUE — quedan fuera
   del cruce (documentado; su volumen de carpetas debe reportarse para
   dimensionar la pérdida).
4. **Revisiones del SESNSP**: cada corte mensual revisa cifras
   históricas. El pipeline reconstruye la tabla completa por corte; los
   resultados deben citar el corte usado (hoy: jun2026).
5. **Falacia ecológica**: la exposición es municipal, no del negocio
   (un municipio grande promedia zonas muy distintas). Mitigación
   posible: exposición a nivel de celda H3 res-5 con datos de crimen
   georreferenciados de fuentes alternas — fuera del alcance actual.
6. **Error de la resolución de identidad**: muertes/nacimientos
   observados dependen del recall del matching CLEE. El protocolo de
   CLEE enmascarado (Fase 7 del todo.md) produce las tasas de error que
   deben acompañar cualquier estimación de mortalidad.
7. **Cobertura temporal**: DENUE llega a 2026 pero SESNSP (corte actual)
   a dic-2025; el último intervalo DENUE queda sin exposición completa.

## 6. Pendientes para cerrar el capítulo

- [ ] Construir `entidades_economicas` tras la re-resolución radio.
- [ ] Decidir familias de delito definitivas (validar subtipos exactos
      contra `sesnsp_delitos`; "Robo a negocio" y "Extorsión" ya
      verificados en el catálogo).
- [ ] Incorporar población CONAPO para tasas (tercera fuente).
- [ ] Números de Fase 7 (precision/recall) como anexo de calidad del
      linkage.
