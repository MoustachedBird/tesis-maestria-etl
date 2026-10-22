"""Fronteras de import entre los paquetes de `src/`.

El API y el ETL son procesos distintos con privilegios distintos: el API
entra a la base con `api_ro`, que solo tiene SELECT en gold y meta
(arquitectura.md §2.3.1). Un endpoint que importara un transformer podria
arrancar un ingest dentro del proceso del API y morir con un error de
permisos que no dice nada del verdadero problema.

Los imports se leen del codigo fuente con `ast`, sin importar los modulos:
asi el test no necesita las dependencias del ETL instaladas y falla por la
razon correcta --un import prohibido-- en vez de por un ImportError.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"

PROHIBIDOS = {
    "src.api": ("src.etl", "src.scheduler"),
    "src.etl": ("src.api", "src.scheduler"),
    "src.shared": ("src.api", "src.etl", "src.meta", "src.scheduler"),
    "src.meta": ("src.api", "src.etl", "src.scheduler"),
}
"""Que paquete no puede importar de que.

- `src.api` no toca el ETL: es otro proceso, con otro rol de base.
- `src.etl` no toca el API ni el scheduler: lo orquestan a el, no al reves.
- `src.shared` es la base de todos, asi que no puede depender de nadie --si
  lo hiciera, importar la config arrastraria medio proyecto.
- `src.meta` describe corridas; no las ejecuta.
"""


def _imports(modulo: Path) -> set[str]:
    """Modulos `src.*` que importa el archivo, sin ejecutarlo."""
    arbol = ast.parse(modulo.read_text())
    nombres: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            nombres.update(a.name for a in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.module and nodo.level == 0:
            nombres.add(nodo.module)
    return {n for n in nombres if n.startswith("src.")}


@pytest.mark.unit
@pytest.mark.parametrize("paquete,vetados", PROHIBIDOS.items(), ids=list(PROHIBIDOS))
def test_paquete_no_importa_capas_vetadas(paquete: str, vetados: tuple[str, ...]):
    """Ningun modulo del paquete importa de las capas que le estan vetadas."""
    raiz = SRC / paquete.split(".")[1]
    if not raiz.exists():
        pytest.skip(f"{paquete} todavia no existe")

    infracciones = [
        f"{modulo.relative_to(SRC.parent)} -> {importado}"
        for modulo in raiz.rglob("*.py")
        for importado in _imports(modulo)
        if importado.startswith(vetados)
    ]

    assert not infracciones, f"{paquete} no puede importar {vetados}:\n" + "\n".join(infracciones)


@pytest.mark.unit
def test_los_paquetes_declarados_cubren_src():
    """La tabla no se queda atras cuando aparece un paquete nuevo en `src/`.

    Si alguien agrega `src/scheduler/` (Fase 7) sin decidir sus fronteras,
    este test lo obliga a pasar por aqui.
    """
    en_disco = {f"src.{d.name}" for d in SRC.iterdir() if d.is_dir() and not d.name.startswith("_")}
    sin_declarar = en_disco - set(PROHIBIDOS)
    assert not sin_declarar, f"paquetes sin fronteras declaradas: {sorted(sin_declarar)}"
