import typer
from pathlib import Path
from rich.console import Console

# Import orchestrators
from scripts.sources.inegi import InegiDownloader
from scripts.sources.denue import DenueProcessor

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
    output_dir: Path = typer.Option(DEFAULT_PROCESSED_DIR, help="Destination for Parquet files")
):
    """
    Step 2: Process Raw ZIPs into Clean Parquet files (Processed Layer).
    Uses Polars + Multiprocessing for high performance.
    """
    console.print(f"[bold blue]⚙️ Starting DENUE Processor[/bold blue]")
    console.print(f"📂 Input: {input_dir}")
    console.print(f"📂 Output: {output_dir}")
    
    # Validation
    if not input_dir.exists():
        console.print(f"[bold red]❌ Error: Input directory does not exist: {input_dir}[/bold red]")
        raise typer.Exit(code=1)

    # Instantiate and run processor
    processor = DenueProcessor(input_dir=input_dir, output_dir=output_dir)
    processor.process()

if __name__ == "__main__":
    app()