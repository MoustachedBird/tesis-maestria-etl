import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))

import typer
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
    # We only need the directory now, because the filename is fixed (report_download.csv)
    output: Path = typer.Option(DEFAULT_DATA_DIR, help="Directory containing the report to retry")
):
    """
    Retries failed downloads found in 'report_download.csv' inside the output directory.
    """
    console.print(f"[bold yellow]🔄 Starting Retry Process[/bold yellow]")
    console.print(f"📂 Target Directory: {output}")
    
    # Instantiate the class (Headless is typically fine for retries)
    downloader = InegiDownloader(output_dir=output, headless=True)
    
    # Execute the retry logic (No arguments needed now)
    downloader.retry()

if __name__ == "__main__":
    app()