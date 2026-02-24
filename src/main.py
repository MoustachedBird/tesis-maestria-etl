import typer
from pathlib import Path
from rich.console import Console
from rich.markup import escape

# Importación de orquestadores modulares
from src.extractors.denue_downloader import DenueDownloader
# Legacy imports kept for backward compatibility during migration
# TODO: Remove after full migration verification
# from src.transformers.denue_processor import DenueProcessor
# from src.transformers.denue_harmonizer import DenueHarmonizer

# Configuración de la CLI
app = typer.Typer(help="ETL Pipeline para DENUE - Proyecto de Tesis")
console = Console()

# Definición de rutas raíz (basadas en la ubicación de este archivo)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT / "data"
RAW_DIR = DATA_ROOT / "raw" / "denue"


@app.command()
def download_inegi(
    headless: bool = typer.Option(
        True, help="Ejecutar navegador en modo headless (sin ventana)"
    ),
    output: Path = typer.Option(
        RAW_DIR, help="Carpeta de destino para los archivos ZIP"
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="🔄 Re-download ALL files, ignoring metadata and disk cache",
    ),
):
    """
    PASO 1: Descarga masiva de datos DENUE desde el sitio de INEGI.
    Sincroniza lo que hay en disco con el reporte de control en reports.db.

    Use --force to re-download all files regardless of existing downloads.
    """
    console.print(f"[bold green]🚀 Iniciando DENUE Downloader (Phase 2)[/bold green]")
    console.print(f"📂 Destino: {output}")
    console.print(f"👻 Modo Headless: {headless}")

    if force:
        console.print(
            f"⚠️  [bold yellow]FORCE MODE: Re-downloading ALL files[/bold yellow]"
        )

    downloader = DenueDownloader(output_dir=output, headless=headless, force=force)
    downloader.download_all()


@app.command()
def retry_inegi(
    output: Path = typer.Option(
        RAW_DIR, help="Carpeta que contiene los ZIPs y el reporte a reintentar"
    ),
):
    """
    PASO 1.5: Reintenta descargas fallidas basándose en el historial de metadata.duckdb.
    """
    console.print(f"[bold yellow]🔄 Reintentando descargas fallidas[/bold yellow]")
    downloader = DenueDownloader(output_dir=output, headless=True)
    # Retry functionality will be added separately
    console.print(
        "[yellow]Retry functionality coming soon - use download-inegi to re-run[/yellow]"
    )


@app.command()
def process_denue(
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Forzar el procesamiento de todos los archivos ZIP, ignorando si ya fueron marcados como exitosos (DONE).",
    ),
):
    """
    PASO 2: Procesar ZIPs Raw -> Warehouse Centralizado (DuckDB).
    Limpia, normaliza encodings (UTF-8/Latin-1) y sincroniza catálogos de actividades y entidades.

    Usa el nuevo sistema de MetadataTracker para tracking completo.
    """
    console.print(f"[bold blue]⚙️ Iniciando DENUE Warehouse Processor[/bold blue]")
    if force:
        console.print(
            "[bold orange3]⚠️ MODO FORCE: Se re-procesarán todos los archivos en disco.[/bold orange3]"
        )

    try:
        # Use new unified transformer
        from src.transformers.denue_transformer import DenueTransformer

        transformer = DenueTransformer(data_root=DATA_ROOT)
        success = transformer.run_process_stage(force=force)

        if success:
            console.print(
                f"[bold green]✨ Sincronización con Warehouse finalizada correctamente.[/bold green]"
            )
        else:
            console.print(
                f"[bold yellow]⚠️ Proceso completado con errores. Revisa los logs.[/bold yellow]"
            )
            raise typer.Exit(code=1)

    except Exception as e:
        console.print(
            f"[bold red]❌ Error en el procesamiento: {escape(str(e))}[/bold red]"
        )
        raise typer.Exit(code=1)


@app.command()
def harmonize():
    """
    PASO 3: Resolución de Identidad (Capa Gold).
    Vincula establecimientos a través del tiempo generando el catálogo maestro de identidades.

    Usa el nuevo sistema de MetadataTracker para tracking completo.
    """
    console.print(
        f"[bold magenta]🔗 Iniciando Resolución de Identidad (Harmonizer)[/bold magenta]"
    )

    try:
        # Use new unified transformer
        from src.transformers.denue_transformer import DenueTransformer

        transformer = DenueTransformer(data_root=DATA_ROOT)
        success = transformer.run_harmonize_stage()

        if success:
            console.print(
                f"[bold green]✅ Pipeline de Armonización finalizado con éxito.[/bold green]"
            )
        else:
            console.print(
                f"[bold yellow]⚠️ Harmonización completada con errores. Revisa los logs.[/bold yellow]"
            )
            raise typer.Exit(code=1)

    except Exception as e:
        console.print(
            f"[bold red]❌ Error en armonización: {escape(str(e))}[/bold red]"
        )
        raise typer.Exit(code=1)


@app.command()
def pipeline(
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Forzar el procesamiento de todos los archivos ZIP.",
    ),
):
    """
    EJECUTAR PIPELINE COMPLETO: Download -> Process -> Harmonize

    Ejecuta la cadena completa de transformación de datos DENUE
    con tracking completo en metadata.duckdb.
    """
    console.print(f"[bold cyan]🚀 Iniciando Pipeline Completo DENUE[/bold cyan]")
    console.print("=" * 60)

    try:
        from src.transformers.denue_transformer import DenueTransformer

        transformer = DenueTransformer(data_root=DATA_ROOT)
        success = transformer.run_full_pipeline(force=force)

        console.print("=" * 60)
        if success:
            console.print(
                f"[bold green]🎉 Pipeline Completo finalizado con éxito![/bold green]"
            )

            # Show summary
            status = transformer.get_pipeline_status()
            console.print(f"\n[bold]📊 Resumen del Pipeline:[/bold]")
            console.print(f"  Processed: {status['processed_output']}")
            console.print(f"  Curated: {status['curated_output']}")
            console.print(f"  Metadata: {status['metadata_db']}")
        else:
            console.print(
                f"[bold yellow]⚠️ Pipeline completado con errores. Revisa los logs.[/bold yellow]"
            )
            raise typer.Exit(code=1)

    except Exception as e:
        console.print(f"[bold red]❌ Error en pipeline: {escape(str(e))}[/bold red]")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
