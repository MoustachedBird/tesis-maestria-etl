"""CLI de los agentes: `python -m src.agents.main <comando>`.

Tres subcomandos y no un comando global con `--no-*` como el ETL, porque no
son un pipeline que se rehaga completo: `muestrear` se corre una vez por
muestra, `verificar` se reanuda las veces que haga falta y `metricas` solo
lee. Cada uno dice en su `--help` qué toca.

    muestrear   gold -> verificacion.muestras + muestra_negocios
    verificar   muestra -> verificacion.veredictos + evidencias (Ollama + web)
    metricas    el cruce veredicto vs cerrado_gold, por estrato
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from src.shared.config import get_settings

app = typer.Typer(help="Agentes LLM sobre el warehouse: verificación de supervivencia")
console = Console()


@app.command()
def muestrear(
    nombre: str = typer.Option(..., "--nombre", help="Cómo se citará la muestra (único)."),
    semilla: float | None = typer.Option(
        None, "--semilla", help="setseed() en [-1, 1]; default MUESTRA_SEMILLA."
    ),
    cuota: int | None = typer.Option(
        None, "--cuota", help="Negocios por estrato; default MUESTRA_CUOTA_POR_ESTRATO."
    ),
) -> None:
    """Saca una muestra estratificada y reproducible de gold.negocios_clee_oficial."""
    from src.agents.survival.muestreo import crear_muestra
    from src.meta.tracker import MetadataTracker
    from src.shared.db.session import get_engine

    ajustes = get_settings()
    semilla = ajustes.MUESTRA_SEMILLA if semilla is None else semilla
    cuota = ajustes.MUESTRA_CUOTA_POR_ESTRATO if cuota is None else cuota
    console.print(
        f"[bold magenta]   Muestreando {nombre!r}: {cuota} por estrato, semilla {semilla}[/bold magenta]"
    )
    try:
        conteos = crear_muestra(get_engine(), MetadataTracker(), nombre, semilla, cuota)
    except ValueError as e:
        console.print(f"[red]   ✖ {e}[/red]")
        raise typer.Exit(code=1) from e
    for estrato, n in conteos.items():
        console.print(f"   {estrato:<22} {n:>5}")
    console.print(f"   {sum(conteos.values())} negocios en verificacion.muestra_negocios")


@app.command()
def verificar(
    muestra: str = typer.Option(..., "--muestra", help="Nombre de la muestra a verificar."),
    limite: int | None = typer.Option(
        None, "--limite", help="Verificar a lo más N pendientes (1, 10...)."
    ),
    clee: str | None = typer.Option(None, "--clee", help="Verificar solo este negocio."),
    modelo: str | None = typer.Option(
        None, "--modelo", help="Tag de Ollama; default OLLAMA_MODEL."
    ),
) -> None:
    """Busca en línea cada negocio pendiente de la muestra y escribe su veredicto."""
    from src.agents.survival.sources.busqueda import BuscadorBrave, BuscadorSearxng
    from src.agents.survival.sources.paginas import leer_pagina
    from src.agents.survival.verificador import Verificador, resumen_muestra, verificar_muestra
    from src.meta.tracker import MetadataTracker
    from src.shared.db.session import get_engine
    from src.shared.llm import OllamaCliente, OllamaNoDisponible

    ajustes = get_settings()
    cliente = OllamaCliente(
        ajustes.OLLAMA_URL,
        modelo or ajustes.OLLAMA_MODEL,
        timeout=ajustes.OLLAMA_TIMEOUT_SEGUNDOS,
        num_ctx=ajustes.OLLAMA_CONTEXTO_TOKENS,
        pensar=ajustes.OLLAMA_PENSAR,
    )
    try:
        cliente.comprobar()
    except OllamaNoDisponible as e:
        console.print(f"[red]   ✖ {e}[/red]")
        raise typer.Exit(code=1) from e

    if ajustes.BRAVE_API_KEY:
        buscador = BuscadorBrave(
            ajustes.BRAVE_API_KEY, timeout=ajustes.AGENTE_TIMEOUT_HTTP_SEGUNDOS
        )
    else:
        buscador = BuscadorSearxng(
            ajustes.SEARXNG_URL, timeout=ajustes.AGENTE_TIMEOUT_HTTP_SEGUNDOS
        )
    try:
        buscador.buscar("INEGI DENUE", n=1)
    except Exception as e:  # noqa: BLE001  (se reporta y se sale: sin buscador no hay agente)
        console.print(f"[red]   ✖ el buscador {buscador.nombre} no responde: {e}[/red]")
        raise typer.Exit(code=1) from e

    verificador = Verificador(
        cliente,
        buscador,
        lambda url: leer_pagina(
            url, ajustes.AGENTE_MAX_CARACTERES_PAGINA, ajustes.AGENTE_TIMEOUT_HTTP_SEGUNDOS
        ),
        version=ajustes.AGENTE_VERSION,
        max_busquedas=ajustes.AGENTE_MAX_BUSQUEDAS,
        max_paginas=ajustes.AGENTE_MAX_PAGINAS,
        max_vueltas=ajustes.AGENTE_MAX_VUELTAS,
    )
    engine = get_engine()
    with engine.connect() as conn:
        try:
            resumen = resumen_muestra(conn, muestra, verificador.agente)
        except ValueError as e:
            console.print(f"[red]   ✖ {e}[/red]")
            raise typer.Exit(code=1) from e
    console.print(
        f"[bold magenta]   {verificador.agente} · buscador {buscador.nombre} · muestra {muestra!r}: "
        f"{resumen['pendientes']}/{resumen['total']} pendientes[/bold magenta]"
    )
    conteos = verificar_muestra(
        engine, MetadataTracker(), verificador, muestra, limite=limite, solo_clee=clee
    )
    console.print(f"   {conteos}")
    if conteos["fallidos"]:
        raise typer.Exit(code=1)


@app.command()
def metricas(
    muestra: str = typer.Option(..., "--muestra", help="Nombre de la muestra."),
    agente: str | None = typer.Option(
        None, "--agente", help="modelo/version a contar; default el más reciente de cualquiera."
    ),
) -> None:
    """La tabla de confusión por estrato: lo que gold afirmó contra lo que el agente encontró."""
    from src.agents.survival.metricas import (
        calcular,
        precision_cerrado_reponderada,
        tasa_falsos_negativos_reponderada,
    )
    from src.shared.db.session import get_engine

    with get_engine().connect() as conn:
        try:
            filas = calcular(conn, muestra, agente)
        except ValueError as e:
            console.print(f"[red]   ✖ {e}[/red]")
            raise typer.Exit(code=1) from e

    def pct(v: float | None) -> str:
        return "—" if v is None else f"{100 * v:.1f}%"

    tabla = Table(title=f"muestra {muestra!r}" + (f" · {agente}" if agente else ""))
    for col in (
        "estrato",
        "n",
        "sin veredicto",
        "indet.",
        "VP",
        "FP",
        "FN",
        "VN",
        "precisión cerrado",
        "FN activos",
        "población",
    ):
        tabla.add_column(col, justify="right" if col != "estrato" else "left")
    for f in filas:
        tabla.add_row(
            f.estrato,
            str(f.n),
            str(f.sin_veredicto),
            str(f.indeterminados),
            str(f.vp),
            str(f.fp),
            str(f.fn),
            str(f.vn),
            pct(f.precision_cerrado),
            pct(f.tasa_falsos_negativos),
            f"{f.poblacion:,}",
        )
    console.print(tabla)
    console.print(
        f"   precisión de `cerrado` en la población (reponderada): {pct(precision_cerrado_reponderada(filas))}"
    )
    console.print(
        f"   `activos` de gold que ya cerraron (reponderada):      {pct(tasa_falsos_negativos_reponderada(filas))}"
    )


if __name__ == "__main__":
    app()
