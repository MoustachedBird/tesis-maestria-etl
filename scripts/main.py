import typer
from pathlib import Path
from rich.console import Console

# Import orchestrators
from scripts.src.downloader import InegiDownloader
from scripts.src.processor import DenueProcessor  # Updated to processors
from scripts.src.harmonizer import harmonize_timeline  # New import

# Help text explaining the CLI purpose
app = typer.Typer(help="ETL Scripts Runner for the Thesis (Extract, Transform, Load)")
console = Console()

# Define default paths (relative to project root)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RAW_DIR = PROJECT_ROOT / "data" / "raw" / "denue"
DEFAULT_PROCESSED_DIR = PROJECT_ROOT / "data" / "processed" / "denue"

@app.command()
def download_inegi(
    # CLI Parameters
    headless: bool = typer.Option(True, help="Run browser in headless mode (no window)"),
    output: Path = typer.Option(DEFAULT_RAW_DIR, help="Destination folder for raw zip files")
):
    """
    Step 1: Mass download of DENUE data (Raw Layer).
    """
    console.print(f"[bold green]🚀 Starting INEGI Downloader[/bold green]")
    console.print(f"📂 Destination: {output}")
    console.print(f"👻 Headless Mode: {headless}")

    downloader = InegiDownloader(output_dir=output, headless=headless)
    downloader.download_all()

@app.command()
def retry_inegi(
    output: Path = typer.Option(DEFAULT_RAW_DIR, help="Directory containing the report to retry")
):
    """
    Step 1.5: Retries failed downloads found in 'report_download.csv'.
    """
    console.print(f"[bold yellow]🔄 Starting Retry Process[/bold yellow]")
    console.print(f"📂 Target Directory: {output}")
    
    # Headless is usually preferred for retries
    downloader = InegiDownloader(output_dir=output, headless=True)
    downloader.retry()

@app.command()
def process_denue(
    input_dir: Path = typer.Option(DEFAULT_RAW_DIR, help="Folder containing downloaded ZIPs"),
    output_dir: Path = typer.Option(DEFAULT_PROCESSED_DIR, help="Destination for Parquet files"),
    force: bool = typer.Option(False, "--force", "-f", help="Retry failed files in Manifest")
):
    """
    Step 2: Process Raw ZIPs -> Parquet.
    Controlled by 'data/manifest_download.csv'.
    """
    console.print(f"[bold blue]⚙️ Starting DENUE Processor[/bold blue]")
    
    # Pasamos PROJECT_ROOT
    processor = DenueProcessor(
        input_dir=input_dir, 
        output_dir=output_dir, 
        project_root=PROJECT_ROOT, 
        force_mode=force
    )
    processor.process()

@app.command()
def harmonize():
    """
    Step 3: Identity Resolution (Gold Layer).
    Generates the 'master_catalog.parquet' linking stores across time (2015-2024).
    """
    console.print(f"[bold magenta]🔗 Starting Identity Resolution[/bold magenta]")
    harmonize_timeline()

if __name__ == "__main__":
    app()