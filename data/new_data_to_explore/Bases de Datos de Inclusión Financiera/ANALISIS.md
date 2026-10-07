# Inclusión Financiera (CNBV) — análisis de fuente

**Veredicto:** ✅ muy útil, encaja directo al grano municipal del proyecto.
Prioridad alta por relevancia directa a MIPYMES y por ser panel temporal.

## Grano y clave de join

- **Grano:** municipal.
- Columna `Clave Municipio` (`1001`, `1002`, …) = código INEGI de 5 dígitos
  **sin cero a la izquierda** → `zfill(5)` = `01001` = `cve_ent='01'` + `cve_mun='001'`.
- **Cobertura de join:** 2 475 / 2 476 municipios hacen match con
  `data/seeds/seed_municipios.parquet` (2 486). El único que no cruza es
  `99999` (fila "no identificado", se descarta). Cobertura efectivamente total.

## Contenido (hojas `*Mun`)

Por municipio, desglosado en 5 sectores (Todos los sectores, Banca múltiple,
Banca de desarrollo, SOCAPs, SOFIPOs):

- **Infraestructura:** corresponsales, sucursales, cajeros, TPV,
  **establecimientos con TPV**, transacciones.
- **Captación** (ahorro/depósitos) y **Crédito**.
- Contexto: población total/adulta (por sexo), tipo de población,
  índice de rezago social.

Para la tesis (MIPYMES), lo más relevante: `Establecimientos con TPV`, `TPV`,
`Corresponsales` y `Crédito` municipal — miden la infraestructura financiera
disponible al pequeño comercio. Covariable fuerte al mismo grano.

## Frecuencia y apilado de periodos

- Cada `Base_de_Datos_de_Inclusion_Financiera_YYYYMM.xlsx` es **un corte de un
  periodo** (el archivo actual = dic-2025, "cierre a diciembre 2025").
- Se pueden **apilar los meses**: etiquetar el periodo tomando `YYYYMM` del
  **nombre del archivo** (la hoja NO trae la fecha en las filas) → panel
  municipio × periodo.
- La hoja `BD Datos históricos` es serie **nacional trimestral**, no municipal
  → se ignora para el panel.

## Caveats de parseo (no es un xlsx plano)

- Encabezado real en la **fila 10** (0-indexed); datos desde la **fila 11**.
- Header **multinivel con celdas combinadas** (sector × infra/transacciones)
  → hay que aplanar 2-3 filas para construir los nombres de columna.
- `Clave Municipio` viene como número → `zfill(5)`; descartar `99999`.

## Hojas del archivo

`MENU`, `Definición IF`, `Indicadores IF`, `Ficha técnica`,
`BD Infraestructura Mun`, `BD Infraestructura Edo`, `BD Infraestructura Banca`,
`BD Captación Mun`, `BD Captación Edo`, `BD Captación Banca`,
`BD Crédito Mun`, `BD Crédito Edo`, `BD Crédito Banca`,
`BD por sexo Banca Mun`, `BD por sexo EACP Mun`, `BD por sexo Banca`,
`BD Datos históricos`, `BD Banca`, `BD EACP`.

Las hojas `*Mun` son las de interés (grano municipal).
