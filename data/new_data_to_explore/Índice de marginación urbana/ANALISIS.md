# Índice de marginación (CONAPO) — análisis de fuente

**Veredicto:** ✅ útil como **covariable estructural** de contexto socioeconómico.
Encaja al grano municipal (2010/2015) y localidad (2020) del proyecto.

## Archivos y grano

| Archivo | Hoja de datos | Grano | Clave |
|---|---|---|---|
| `IMM_DP2_2010.xlsx` | `IMM_2010` | Municipal | `CVE_MUN` 5 díg. (`01001`) |
| `IMM_DP2_2015.xlsx` | `IMM_2015` | Municipal | `CVE_MUN` 5 díg. (`01001`) |
| `IML_2020.xls` | `IML_2020_AGS-MEX`, `IML_2020_MICH-ZAC` | Localidad | `CVE_LOC` 9 díg. |

- Cada `.xls*` incluye además una hoja `Diccionario` con la definición de los indicadores.
- `IMM_2010` / `IMM_2015`: encabezado en fila 0, datos desde fila 1 (xlsx plano, fácil).
- `IML_2020`: partido en dos hojas por rango de estados (AGS-MEX, MICH-ZAC),
  ~52k localidades por hoja; hay también `IML_2020_LOC2viv`.

## Cobertura de join

- `IMM_2015`: **2 457 / 2 457** match perfecto con `seed_municipios` (`CVE_MUN` ya
  viene con cero a la izquierda, join directo). `IMM_2010` misma estructura.
- `IML_2020`: `CVE_LOC` cruza con `data/seeds/seed_localidades.parquet`
  (`cve_ent`+`cve_mun`+`cve_loc`).

## Contenido

- 9 indicadores parciales: analfabetismo (`ANALF`), sin educación básica (`SBASC`),
  ocupantes en viviendas sin drenaje (`OVSDE`), sin energía (`OVSEE`), sin agua
  entubada (`OVSAE`), con piso de tierra (`OVPT`), hacinamiento (`VHAC`/`OVHAC`),
  población en localidades <5 000 hab (`PL.5000`), ocupada con ≤2 SM (`PO2SM`), etc.
- Índice continuo `IM_YYYY` y grado `GM_YYYY` (categórico: Muy bajo … Muy alto).

## Limitación

- Son **cortes censales/intercensales estáticos** (2010, 2015, 2020) — no hay serie
  anual. Sirven para empatar por año más cercano como contexto, no como variable
  temporal fina.
- En esta carpeta solo hay **municipal 2010/2015** y **localidad 2020**. Falta
  municipal 2020 y localidad 2010/2015 si se necesitan otras vintages
  (descargables de CONAPO).

> Nota: la carpeta se llama "urbana", pero el contenido es marginación municipal
> (IMM) y por localidad (IML), no el índice a nivel AGEB urbana.
