"""
DENUE Downloader - Simplified working version.

Integrates Phase 2 components with existing architecture.
"""

import os
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn

from src_etl.core.download_config import DownloadConfig
from src_etl.core.logging import StructuredLogger
from src_etl.utils.browser import get_chrome_driver
from src_etl.utils.denue_scraping import scrape_denue_table
from src_etl.utils.downloader import Downloader
from src_metadata.models import TaskStatus
from src_metadata.tracker import MetadataTracker


class DenueDownloader:
    """
    Simplified DENUE downloader using Phase 2 components.

    Example:
        >>> downloader = DenueDownloader(
        ...     output_dir=Path("data/bronze/denue"),
        ...     headless=True,
        ... )
        >>> downloader.download_all()
    """

    def __init__(
        self,
        output_dir: Path,
        headless: bool = True,
        config: DownloadConfig | None = None,
        force: bool = False,
    ):
        """Initialize DENUE downloader."""
        self.output_dir = Path(output_dir)
        self.headless = headless
        self.force = force
        self.base_url = "https://www.inegi.org.mx/app/descarga/"
        self.base_url_inegi = "https://www.inegi.org.mx"

        # Configuration
        self.config = config or DownloadConfig(
            download_directory=self.output_dir,
            max_retries=3,
            max_parallel_downloads=3,
            timeout_seconds=300,
        )

        # Metadata tracker
        # Try to read from environment variable first, otherwise compute default path
        metadata_path_env = os.getenv("METADATA_DUCKDB_PATH")
        if metadata_path_env:
            metadata_path = Path(metadata_path_env)
        else:
            # Fallback to computed path (backward compatibility)
            metadata_path = self.output_dir.parent.parent / "metadata.duckdb"

        self.tracker = MetadataTracker(db_path=metadata_path)

        # Console
        self.console = Console()

        # Run context
        self.run_id: str | None = None
        self.logger: StructuredLogger | None = None

    def download_all(self) -> dict:
        """
        Download all available DENUE files from INEGI.

        Returns:
            Run summary dictionary
        """
        # Create job run
        self.run_id = self.tracker.create_run(
            job_name="denue_downloader",
            job_type="DOWNLOAD",
            trigger_type="MANUAL",
            triggered_by="user",
            config=self.config.to_dict(),
        )

        # Initialize logging
        self.logger = StructuredLogger.create_for_run(
            run_id=self.run_id,
            job_name="denue_downloader",
            log_dir=self.config.log_directory,
            log_level=self.config.log_level,
        )
        log = self.logger.get_logger()

        self.console.print("\n🚀 [bold cyan]DENUE Download System[/bold cyan]")
        self.console.print(f"📁 Output: {self.output_dir}")
        self.console.print(f"🆔 Run ID: {self.run_id}\n")

        log.info("download_job_started", output_dir=str(self.output_dir))

        try:
            # Step 1: Scrape INEGI website
            tasks = self._scrape_download_tasks(log)

            if not tasks:
                self.console.print("✨ [green]No new files to download[/green]")
                self.tracker.complete_run(self.run_id)
                summary = self.tracker.get_run_summary(self.run_id)
                return summary

            # Step 2: Download files
            self.console.print(f"📊 [bold]Files to download: {len(tasks)}[/bold]\n")
            self._download_tasks(tasks, log)

            # Step 3: Complete job run
            self.tracker.complete_run(self.run_id)
            summary = self.tracker.get_run_summary(self.run_id)

            # Display summary
            self._display_summary(summary)

            log.info("download_job_completed", summary=summary)

            return summary

        except Exception as e:
            log.error("download_job_failed", error=str(e))
            self.tracker.complete_run(self.run_id)
            raise

    def _scrape_download_tasks(self, log) -> list[tuple[str, str, str]]:
        """Scrape INEGI website for download tasks."""
        self.console.print("🔍 [yellow]Scraping INEGI website...[/yellow]")

        if self.force:
            self.console.print("⚠️  [yellow]Force mode: Will queue ALL files[/yellow]")

        log.info("scraping_started", url=self.base_url, force=self.force)

        driver = get_chrome_driver(headless=self.headless)
        if not driver:
            log.error("browser_init_failed")
            raise RuntimeError("Failed to initialize Chrome driver")

        try:
            driver.get(self.base_url)
            tasks = scrape_denue_table(
                driver=driver,
                wait_timeout=30,
                tracker=self.tracker,
                run_id=self.run_id,
                output_dir=self.output_dir,
                base_url_inegi=self.base_url_inegi,
                force=self.force,
            )

            log.info("scraping_completed", tasks_found=len(tasks), force=self.force)
            return tasks

        finally:
            if driver:
                driver.quit()

    def _download_tasks(self, tasks: list[tuple[str, str, str]], log):
        """Download tasks with progress tracking."""
        # Create download tasks in metadata
        task_mappings = []
        for period, activity, url in tasks:
            dest_path = self.output_dir / period / f"{activity}.zip"

            # Create task with task_name parameter
            task_id = self.tracker.create_task(
                run_id=self.run_id,
                task_name=f"{activity}_{period}",
                source_url=url,
                target_path=str(dest_path.relative_to(Path.cwd())),
                period=period,
                max_retries=self.config.max_retries,
            )
            task_mappings.append((task_id, period, activity, url, dest_path))

        # Download with progress bar
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=self.console,
        ) as progress:
            download_task = progress.add_task("[cyan]Downloading...", total=len(task_mappings))

            # Create downloader
            downloader = Downloader(self.config)

            # Sequential downloads (simpler for MVP)
            for task_id, period, activity, url, dest_path in task_mappings:
                success = self._download_single_task(
                    task_id, period, activity, url, dest_path, downloader, log
                )

                status_icon = "✅" if success else "❌"
                progress.console.print(f"  {status_icon} {activity}.zip")
                progress.update(download_task, advance=1)

            downloader.close()

    def _download_single_task(
        self,
        task_id: str,
        period: str,
        activity: str,
        url: str,
        dest_path: Path,
        downloader: Downloader,
        log,
    ) -> bool:
        """Download a single file and update metadata."""
        task_log = log.bind(task_id=task_id, activity=activity, period=period)

        # Start task
        self.tracker.start_task(task_id)
        task_log.info("task_started", url=url)

        # Ensure parent directory exists
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        # Download
        result = downloader.download(
            url=url,
            target_path=dest_path,
        )

        # Update metadata using individual parameters
        if result.success:
            self.tracker.complete_task(
                task_id=task_id,
                status=TaskStatus.SUCCESS,
                actual_size_bytes=result.size_bytes,
                actual_hash=(
                    result.validation_result.actual_hash if result.validation_result else None
                ),
                download_speed_mbps=result.download_speed_mbps,
                validation_passed=(
                    result.validation_result.is_valid if result.validation_result else None
                ),
                duration_seconds=result.duration_seconds,
            )

            # Record data lineage for audit trail and thesis documentation
            self.tracker.record_lineage(
                run_id=self.run_id,
                task_id=task_id,
                source_location=url,
                target_location=str(dest_path),
                source_type="WEB",
                target_type="FILE",
                bytes_processed=result.size_bytes,
            )

            task_log.info("task_completed", size_bytes=result.size_bytes)
            return True
        else:
            self.tracker.complete_task(
                task_id=task_id,
                status=TaskStatus.FAILED,
                error_type=result.error_type,
                error_message=result.error_message,
                duration_seconds=result.duration_seconds,
            )
            task_log.error(
                "task_failed",
                error_type=result.error_type.value if result.error_type else None,
                error_message=result.error_message,
            )
            return False

    def _display_summary(self, summary: dict):
        """Display run summary."""
        self.console.print("\n" + "=" * 60)
        self.console.print("[bold green]Download Summary[/bold green]")
        self.console.print("=" * 60)

        self.console.print(f"Total Tasks: {summary.get('total_tasks', 0)}")
        self.console.print(f"Successful: {summary.get('successful_tasks', 0)}")
        self.console.print(f"Failed: {summary.get('failed_tasks', 0)}")
        self.console.print(f"Duration: {summary.get('duration_seconds', 0):.2f}s")

        total_bytes = summary.get("total_bytes_downloaded", 0)
        if total_bytes:
            self.console.print(f"Downloaded: {total_bytes / 1_000_000:.2f} MB")

        avg_speed = summary.get("avg_download_speed_mbps", 0)
        if avg_speed:
            self.console.print(f"Avg Speed: {avg_speed:.2f} Mbps")
