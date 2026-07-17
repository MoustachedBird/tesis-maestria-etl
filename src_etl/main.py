from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from src_etl.extractors.denue import DenueExtractor

app = typer.Typer(help="ETL Pipeline para DENUE - Proyecto de Tesis")
console = Console()

# Project paths
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT / "data"
BRONZE_DIR = DATA_ROOT / "bronze" / "denue"


# ---------------------------------------------------------------------------
# EXTRACT
# ---------------------------------------------------------------------------
@app.command()
def extract(
    source: str = typer.Option("denue", "--source", "-s", help="Data source to extract"),
    headless: bool = typer.Option(True, help="Run browser in headless mode"),
    output: Path = typer.Option(BRONZE_DIR, help="Output directory for downloaded files"),
    force: bool = typer.Option(False, "--force", help="Re-download all files"),
):
    """
    STEP 1: Extract raw data from source.

    Downloads data files from the configured source (default: DENUE/INEGI).
    """
    if source != "denue":
        console.print(f"[bold red]Unknown source: {source}. Available: denue[/bold red]")
        raise typer.Exit(code=1)

    console.print("[bold green]Starting DENUE Extractor[/bold green]")
    console.print(f"   Output: {output}")
    console.print(f"   Headless: {headless}")

    if force:
        console.print("[bold yellow]   FORCE MODE: Re-downloading ALL files[/bold yellow]")

    downloader = DenueExtractor(output_dir=output, headless=headless, force=force)
    downloader.download_all()


# ---------------------------------------------------------------------------
# TRANSFORM
# ---------------------------------------------------------------------------
@app.command()
def transform(
    source: str = typer.Option("denue", "--source", "-s", help="Data source to transform"),
    ingest: bool = typer.Option(True, "--ingest/--no-ingest", help="Ingest raw CSVs into Silver"),
    normalize_dates: bool = typer.Option(
        True,
        "--normalize-dates/--no-normalize-dates",
        help="Normalize fecha_alta dates in Silver",
    ),
    h3_cells: bool = typer.Option(
        True,
        "--h3-cells/--no-h3-cells",
        help="Backfill H3 cells (h3_cell_9/h3_cell_5) from coordinates",
    ),
    clee_resolution: bool = typer.Option(
        True, "--clee-resolution/--no-clee-resolution", help="Run CLEE resolution sub-step"
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Force reprocess all files"),
):
    """
    STEP 2: Transform raw data (Bronze -> Silver).

    Cleans, normalizes, and loads data into the Silver DuckDB warehouse.
    Sub-steps: ingest -> normalize-dates -> h3-cells -> clee-resolution.
    Control each with --ingest, --normalize-dates, --h3-cells and
    --clee-resolution flags.
    """
    if source != "denue":
        console.print(f"[bold red]Unknown source: {source}. Available: denue[/bold red]")
        raise typer.Exit(code=1)

    console.print("[bold blue]Starting DENUE Transform[/bold blue]")

    if force:
        console.print("[bold orange3]   FORCE MODE: Reprocessing all files[/bold orange3]")

    try:
        from src_etl.transformers.denue import DenuePipelineOrchestrator

        transformer = DenuePipelineOrchestrator(data_root=DATA_ROOT)
        success = True

        if ingest:
            console.print("[bold blue]   Running: ingest[/bold blue]")
            success = transformer.run_transform_stage(force=force)

        if normalize_dates and success:
            console.print("[bold blue]   Running: normalize-dates[/bold blue]")
            success = transformer.run_normalize_dates_stage(force=force)

        if h3_cells and success:
            console.print("[bold blue]   Running: h3-cells[/bold blue]")
            success = transformer.run_h3_cells_stage(force=force)

        if clee_resolution and success:
            console.print("[bold magenta]   Running: clee-resolution[/bold magenta]")
            success = transformer.run_clee_resolution_stage(force=force)

        if success:
            console.print("[bold green]Transform completed successfully.[/bold green]")
        else:
            console.print("[bold yellow]Transform completed with errors. Check logs.[/bold yellow]")
            raise typer.Exit(code=1)

    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[bold red]Transform failed: {escape(str(e))}[/bold red]")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# LOAD
# ---------------------------------------------------------------------------
@app.command()
def load(
    source: str = typer.Option("denue", "--source", "-s", help="Data source to load"),
    force: bool = typer.Option(
        False, "--force", "-f", help="Force load even if Gold is up-to-date"
    ),
):
    """
    STEP 3: Load Silver -> Gold.

    Promotes the Silver DuckDB warehouse to Gold for production use.
    """
    if source != "denue":
        console.print(f"[bold red]Unknown source: {source}. Available: denue[/bold red]")
        raise typer.Exit(code=1)

    console.print("[bold cyan]Starting DENUE Loader[/bold cyan]")

    try:
        from src_etl.transformers.denue import DenuePipelineOrchestrator

        transformer = DenuePipelineOrchestrator(data_root=DATA_ROOT)
        success = transformer.run_load_stage(force=force)

        if success:
            console.print("[bold green]Load completed successfully.[/bold green]")
        else:
            console.print("[bold yellow]Load failed. Check logs.[/bold yellow]")
            raise typer.Exit(code=1)

    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[bold red]Load failed: {escape(str(e))}[/bold red]")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# PIPELINE
# ---------------------------------------------------------------------------
@app.command()
def pipeline(
    source: str = typer.Option("denue", "--source", "-s", help="Data source"),
    force: bool = typer.Option(False, "--force", "-f", help="Force reprocess all files"),
):
    """
    Run full pipeline: Extract -> Transform -> Load.

    Executes the complete ETL chain with tracking in metadata.duckdb.
    """
    if source != "denue":
        console.print(f"[bold red]Unknown source: {source}. Available: denue[/bold red]")
        raise typer.Exit(code=1)

    console.print("[bold cyan]Starting Full Pipeline (DENUE)[/bold cyan]")
    console.print("=" * 60)

    try:
        from src_etl.transformers.denue import DenuePipelineOrchestrator

        transformer = DenuePipelineOrchestrator(data_root=DATA_ROOT)
        success = transformer.run_full_pipeline(force=force)

        console.print("=" * 60)
        if success:
            console.print("[bold green]Pipeline completed successfully![/bold green]")

            status = transformer.get_pipeline_status()
            console.print("\n[bold]Pipeline Summary:[/bold]")
            console.print(f"  Silver: {status['silver_db']}")
            console.print(f"  Gold: {status['gold_db']}")
            console.print(f"  Metadata: {status['metadata_db']}")
        else:
            console.print("[bold yellow]Pipeline completed with errors. Check logs.[/bold yellow]")
            raise typer.Exit(code=1)

    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[bold red]Pipeline failed: {escape(str(e))}[/bold red]")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# RETRY (placeholder)
# ---------------------------------------------------------------------------
@app.command()
def retry(
    source: str = typer.Option("denue", "--source", "-s", help="Data source to retry"),
    output: Path = typer.Option(BRONZE_DIR, help="Output directory"),
):
    """
    Retry failed extractions based on metadata history.
    """
    console.print("[bold yellow]Retrying failed downloads[/bold yellow]")
    console.print("[yellow]Retry functionality coming soon - use extract to re-run[/yellow]")


if __name__ == "__main__":
    app()
