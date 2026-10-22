"""Helpers de arranque del arnés de pruebas.

Deciden si los tests marcados `postgres` corren o se saltan, así que un bug
aquí no rompe nada: hace que 157 tests desaparezcan en silencio y que una
suite verde no signifique nada. Por eso se prueban.
"""

from tests.conftest import _maintenance_url, _primera_linea


class TestMaintenanceUrl:
    """La conexión para crear la base de pruebas."""

    def test_apunta_a_la_base_postgres(self):
        """`CREATE DATABASE` no corre desde la base que va a crear."""
        assert (
            _maintenance_url("postgresql+psycopg://etl_rw:pw@host:5432/mipymes_test")
            == "postgresql+psycopg://etl_rw:pw@host:5432/postgres"
        )

    def test_conserva_credenciales_host_y_puerto(self):
        """Solo cambia la base: el resto es la misma conexión."""
        url = _maintenance_url("postgresql+psycopg://u:p@172.20.0.2:6543/otra_test")
        assert url.startswith("postgresql+psycopg://u:p@172.20.0.2:6543/")

    def test_solo_reemplaza_el_ultimo_segmento(self):
        """Un password con '/' no debe confundir el corte."""
        url = _maintenance_url("postgresql+psycopg://u:a%2Fb@host:5432/mipymes_test")
        assert url.endswith("/postgres")
        assert "a%2Fb" in url


class TestPrimeraLinea:
    """Resumen de errores para el motivo del skip."""

    def test_se_queda_con_la_primera_linea(self):
        """El detalle multilínea de psycopg no aporta al motivo del skip."""
        exc = RuntimeError("connection failed\nDETAIL: host=x password=secreto")
        assert _primera_linea(exc) == "connection failed"

    def test_acota_el_largo(self):
        """Acotado para que el DSN completo no acabe en el log de pytest."""
        assert len(_primera_linea(RuntimeError("x" * 500))) == 120

    def test_tolera_un_error_sin_mensaje(self):
        assert _primera_linea(RuntimeError("")) == ""
