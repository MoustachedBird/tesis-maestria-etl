"""Modelos ORM de la capa `gold` — pendiente de implementar (Fase 5).

Gold no existe todavía: hoy el `load` copia el archivo de Silver porque la
capa de consumo nunca se diseñó (ver `arquitectura.md` §1.1). Este módulo
está vacío a propósito, para que el schema exista en las migraciones y la
estructura de `models/` no cambie cuando se llene.

Lo que vendrá aquí, con el contrato ya escrito en
`docs/cruce_denue_sesnsp.md`:

- `Establecimiento` — una fila por negocio resuelto, con la **regla de
  cierre aplicada** sobre `silver.permanencia_<metodo>` del método elegido.
  Esa tabla ya trae la ventana observada y los ingredientes de censura
  (`huecos`, `cortes_tras_ultimo`); lo que Gold agrega es el veredicto, y
  por eso espera a que la regla esté validada. **Mismo nombre que
  `silver.Establecimiento` y grano distinto**: ~10M negocios frente a 82.9M
  observaciones.
- Los agregados del análisis: panel negocio×periodo e incidencia municipal
  alineada temporalmente.
"""
