"""El schema `verificacion`: lo que la base garantiza del trabajo del agente.

Tres clases de aserción:

- **El vocabulario es cerrado.** `estado`, `confianza` y `senal` tienen un
  `CHECK` que sale de los enums: un agente que escriba "closed" o "CERRADO "
  falla el `INSERT` en vez de romper el conteo en silencio.
- **La muestra es la unidad de vida.** Borrar una muestra se lleva sus
  negocios, sus veredictos y sus evidencias; un veredicto no puede referirse
  a un negocio que no salió en la muestra.
- **No toca `gold`.** Ninguna FK apunta a `gold`: es lo que permite truncar
  la cohorte sin perder un solo veredicto, y es la razón de que esto sea un
  schema aparte.
"""

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.shared.db.base import SCHEMAS, tables_for
from src.shared.db.models.verificacion import (
    SCHEMA,
    Evidencia,
    Muestra,
    MuestraNegocio,
    Veredicto,
)
from src.shared.enums import Confianza, EstadoVerificado, Layer, SenalEvidencia

VENTANA = [date(2021, 5, 1), date(2021, 11, 1), date(2022, 5, 1)]


def _muestra(nombre: str = "piloto") -> Muestra:
    return Muestra(
        muestra_id=str(uuid4()),
        nombre=nombre,
        semilla=0.42,
        tamano_pedido=400,
        n_negocios=1,
        estratos={"cerrado": 200, "activo": 200},
        filtro="NOT cluster_sospechoso AND nombre IS NOT NULL",
        version_reglas="v1",
        ventana_cortes=VENTANA,
        cierre_meses_ausente=12,
    )


def _negocio(muestra: Muestra, clee: str = "14TAC0001") -> MuestraNegocio:
    return MuestraNegocio(
        muestra_id=muestra.muestra_id,
        clee=clee,
        orden=1,
        estrato="cerrado",
        cerrado_gold=True,
        meses_sin_aparecer_gold=12,
        huecos_gold=0,
    )


def _veredicto(negocio: MuestraNegocio, **extra) -> Veredicto:
    campos = {
        "veredicto_id": str(uuid4()),
        "muestra_id": negocio.muestra_id,
        "clee": negocio.clee,
        "estado": EstadoVerificado.CERRADO.value,
        "confianza": Confianza.ALTA.value,
        "razonamiento": "Google Maps lo marca cerrado permanentemente desde 2024.",
        "agente": "claude-opus-5-5/v1",
    }
    campos.update(extra)
    return Veredicto(**campos)


@pytest.mark.unit
class TestElEsquemaSinBase:
    """Lo que se puede afirmar del modelo sin un servidor."""

    def test_es_el_cuarto_schema(self):
        assert Layer.VERIFICACION.value == "verificacion"
        assert SCHEMA in SCHEMAS

    def test_las_cuatro_tablas(self):
        assert [t.name for t in tables_for(SCHEMA)] == [
            "muestras",
            "muestra_negocios",
            "veredictos",
            "evidencias",
        ]

    def test_ninguna_fk_apunta_a_gold(self):
        """Es lo que permite `TRUNCATE gold.*` sin perder veredictos."""
        for tabla in tables_for(SCHEMA):
            for fk in tabla.foreign_keys:
                assert fk.column.table.schema == SCHEMA, f"{tabla.name} -> {fk.target_fullname}"

    def test_los_check_salen_de_los_enums(self):
        """Agregar un valor al enum lo admite; escribir otro falla."""
        # La convencion de nombres de `base.py` los deja como `ck_<tabla>_<nombre>`.
        checks = {
            c.name.rsplit("_", 1)[-1]: str(c.sqltext)
            for t in tables_for(SCHEMA)
            for c in t.constraints
            if hasattr(c, "sqltext")
        }
        for estado in EstadoVerificado:
            assert f"'{estado.value}'" in checks["estado"]
        for confianza in Confianza:
            assert f"'{confianza.value}'" in checks["confianza"]
        for senal in SenalEvidencia:
            assert f"'{senal.value}'" in checks["senal"]

    def test_indeterminado_es_un_estado_legitimo(self):
        """Forzarlo a activo o cerrado contaminaría la medida con adivinanzas."""
        assert EstadoVerificado.INDETERMINADO.value == "indeterminado"


@pytest.fixture
def verificacion_engine(pg_engine):
    """`verificacion` recién creado desde el modelo, como lo hace la migración."""
    from src.shared.db.base import Base

    with pg_engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))
    Base.metadata.create_all(pg_engine, tables=tables_for(SCHEMA))
    yield pg_engine


@pytest.mark.unit
@pytest.mark.postgres
class TestElVocabularioEsCerrado:
    def test_un_veredicto_completo_se_guarda(self, verificacion_engine):
        with Session(verificacion_engine) as s:
            m = _muestra()
            n = _negocio(m)
            v = _veredicto(n, fecha_cierre_estimada=date(2024, 3, 1))
            e = Evidencia(
                evidencia_id=str(uuid4()),
                veredicto_id=v.veredicto_id,
                fuente="google_maps",
                url="https://maps.google.com/?cid=1",
                fecha_publicacion=date(2024, 3, 1),
                extracto="Cerrado permanentemente",
                senal=SenalEvidencia.CERRADO.value,
            )
            # Sin `relationship()` el ORM no sabe que la evidencia depende del
            # veredicto, asi que se persisten en el orden de la FK.
            s.add_all([m, n, v])
            s.flush()
            s.add(e)
            s.commit()
            guardado = s.get(Veredicto, v.veredicto_id)
            assert guardado.estado == "cerrado"
            assert guardado.verificado_en is not None
            assert s.get(Muestra, m.muestra_id).estratos == {"cerrado": 200, "activo": 200}

    @pytest.mark.parametrize("estado", ["closed", "CERRADO", "cerrado ", ""])
    def test_un_estado_fuera_del_vocabulario_falla(self, verificacion_engine, estado):
        with Session(verificacion_engine) as s:
            m = _muestra()
            n = _negocio(m)
            s.add_all([m, n, _veredicto(n, estado=estado)])
            with pytest.raises(IntegrityError):
                s.commit()

    def test_una_confianza_numerica_falla(self, verificacion_engine):
        """Ordinal a propósito: un 0.83 de un LLM no mide nada."""
        with Session(verificacion_engine) as s:
            m = _muestra()
            n = _negocio(m)
            s.add_all([m, n, _veredicto(n, confianza="0.83")])
            with pytest.raises(IntegrityError):
                s.commit()

    def test_el_razonamiento_es_obligatorio(self, verificacion_engine):
        """Un estado sin el porqué no se puede auditar."""
        with Session(verificacion_engine) as s:
            m = _muestra()
            n = _negocio(m)
            s.add_all([m, n, _veredicto(n, razonamiento=None)])
            with pytest.raises(IntegrityError):
                s.commit()

    def test_una_senal_fuera_del_vocabulario_falla(self, verificacion_engine):
        with Session(verificacion_engine) as s:
            m = _muestra()
            n = _negocio(m)
            v = _veredicto(n)
            s.add_all([m, n, v])
            s.commit()
            s.add(
                Evidencia(
                    evidencia_id=str(uuid4()),
                    veredicto_id=v.veredicto_id,
                    fuente="facebook",
                    extracto="...",
                    senal="closed",
                )
            )
            with pytest.raises(IntegrityError):
                s.commit()


@pytest.mark.unit
@pytest.mark.postgres
class TestLaMuestraEsLaUnidadDeVida:
    def test_un_veredicto_sobre_un_negocio_que_no_salio_falla(self, verificacion_engine):
        """La FK compuesta: el agente solo opina de lo que se le dio."""
        with Session(verificacion_engine) as s:
            m = _muestra()
            s.add(m)
            s.commit()
            s.add(_veredicto(_negocio(m, clee="NO_MUESTREADO")))
            with pytest.raises(IntegrityError):
                s.commit()

    def test_borrar_la_muestra_arrastra_todo(self, verificacion_engine):
        with Session(verificacion_engine) as s:
            m = _muestra()
            n = _negocio(m)
            v = _veredicto(n)
            e = Evidencia(
                evidencia_id=str(uuid4()),
                veredicto_id=v.veredicto_id,
                fuente="sitio_web",
                extracto="...",
                senal=SenalEvidencia.NEUTRAL.value,
            )
            s.add_all([m, n, v])
            s.flush()
            s.add(e)
            s.commit()
            s.delete(m)
            s.commit()
        with verificacion_engine.connect() as conn:
            for tabla in ("muestra_negocios", "veredictos", "evidencias"):
                assert conn.execute(text(f"SELECT count(*) FROM {SCHEMA}.{tabla}")).scalar() == 0

    def test_dos_veredictos_del_mismo_negocio_conviven(self, verificacion_engine):
        """Otro agente u otra fecha: cuál se usa es decisión de consulta."""
        with Session(verificacion_engine) as s:
            m = _muestra()
            n = _negocio(m)
            s.add_all(
                [
                    m,
                    n,
                    _veredicto(n, agente="a/v1"),
                    _veredicto(n, agente="b/v1", estado=EstadoVerificado.INDETERMINADO.value),
                ]
            )
            s.commit()
            assert s.query(Veredicto).filter_by(clee=n.clee).count() == 2

    def test_el_nombre_de_la_muestra_es_unico(self, verificacion_engine):
        """Dos tiradas con reglas distintas son dos filas con dos nombres."""
        with Session(verificacion_engine) as s:
            s.add_all([_muestra("piloto"), _muestra("piloto")])
            with pytest.raises(IntegrityError):
                s.commit()


@pytest.mark.unit
@pytest.mark.postgres
def test_truncar_gold_no_toca_la_verificacion(verificacion_engine):
    """La razón de que sea un schema aparte, comprobada contra el motor."""
    from src.shared.db.base import Base

    Base.metadata.create_all(verificacion_engine, tables=tables_for("gold"))
    with Session(verificacion_engine) as s:
        m = _muestra()
        n = _negocio(m)
        s.add_all([m, n, _veredicto(n)])
        s.commit()
    with verificacion_engine.begin() as conn:
        conn.execute(text("TRUNCATE gold.negocios_clee_oficial"))
    with verificacion_engine.connect() as conn:
        assert conn.execute(text(f"SELECT count(*) FROM {SCHEMA}.veredictos")).scalar() == 1
