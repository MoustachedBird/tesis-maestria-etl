"""Nombres de partición: funciones puras sobre la fecha y la tabla.

Viven aparte de `test_partitions.py` porque ese archivo entero está marcado
`postgres` —sus pruebas enganchan y desenganchan particiones de verdad— y
estas no necesitan servidor. Separarlas es lo que las hace correr siempre,
que es justo lo que se quiere de una función pura.
"""

from datetime import date

from src.shared.db.partitions import partition_name, qualified_partition

PERIODO = date(2024, 11, 1)


class TestNombreDeParticion:
    """Se derivan de la fecha, sin tocar la base."""

    def test_el_nombre_lleva_ano_y_mes(self):
        assert partition_name(PERIODO) == "establecimientos_2024_11"

    def test_el_mes_va_con_cero_a_la_izquierda(self):
        """Sin esto el orden alfabetico dejaria de ser cronologico."""
        assert partition_name(date(2025, 3, 1)) == "establecimientos_2025_03"

    def test_el_orden_alfabetico_es_cronologico(self):
        cortes = [date(2025, 3, 1), date(2024, 11, 1), date(2025, 1, 1)]
        nombres = sorted(partition_name(c) for c in cortes)
        assert nombres == [partition_name(c) for c in sorted(cortes)]

    def test_el_calificado_va_entrecomillado(self):
        assert qualified_partition(PERIODO) == '"silver"."establecimientos_2024_11"'


class TestVariasTablasParticionadas:
    """La tabla de hechos ya no es la única particionada por corte.

    `silver.clee_radio` y `silver.clee_h3` también lo están, así que el
    nombre de la partición tiene que derivarse del padre: si no, el mismo
    corte en dos métodos apuntaría al mismo nombre.
    """

    def test_el_nombre_lleva_la_tabla_padre(self):
        assert partition_name(PERIODO, "clee_radio") == "clee_radio_2024_11"
        assert partition_name(PERIODO, "clee_h3") == "clee_h3_2024_11"

    def test_el_mismo_corte_en_dos_metodos_no_colisiona(self):
        """El bug que la parametrización evita."""
        assert partition_name(PERIODO, "clee_radio") != partition_name(PERIODO, "clee_h3")

    def test_la_tabla_de_hechos_sigue_siendo_el_default(self):
        """Los call sites del ingest no nombran la tabla: es el caso dominante."""
        assert partition_name(PERIODO) == partition_name(PERIODO, "establecimientos")

    def test_el_calificado_tambien_acepta_la_tabla(self):
        assert qualified_partition(PERIODO, "silver", "clee_h3") == '"silver"."clee_h3_2024_11"'
