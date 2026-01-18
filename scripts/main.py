import typer
from pathlib import Path
from rich.console import Console
from rich.markup import escape

# Importación de orquestadores modulares
from scripts.src.denue_downloader import InegiDownloader
from scripts.src.processor import DenueProcessor
from scripts.src.harmonizer import DenueHarmonizer

# Configuración de la CLI
app = typer.Typer(help="ETL Pipeline para DENUE - Proyecto de Tesis")
console = Console()

# Definición de rutas raíz (basadas en la ubicación de este archivo)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT / "data"
RAW_DIR = DATA_ROOT / "raw" / "denue"

@app.command()
def download_inegi(
    headless: bool = typer.Option(True, help="Ejecutar navegador en modo headless (sin ventana)"),
    output: Path = typer.Option(RAW_DIR, help="Carpeta de destino para los archivos ZIP")
):
    """
    PASO 1: Descarga masiva de datos DENUE desde el sitio de INEGI.
    Sincroniza lo que hay en disco con el reporte de control en reports.db.
    """
    console.print(f"[bold green]🚀 Iniciando Inegi Downloader[/bold green]")
    console.print(f"📂 Destino: {output}")
    console.print(f"👻 Modo Headless: {headless}")

    downloader = InegiDownloader(output_dir=output, headless=headless)
    downloader.download_all()

@app.command()
def retry_inegi(
    output: Path = typer.Option(RAW_DIR, help="Carpeta que contiene los ZIPs y el reporte a reintentar")
):
    """
    PASO 1.5: Reintenta descargas fallidas basándose en el historial de reports.db.
    """
    console.print(f"[bold yellow]🔄 Reintentando descargas fallidas[/bold yellow]")
    downloader = InegiDownloader(output_dir=output, headless=True)
    downloader.retry()

@app.command()
def process_denue(
    force: bool = typer.Option(
        False, "--force", "-f", 
        help="Forzar el procesamiento de todos los archivos ZIP, ignorando si ya fueron marcados como exitosos (DONE)."
    )
):
    """
    PASO 2: Procesar ZIPs Raw -> Warehouse Centralizado (DuckDB).
    Limpia, normaliza encodings (UTF-8/Latin-1) y sincroniza catálogos de actividades y entidades.
    """
    console.print(f"[bold blue]⚙️ Iniciando DENUE Warehouse Processor[/bold blue]")
    if force:
        console.print("[bold orange3]⚠️ MODO FORCE: Se re-procesarán todos los archivos en disco.[/bold orange3]")
    
    try:
        # DenueProcessor localiza automáticamente reports.db y denue_warehouse.db en DATA_ROOT
        processor = DenueProcessor(data_root=DATA_ROOT)
        
        # Ejecuta el pipeline (Limpieza -> Sincronización de Catálogos -> Carga de Hechos)
        processor.run_pipeline(force=force)
        
        console.print(f"[bold green]✨ Sincronización con Warehouse finalizada correctamente.[/bold green]")
        
    except Exception as e:
        console.print(f"[bold red]❌ Error en el procesamiento: {escape(str(e))}[/bold red]")
        raise typer.Exit(code=1)

@app.command()
def harmonize():
    """
    PASO 3: Resolución de Identidad (Capa Gold).
    Vincula establecimientos a través del tiempo generando el catálogo maestro de identidades.
    """
    console.print(f"[bold magenta]🔗 Iniciando Resolución de Identidad (Harmonizer)[/bold magenta]")
    
    try:
        # El Harmonizer ahora leerá directamente de las tablas de DuckDB en el Warehouse
        harmonizer = DenueHarmonizer(project_root=PROJECT_ROOT)
        harmonizer.run()
        
        console.print(f"[bold green]✅ Pipeline de Armonización finalizado con éxito.[/bold green]")
        
    except Exception as e:
        console.print(f"[bold red]❌ Error en armonización: {escape(str(e))}[/bold red]")
        raise typer.Exit(code=1)

if __name__ == "__main__":
    app()