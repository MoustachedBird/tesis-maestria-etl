import typer
from pathlib import Path
from rich.console import Console

app = typer.Typer()
console = Console()

@app.command()
def download(
    headless: bool = True,
    output: Path = typer.Option(Path("data/raw/denue"), help="Carpeta destino")
):
    """Descarga masiva del DENUE"""
    console.print(f"[green]Iniciando descarga en: {output}[/green]")
    # Aquí iría tu lógica de Selenium
    pass

@app.command()
def retry(reporte: Path):
    """Reintentar descargas fallidas"""
    console.print(f"[yellow]Reintentando desde reporte: {reporte}[/yellow]")
    pass
