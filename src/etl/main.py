from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from src.etl.extractors.denue import DenueExtractor
from src.shared.enums import DENUE_ATOMIC, SECTION_GROUPS, Section

app = typer.Typer(help="ETL Pipeline para DENUE - Proyecto de Tesis")
console = Console()

# Project paths
# parents[2] y no [1]: este modulo vive en src/etl/, asi que la raiz del
# proyecto esta dos niveles arriba (antes era src_etl/ y bastaba con uno).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = PROJECT_ROOT / "data"
BRONZE_DIR = DATA_ROOT / "bronze" / "denue"


# ---------------------------------------------------------------------------
# SECTION SELECTION (--only allowlist mode)
# ---------------------------------------------------------------------------
# Section, DENUE_ATOMIC y SECTION_GROUPS viven en src/shared/enums/sections.py:
# el scheduler nombra la sección de cada tick y el API las valida al encolar,
# y ninguno debe importar del CLI del ETL.


def _expand_only(only: list[Section] | None) -> set[Section] | None:
    """Expand an ``--only`` selection into the set of ATOMIC sections to run.

    Returns None when nothing was passed, so callers keep the default
    subtractive mode (everything ON, opt out with ``--no-*``).
    """
    if not only:
        return None
    atomic: set[Section] = set()
    for sec in only:
        atomic.update(SECTION_GROUPS.get(sec, (sec,)))
    return atomic


# ---------------------------------------------------------------------------
# SHARED STEP HELPERS (reused by extract / transform / pipeline)
# ---------------------------------------------------------------------------
def _run_denue_extract(*, headless: bool, output: Path | None, force: bool) -> None:
    """Download the DENUE Bronze layer via Selenium into `output` (or BRONZE_DIR)."""
    denue_output = output or BRONZE_DIR
    console.print("[bold green]Starting DENUE Extractor[/bold green]")
    console.print(f"   Output: {denue_output}")
    console.print(f"   Headless: {headless}")
    DenueExtractor(output_dir=denue_output, headless=headless, force=force).download_all()


def _run_sesnsp_extract(*, force: bool) -> None:
    """Download the SESNSP crime CSV (CKAN) into data/bronze/sesnsp/."""
    from src.etl.extractors.sesnsp import SesnspExtractor

    sesnsp_output = DATA_ROOT / "bronze" / "sesnsp"
    console.print("[bold green]Starting SESNSP Extractor[/bold green]")
    console.print(f"   Output: {sesnsp_output}")
    SesnspExtractor(output_dir=sesnsp_output, force=force).download_all()


def _run_denue_transform(
    *,
    ingest: bool,
    clee_resolution_radio: bool,
    clee_resolution_h3: bool,
    force: bool,
    periods: list[str] | None = None,
) -> bool:
    """Run the enabled DENUE Bronze->Silver sub-steps in order; return True on success."""
    from src.etl.transformers.denue import DenuePipelineOrchestrator

    transformer = DenuePipelineOrchestrator(data_root=DATA_ROOT)
    success = True

    if ingest:
        console.print("[bold blue]   Running: ingest[/bold blue]")
        success = transformer.run_transform_stage(force=force, periods=periods)
    if clee_resolution_radio and success:
        console.print("[bold magenta]   Running: clee-resolution (radio)[/bold magenta]")
        success = transformer.run_clee_resolution_stage(force=force)
    if clee_resolution_h3 and success:
        console.print("[bold magenta]   Running: clee-resolution (h3)[/bold magenta]")
        success = transformer.run_clee_resolution_h3_stage(force=force)
    return success


def _run_sesnsp_transform(*, force: bool) -> bool:
    """Run the SESNSP CSV -> Silver transform; return True on success."""
    from src.etl.transformers.sesnsp import SesnspProcessor

    processor = SesnspProcessor(data_root=DATA_ROOT)
    return processor.run(force=force)


# ---------------------------------------------------------------------------
# EXTRACT
# ---------------------------------------------------------------------------
@app.command()
def extract(
    denue: bool = typer.Option(
        True, "--denue/--no-denue", help="Extract DENUE (INEGI, Selenium scraping)"
    ),
    sesnsp: bool = typer.Option(
        True, "--sesnsp/--no-sesnsp", help="Extract SESNSP crime data (datos.gob.mx, CKAN API)"
    ),
    only: list[Section] = typer.Option(
        None,
        "--only",
        help="Run ONLY these sources (repeatable: denue, sesnsp); overrides --no-* defaults",
    ),
    headless: bool = typer.Option(True, help="Run browser in headless mode (denue only)"),
    output: Path = typer.Option(None, help="Output directory override (denue only)"),
    force: bool = typer.Option(False, "--force", help="Re-download all files"),
):
    """
    STEP 1: Extract raw data (Bronze).

    Runs EVERY source by default -- DENUE (INEGI, Selenium) and SESNSP
    (datos.gob.mx, CKAN). Opt out with --no-denue / --no-sesnsp, or allowlist
    with --only (e.g. --only sesnsp); with no flags a single `make extract`
    fetches both.
    """
    selected = _expand_only(only)
    if selected is not None:
        # groups are already expanded to atomic sections; any DENUE sub-step selects the source
        denue = bool(selected & set(DENUE_ATOMIC))
        sesnsp = Section.sesnsp in selected

    if force:
        console.print("[bold yellow]   FORCE MODE: Re-downloading ALL files[/bold yellow]")

    if denue:
        _run_denue_extract(headless=headless, output=output, force=force)

    if sesnsp:
        _run_sesnsp_extract(force=force)


# ---------------------------------------------------------------------------
# SYNC-BRONZE
# ---------------------------------------------------------------------------
@app.command("sync-bronze")
def sync_bronze_cmd(
    bronze: Path = typer.Option(
        None, "--bronze", help="Directorio de Bronze. Default: data/bronze/denue"
    ),
):
    """
    Registra en metadata los ZIP que ya estan en Bronze.

    El ingest lee de metadata que archivos hay, no del disco. Esto hace
    falta cuando Bronze llego por una via distinta a `extract`: una copia
    desde otra maquina, un respaldo restaurado, un ZIP puesto a mano.

    Es idempotente: lo ya registrado se omite.
    """
    from src.etl.extractors.bronze_sync import sync_bronze
    from src.meta.tracker import MetadataTracker

    bronze_dir = bronze or DATA_ROOT / "bronze" / "denue"
    console.print(f"[bold cyan]Sincronizando Bronze desde {bronze_dir}[/bold cyan]")

    try:
        registrados = sync_bronze(bronze_dir, MetadataTracker())
    except FileNotFoundError as e:
        console.print(f"[bold red]{escape(str(e))}[/bold red]")
        raise typer.Exit(code=1)

    if not registrados:
        console.print("[bold green]Nada nuevo: todo lo de Bronze ya estaba registrado.[/bold green]")
        return

    for period, archivos in sorted(registrados.items()):
        console.print(f"   {period}: {archivos} archivo(s)")
    console.print(
        f"[bold green]{sum(registrados.values())} archivo(s) en "
        f"{len(registrados)} corte(s) registrados.[/bold green]"
    )


# ---------------------------------------------------------------------------
# TRANSFORM
# ---------------------------------------------------------------------------
@app.command()
def transform(
    denue: bool = typer.Option(
        True, "--denue/--no-denue", help="Run the DENUE Bronze->Silver pipeline"
    ),
    ingest: bool = typer.Option(True, "--ingest/--no-ingest", help="Ingest raw CSVs into Silver"),
    clee_resolution_radio: bool = typer.Option(
        True,
        "--clee-resolution-radio/--no-clee-resolution-radio",
        help="Run CLEE resolution (radio/BallTree -> clee_propagado_radio)",
    ),
    clee_resolution_h3: bool = typer.Option(
        True,
        "--clee-resolution-h3/--no-clee-resolution-h3",
        help="Run CLEE resolution (H3 blocking -> clee_propagado_h3), comparison track",
    ),
    sesnsp: bool = typer.Option(
        True,
        "--sesnsp/--no-sesnsp",
        help="Run the SESNSP transform (CSV -> sesnsp_incidencia_municipal)",
    ),
    only: list[Section] = typer.Option(
        None,
        "--only",
        help=(
            "Run ONLY these sections (repeatable): ingest, clee-radio, clee-h3, "
            "sesnsp (+ groups denue, clee). Overrides --no-* defaults."
        ),
    ),
    period: list[str] = typer.Option(
        None,
        "--period",
        help=(
            "Cargar SOLO estos cortes (repetible), p. ej. --period 03_2018. "
            "Util para estrenar el ingest con uno antes de soltar los veinte."
        ),
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Force reprocess all files"),
):
    """
    STEP 2: Transform raw data (Bronze -> Silver).

    Runs EVERY section by default so a single `make transform` populates the
    whole Silver warehouse. Opt out of any section with a --no-* flag, or
    allowlist with --only (e.g. --only clee-radio --only sesnsp); nothing needs
    to be passed for the full run.

    DENUE sub-steps (ordered): ingest -> clee-resolution-radio ->
    clee-resolution-h3, each gated by its flag (--no-denue skips the whole
    DENUE block). El ingest deriva la fecha y las celdas H3 en vuelo, asi
    que ya no hay pasos sueltos para eso.
    SESNSP: CSV consolidado -> sesnsp_incidencia_municipal (--no-sesnsp skips).
    """
    selected = _expand_only(only)
    if selected is not None:
        ingest = Section.ingest in selected
        clee_resolution_radio = Section.clee_radio in selected
        clee_resolution_h3 = Section.clee_h3 in selected
        sesnsp = Section.sesnsp in selected
        denue = ingest or clee_resolution_radio or clee_resolution_h3

    if force:
        console.print("[bold orange3]   FORCE MODE: Reprocessing all data[/bold orange3]")

    overall_ok = True

    if denue:
        console.print("[bold blue]== DENUE Transform ==[/bold blue]")
        try:
            overall_ok = (
                _run_denue_transform(
                    ingest=ingest,
                    clee_resolution_radio=clee_resolution_radio,
                    clee_resolution_h3=clee_resolution_h3,
                    force=force,
                    periods=period,
                )
                and overall_ok
            )
        except Exception as e:
            console.print(f"[bold red]DENUE transform failed: {escape(str(e))}[/bold red]")
            raise typer.Exit(code=1)

    if sesnsp:
        console.print("[bold blue]== SESNSP Transform ==[/bold blue]")
        try:
            if not _run_sesnsp_transform(force=force):
                console.print("[bold yellow]SESNSP transform failed. Check logs.[/bold yellow]")
                overall_ok = False
        except Exception as e:
            console.print(f"[bold red]SESNSP transform failed: {escape(str(e))}[/bold red]")
            raise typer.Exit(code=1)

    if overall_ok:
        console.print("[bold green]Transform completed successfully.[/bold green]")
    else:
        console.print("[bold yellow]Transform completed with errors. Check logs.[/bold yellow]")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# PIPELINE
# ---------------------------------------------------------------------------
@app.command()
def pipeline(
    run_extract: bool = typer.Option(
        False,
        "--extract/--no-extract",
        help="Run extract (DENUE Selenium + SESNSP) before transform; OFF by default",
    ),
    denue: bool = typer.Option(True, "--denue/--no-denue", help="Include the DENUE source"),
    ingest: bool = typer.Option(True, "--ingest/--no-ingest", help="Ingest raw CSVs into Silver"),
    clee_resolution_radio: bool = typer.Option(
        True,
        "--clee-resolution-radio/--no-clee-resolution-radio",
        help="Run CLEE resolution (radio/BallTree)",
    ),
    clee_resolution_h3: bool = typer.Option(
        True,
        "--clee-resolution-h3/--no-clee-resolution-h3",
        help="Run CLEE resolution (H3 blocking), comparison track",
    ),
    sesnsp: bool = typer.Option(True, "--sesnsp/--no-sesnsp", help="Include the SESNSP source"),
    only: list[Section] = typer.Option(
        None,
        "--only",
        help=(
            "Run ONLY these sections (repeatable): ingest, "
            "clee-radio, clee-h3, sesnsp (+ groups denue, clee). Overrides --no-*."
        ),
    ),
    headless: bool = typer.Option(True, help="Run browser in headless mode (denue extract only)"),
    force: bool = typer.Option(False, "--force", "-f", help="Force reprocess all files"),
):
    """
    Run the full pipeline globally: [Extract ->] Transform.

    Like `transform`, this runs EVERY section by default -- DENUE (all
    sub-steps incl. CLEE) + SESNSP -> Silver. Opt out of any section with a
    --no-* flag, or allowlist with --only. Gold no existe todavia: la Fase 5
    define que significa derivarlo. Extract is OFF by default (re-scrapes INEGI);
    enable it with --extract.
    """
    selected = _expand_only(only)
    if selected is not None:
        ingest = Section.ingest in selected
        clee_resolution_radio = Section.clee_radio in selected
        clee_resolution_h3 = Section.clee_h3 in selected
        sesnsp = Section.sesnsp in selected
        denue = ingest or clee_resolution_radio or clee_resolution_h3

    console.print("[bold cyan]Starting Full Pipeline[/bold cyan]")
    console.print("=" * 60)
    if force:
        console.print("[bold orange3]   FORCE MODE: Reprocessing all data[/bold orange3]")

    overall_ok = True

    try:
        from src.etl.transformers.denue import DenuePipelineOrchestrator

        if run_extract:
            console.print("[bold cyan]== Extract ==[/bold cyan]")
            if denue:
                _run_denue_extract(headless=headless, output=None, force=force)
            if sesnsp:
                _run_sesnsp_extract(force=force)

        if denue:
            console.print("[bold blue]== DENUE Transform ==[/bold blue]")
            overall_ok = (
                _run_denue_transform(
                    ingest=ingest,
                    clee_resolution_radio=clee_resolution_radio,
                    clee_resolution_h3=clee_resolution_h3,
                    force=force,
                )
                and overall_ok
            )

        if sesnsp:
            console.print("[bold blue]== SESNSP Transform ==[/bold blue]")
            if not _run_sesnsp_transform(force=force):
                console.print("[bold yellow]SESNSP transform failed. Check logs.[/bold yellow]")
                overall_ok = False

        console.print("=" * 60)
        if overall_ok:
            console.print("[bold green]Pipeline completed successfully![/bold green]")
            status = DenuePipelineOrchestrator(data_root=DATA_ROOT).get_pipeline_status()
            console.print("\n[bold]Pipeline Summary:[/bold]")
            for etapa in ("latest_transform_run", "latest_clee_resolution_run"):
                resumen = status[etapa]
                console.print(
                    f"  {etapa}: {resumen['status']} "
                    f"({resumen['succeeded']} ok, {resumen['failed']} fallidos)"
                )
        else:
            console.print("[bold yellow]Pipeline completed with errors. Check logs.[/bold yellow]")
            raise typer.Exit(code=1)

    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[bold red]Pipeline failed: {escape(str(e))}[/bold red]")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# BUILD ENTIDADES (dimension N3)
# ---------------------------------------------------------------------------
@app.command()
def build_entidades():
    """
    Build the entidades_economicas dimension (one row per resolved business).

    Run AFTER CLEE resolution: aggregates establecimientos by
    clee_propagado_radio in Silver (full rebuild -- NEW_* ids are not
    stable across --force resolutions). Gold inherits on `make load`.
    """
    console.print("[bold blue]Building entidades_economicas dimension[/bold blue]")
    try:
        from src.etl.loaders.entidades import EntidadesDimensionBuilder

        builder = EntidadesDimensionBuilder(data_root=DATA_ROOT)
        if builder.run():
            console.print("[bold green]Dimension built successfully.[/bold green]")
        else:
            console.print("[bold yellow]Dimension build failed. Check logs.[/bold yellow]")
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[bold red]Dimension build failed: {escape(str(e))}[/bold red]")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# RETRY (placeholder)
# ---------------------------------------------------------------------------
@app.command()
def retry():
    """
    Retry failed extractions based on metadata history.
    """
    console.print("[bold yellow]Retrying failed downloads[/bold yellow]")
    console.print("[yellow]Retry functionality coming soon - use extract to re-run[/yellow]")


if __name__ == "__main__":
    app()
