"""Modelos ORM de la capa `gold` — pendiente de implementar (Fase 5).

Gold no existe todavía: hoy el `load` copia el archivo de Silver porque la
capa de consumo nunca se diseñó (ver `arquitectura.md` §1.1). Este módulo
está vacío a propósito, para que el schema exista en las migraciones y la
estructura de `models/` no cambie cuando se llene.

Las reglas de normalización —qué valor canónico toma cada campo a partir de
las observaciones del negocio, y dónde interviene un LLM— están en
`normalizacion_gold.md`. Es ahí, y no en Silver, donde SÍ se materializa un
valor por campo: Gold es la capa citable y tiene que ser estable aunque la
identidad se re-resuelva después (ver `normalizacion_gold.md` §0.1).

El alcance lo fija `gold_cohorte_2021.md`: **solo los cortes en que INEGI
publica el CLEE**, donde la identidad es observada y no inferida.

Lo que vendrá aquí:

- `NegocioCleeOficial` → `gold.negocios_clee_oficial`. Una fila por negocio
  de la cohorte, con su ventana de vida calculada **solo** sobre los cortes
  oficiales. No se copia de `silver.permanencia_<metodo>`: esa tabla agrega
  sobre los 20 cortes y una fila suya mezcla identidad observada con
  identidad propagada (`gold_cohorte_2021.md` §4).
- `VarianteNegocio` → `gold.variantes_negocio`. Una fila por
  `(negocio, campo, valor)`: todos los valores distintos que tuvo en
  `nom_estab`, `raz_social`, `codigo_act`, `telefono`, `correoelec` y `www`,
  con `primer_periodo`/`ultimo_periodo`/`n_cortes` —la ventana en que usó
  cada uno, gratis del mismo `GROUP BY`— y `es_canonico`, que marca el valor
  que publicó la tabla madre. Índice trigram sobre `valor` para búsqueda
  difusa (`normalizacion_gold.md` §6).

  `es_canonico` es lo que la vuelve auditable: la tabla madre publica un
  valor por campo y aquí se ve de qué alternativas salió y cuál ganó, sin
  comparar cadenas entre tablas. Un índice único parcial garantiza a lo más
  un canónico por `(clee, campo)`.

  **Acotada a la misma cohorte**: sin identidades propagadas ni sintéticas
  `NEW_*`. La extensión de robustez tendría las suyas en
  `gold.variantes_negocio_propagado`, porque aquí se aplica la misma regla
  que a `clee_radio`/`clee_h3`: una tabla por régimen, nunca una columna
  discriminadora.
- Los agregados del análisis: panel negocio×periodo e incidencia municipal
  alineada temporalmente, con el contrato de `docs/cruce_denue_sesnsp.md`.

**No se llama `gold.establecimientos`.** `arquitectura.md` §2.3.1 eligió ese
nombre y dejó anotada la alternativa; se adopta la alternativa (decidido
2026-11-07) porque el grano es distinto —un negocio, no una observación— y
el nombre tiene que decirlo. El beneficio es estructural y no estético: con
nombres distintos, confundir la N de 82.9M observaciones con la de los
negocios deja de ser posible, y las tres salvaguardas de §2.3.1 dejan de
depender de la disciplina de quien escribe la consulta.
"""
