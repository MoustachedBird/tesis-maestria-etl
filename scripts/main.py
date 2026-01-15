import typer
from pathlib import Path
from rich.console import Console
from scripts.sources.inegi import InegiDownloader

# Help text explaining the CLI purpose
app = typer.Typer(help="ETL Scripts Runner for the Thesis (Extract, Transform, Load)")
console = Console()

# Define default paths (relative to project root)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data" / "raw" / "denue"

@app.command()
def download_inegi(
    # CLI Parameters
    headless: bool = typer.Option(True, help="Run browser in headless mode (no window)"),
    output: Path = typer.Option(DEFAULT_DATA_DIR, help="Destination folder for the files")
):
    """
    Mass download of DENUE data using the InegiDownloader class.
    """
    console.print(f"[bold green]🚀 Starting INEGI Runner[/bold green]")
    console.print(f"📂 Destination: {output}")
    console.print(f"👻 Headless Mode: {headless}")

    # Initialize the downloader
    downloader = InegiDownloader(output_dir=output, headless=headless)
    
    # Execute the download process
    # Note: Ensure the method in 'inegi.py' is named 'download_all'
    downloader.download_all()

@app.command()
def retry_inegi(
    report: Path = typer.Argument(..., help="Path to the previous CSV report", exists=True),
    output: Path = typer.Option(DEFAULT_DATA_DIR, help="Destination folder")
):
    """
    Retries failed downloads based on a CSV report.
    """
    console.print(f"[bold yellow]🔄 Retrying from report: {report}[/bold yellow]")
    
    # Instantiate the class (usually headless for retries)
    downloader = InegiDownloader(output_dir=output, headless=True)
    
    # Execute the retry logic
    downloader.retry(report_csv=report)

if __name__ == "__main__":
    app()