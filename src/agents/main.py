"""CLI de los agentes: `python -m src.agents.main <comando>`.

Cinco subcomandos y no un comando global con `--no-*` como el ETL, porque
no son un pipeline que se rehaga completo: `muestrear` se corre una vez por
muestra, `verificar` y `auditar` se reanudan las veces que haga falta y
`metricas` solo lee. Cada uno dice en su `--help` qué toca.

    muestrear   gold -> verificacion.muestras + muestra_negocios
    sondear     sin modelo ni escritura: qué trae cada fuente para un negocio
    verificar   muestra -> veredictos + consultas + informes + evidencias
                (el equipo) y, por default, auditorias (el juez)
    auditar     veredictos sin auditoría de este juez -> auditorias
    metricas    el cruce veredicto vs cerrado_gold, por estrato, y el juez
"""

from __future__ import annotations

from typing import Any

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


def _cliente(modelo: str) -> Any:
    """Un cliente de Ollama comprobado (servidor arriba y modelo descargado), o sale con el error."""
    from src.shared.llm import OllamaCliente, OllamaNoDisponible

    ajustes = get_settings()
    cliente = OllamaCliente(
        ajustes.OLLAMA_URL,
        modelo,
        timeout=ajustes.OLLAMA_TIMEOUT_SEGUNDOS,
        num_ctx=ajustes.OLLAMA_CONTEXTO_TOKENS,
        pensar=ajustes.OLLAMA_PENSAR,
    )
    try:
        cliente.comprobar()
    except OllamaNoDisponible as e:
        console.print(f"[red]   ✖ {e}[/red]")
        raise typer.Exit(code=1) from e
    return cliente


def _fuentes() -> tuple[Any, list[Any]]:
    """El navegador de la corrida y las fuentes de `AGENTE_FUENTES`, comprobados; o sale con el error.

    Comprueba lo que se puede comprobar antes de gastar una hora de modelo:
    que la lista sea válida, que Chrome arranque y que el SearXNG del compose
    conteste (si `web` está en la lista).
    """
    from src.agents.survival.armado import Armado, nombres_de_fuentes
    from src.agents.survival.sources.cortesia import Pausador
    from src.agents.survival.sources.navegador import Navegador

    ajustes = get_settings()
    try:
        nombres = nombres_de_fuentes(ajustes.AGENTE_FUENTES)
    except ValueError as e:
        console.print(f"[red]   ✖ {e}[/red]")
        raise typer.Exit(code=1) from e
    pausador = Pausador(ajustes.AGENTE_PAUSA_SEGUNDOS)
    navegador = Navegador(
        timeout=ajustes.AGENTE_TIMEOUT_HTTP_SEGUNDOS,
        espera=ajustes.AGENTE_NAVEGADOR_ESPERA_SEGUNDOS,
        perfil=ajustes.AGENTE_PERFIL_CHROME or None,
        pausador=pausador,
    )
    try:
        navegador.comprobar()
    except Exception as e:  # noqa: BLE001  (se reporta y se sale: sin Chrome no hay Maps ni redes)
        console.print(
            f"[red]   ✖ Chrome no arranca ({e.__class__.__name__}: {e}). La imagen `agente` "
            "lo trae: `docker compose build agente`.[/red]"
        )
        raise typer.Exit(code=1) from e
    armado = Armado(ajustes, navegador, pausador)
    if "web" in nombres:
        buscador = armado.buscador_web()
        try:
            buscador.buscar("INEGI DENUE", n=1)
        except Exception as e:  # noqa: BLE001  (se reporta y se sale: el buscador base no responde)
            navegador.cerrar()
            console.print(f"[red]   ✖ el buscador {buscador.nombre} no responde: {e}[/red]")
            raise typer.Exit(code=1) from e
    try:
        fuentes = armado.fuentes(nombres)
    except ValueError as e:
        navegador.cerrar()
        console.print(f"[red]   ✖ {e}[/red]")
        raise typer.Exit(code=1) from e
    return navegador, fuentes


def _juez(modelo: str | None) -> Any:
    """El juez, con `--modelo-juez`, `JUEZ_MODEL` o el modelo del equipo, en ese orden."""
    from src.agents.survival.juez import Juez
    from src.agents.survival.recoleccion import presupuesto_de

    ajustes = get_settings()
    cliente = _cliente(modelo or ajustes.JUEZ_MODEL or ajustes.OLLAMA_MODEL)
    return Juez(
        cliente,
        version=ajustes.AGENTE_VERSION,
        max_intentos=ajustes.AGENTE_MAX_INTENTOS,
        presupuesto=presupuesto_de(ajustes.OLLAMA_CONTEXTO_TOKENS),
    )


@app.command()
def verificar(
    muestra: str = typer.Option(..., "--muestra", help="Nombre de la muestra a verificar."),
    limite: int | None = typer.Option(
        None, "--limite", help="Verificar a lo más N pendientes (1, 10...)."
    ),
    clee: str | None = typer.Option(None, "--clee", help="Verificar solo este negocio."),
    modelo: str | None = typer.Option(
        None, "--modelo", help="Tag de Ollama del equipo; default OLLAMA_MODEL."
    ),
    con_juez: bool = typer.Option(
        True, "--juez/--no-juez", help="Auditar cada veredicto en cuanto se guarda."
    ),
    modelo_juez: str | None = typer.Option(
        None, "--modelo-juez", help="Tag de Ollama del juez; default JUEZ_MODEL o el del equipo."
    ),
) -> None:
    """El equipo verifica en línea cada negocio pendiente y el juez audita su veredicto."""
    from src.agents.survival.recoleccion import presupuesto_de
    from src.agents.survival.verificador import Equipo, resumen_muestra, verificar_muestra
    from src.meta.tracker import MetadataTracker
    from src.shared.db.session import get_engine

    ajustes = get_settings()
    cliente = _cliente(modelo or ajustes.OLLAMA_MODEL)
    juez = _juez(modelo_juez) if con_juez else None
    navegador, fuentes = _fuentes()
    equipo = Equipo(
        cliente,
        fuentes,
        version=ajustes.AGENTE_VERSION,
        max_ampliaciones=ajustes.AGENTE_MAX_AMPLIACIONES,
        max_intentos=ajustes.AGENTE_MAX_INTENTOS,
        presupuesto=presupuesto_de(ajustes.OLLAMA_CONTEXTO_TOKENS),
    )
    engine = get_engine()
    with engine.connect() as conn:
        try:
            resumen = resumen_muestra(conn, muestra, equipo.agente)
        except ValueError as e:
            navegador.cerrar()
            console.print(f"[red]   ✖ {e}[/red]")
            raise typer.Exit(code=1) from e
    console.print(
        f"[bold magenta]   {equipo.agente} · juez {juez.juez if juez else 'apagado'} · "
        f"muestra {muestra!r}: {resumen['pendientes']}/{resumen['total']} pendientes[/bold magenta]"
    )
    console.print(f"   fuentes: {', '.join(f.recolector for f in fuentes)}")
    try:
        conteos = verificar_muestra(
            engine,
            MetadataTracker(),
            equipo,
            muestra,
            limite=limite,
            solo_clee=clee,
            juez=juez,
        )
    finally:
        navegador.cerrar()
    console.print(f"   {conteos}")
    if conteos["fallidos"]:
        raise typer.Exit(code=1)


@app.command()
def sondear(
    muestra: str = typer.Option(..., "--muestra", help="Muestra de donde sale el negocio."),
    clee: str | None = typer.Option(
        None, "--clee", help="El negocio a sondear; default el primero de la muestra."
    ),
    consulta: str | None = typer.Option(
        None, "--consulta", help="En vez de un negocio: esta búsqueda en cada buscador."
    ),
) -> None:
    """Sin modelo ni escritura: corre la recolección base y dice qué trajo cada fuente.

    Es para revisar las fuentes antes de una corrida (un buscador que cambió
    su HTML, un CAPTCHA, una lista del SAT que se movió). El expediente
    completo queda en `data/agente/sondeo_<clee>.txt`.
    """
    from pathlib import Path

    from sqlalchemy import text

    from src.agents.survival import insumo
    from src.agents.survival.recoleccion import FuenteWeb, agrupar, formatear_expediente

    navegador, fuentes = _fuentes()
    try:
        if consulta:
            tabla = Table(title=f"buscadores · {consulta!r}")
            for col in ("buscador", "resultados", "primero"):
                tabla.add_column(col)
            for f in fuentes:
                if not isinstance(f, FuenteWeb):
                    continue
                try:
                    rs = f.buscador.buscar(consulta, n=get_settings().AGENTE_MAX_RESULTADOS)
                    tabla.add_row(f.recolector, str(len(rs)), rs[0].url if rs else "—")
                except Exception as e:  # noqa: BLE001  (el sondeo reporta, no se cae)
                    tabla.add_row(f.recolector, "ERROR", f"{e.__class__.__name__}: {e}"[:120])
            console.print(tabla)
            return

        from src.shared.db.session import get_engine

        with get_engine().connect() as conn:
            try:
                muestra_id, _ = insumo.muestra_por_nombre(conn, muestra)
            except ValueError as e:
                console.print(f"[red]   ✖ {e}[/red]")
                raise typer.Exit(code=1) from e
            if clee is None:
                clee = conn.execute(
                    text(
                        "SELECT clee FROM verificacion.muestra_negocios "
                        "WHERE muestra_id = :id ORDER BY orden LIMIT 1"
                    ),
                    {"id": muestra_id},
                ).scalar_one()
            negocio = insumo.cargar(conn, muestra_id, clee)
        console.print(
            f"[bold magenta]   {clee} · {negocio.nombre!r} · {negocio.municipio}[/bold magenta]"
        )

        import time

        expediente: list = []
        tabla = Table(title="recolección base")
        for col in (
            "especialista",
            "fuente",
            "piezas",
            "ok",
            "sin res.",
            "error",
            "segundos",
            "nota",
        ):
            tabla.add_column(col)
        for grupo in agrupar(fuentes):
            for f in grupo.fuentes:
                t0 = time.monotonic()
                nuevas = f.recolectar(negocio, expediente)
                segundos = time.monotonic() - t0
                expediente.extend(nuevas)
                errores = [p for p in nuevas if p.estado == "error"]
                nota = (
                    errores[0].contenido[:90]
                    if errores
                    else (nuevas[0].url or "" if nuevas else "")
                )
                tabla.add_row(
                    grupo.especialidad,
                    f.recolector,
                    str(len(nuevas)),
                    str(sum(p.estado == "ok" for p in nuevas)),
                    str(sum(p.estado == "sin_resultados" for p in nuevas)),
                    str(len(errores)),
                    f"{segundos:.0f}",
                    nota,
                )
        console.print(tabla)
        destino = Path("data/agente") / f"sondeo_{clee}.txt"
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(formatear_expediente(expediente), encoding="utf-8")
        console.print(f"   expediente completo ({len(expediente)} piezas): {destino}")
    finally:
        navegador.cerrar()


@app.command()
def auditar(
    muestra: str = typer.Option(..., "--muestra", help="Nombre de la muestra."),
    agente: str | None = typer.Option(
        None, "--agente", help="Solo los veredictos de este modelo/version; default todos."
    ),
    limite: int | None = typer.Option(None, "--limite", help="Auditar a lo más N."),
    modelo: str | None = typer.Option(
        None, "--modelo", help="Tag de Ollama del juez; default JUEZ_MODEL o OLLAMA_MODEL."
    ),
) -> None:
    """El juez audita los veredictos que todavía no auditó (con expediente: v4 en adelante)."""
    from src.agents.survival.juez import auditar_muestra
    from src.meta.tracker import MetadataTracker
    from src.shared.db.session import get_engine

    juez = _juez(modelo)
    console.print(f"[bold magenta]   juez {juez.juez} · muestra {muestra!r}[/bold magenta]")
    try:
        conteos = auditar_muestra(
            get_engine(), MetadataTracker(), juez, muestra, agente=agente, limite=limite
        )
    except ValueError as e:
        console.print(f"[red]   ✖ {e}[/red]")
        raise typer.Exit(code=1) from e
    console.print(f"   {conteos}")
    if conteos["fallidos"]:
        raise typer.Exit(code=1)


@app.command()
def metricas(
    muestra: str = typer.Option(..., "--muestra", help="Nombre de la muestra."),
    agente: str | None = typer.Option(
        None, "--agente", help="modelo/version a contar; default el más reciente de cualquiera."
    ),
    solo_sostenidos: bool = typer.Option(
        False, "--solo-sostenidos", help="Contar solo los veredictos que el juez sostuvo."
    ),
) -> None:
    """La tabla de confusión por estrato (gold contra el equipo) y lo que dijo el juez."""
    from sqlalchemy import text

    from src.agents.survival.metricas import (
        auditoria,
        calcular,
        precision_cerrado_reponderada,
        tasa_falsos_negativos_reponderada,
        tiempos,
    )
    from src.shared.db.session import get_engine

    with get_engine().connect() as conn:
        try:
            filas = calcular(conn, muestra, agente, solo_sostenidos=solo_sostenidos)
            jueces = auditoria(conn, muestra, agente)
            t = tiempos(conn, muestra, agente)
            total = conn.execute(
                text(
                    "SELECT count(*) FROM verificacion.muestra_negocios mn "
                    "JOIN verificacion.muestras m USING (muestra_id) WHERE m.nombre = :n"
                ),
                {"n": muestra},
            ).scalar_one()
        except ValueError as e:
            console.print(f"[red]   ✖ {e}[/red]")
            raise typer.Exit(code=1) from e

    def pct(v: float | None) -> str:
        return "—" if v is None else f"{100 * v:.1f}%"

    titulo = f"muestra {muestra!r}" + (f" · {agente}" if agente else "")
    tabla = Table(title=titulo + (" · solo sostenidos" if solo_sostenidos else ""))
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

    tabla_juez = Table(title="el juez")
    for col in ("estrato", "auditados", "sostenidos", "acuerdo a ciegas"):
        tabla_juez.add_column(col, justify="right" if col != "estrato" else "left")
    for j in jueces:
        tabla_juez.add_row(
            j.estrato,
            str(j.auditados),
            f"{j.sostenidos} ({pct(j.tasa_sostenidos)})",
            f"{j.acuerdo} ({pct(j.tasa_acuerdo)})",
        )
    console.print(tabla_juez)

    def minutos(v: float | None) -> str:
        return "—" if v is None else f"{v:.1f} min"

    console.print(
        f"   tiempo por negocio ({t.verificados} verificados): promedio {minutos(t.promedio_min)}, "
        f"mediana {minutos(t.mediana_min)}, p90 {minutos(t.p90_min)}; "
        f"juez {minutos(t.juez_promedio_min)}"
    )
    for especialista, informes, segundos in t.por_especialista:
        console.print(
            f"      {especialista:<12} {informes:>5} informes · {segundos:6.0f} s promedio"
        )
    faltan = max(int(total) - t.verificados, 0)
    for n in sorted({faltan, 400, 1000} - {0}):
        horas = t.proyeccion_horas(n)
        if horas is not None:
            etiqueta = "los que faltan de esta muestra" if n == faltan else "negocios"
            console.print(f"   a este ritmo, {n} {etiqueta}: {horas:.1f} h ({horas / 24:.1f} días)")


if __name__ == "__main__":
    app()
