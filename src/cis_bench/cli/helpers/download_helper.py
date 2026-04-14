"""Helper functions for download operations with progress bars."""

import logging
import re
import threading

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn

logger = logging.getLogger(__name__)


class _NoOpLock:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return False


def _create_progress(console: Console | None = None) -> Progress:
    """Create a progress display with the standard cis-bench layout."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("({task.completed}/{task.total})"),
        TimeRemainingColumn(),
        console=console,
    )


def download_with_progress(scraper, url, prefix="", workers=1):
    """Download benchmark with Rich progress bar."""
    console = Console()

    progress_bar = None
    progress_task = None

    def progress_callback(msg):
        nonlocal progress_bar, progress_task

        if "Benchmark title:" in msg:
            benchmark_title = msg.split("Benchmark title:", 1)[1].strip()
            console.print(f"{prefix}{benchmark_title}", highlight=False)
        elif "Found" in msg and "recommendations" in msg:
            match = re.search(r"Found (\d+) recommendations", msg)
            if match:
                total = int(match.group(1))
                progress_bar = _create_progress(console)
                progress_bar.start()
                progress_task = progress_bar.add_task(
                    f"{prefix} Downloading {total} recommendations", total=total
                )
        elif msg.startswith("["):
            match = re.search(r"\[(\d+)/(\d+)\]", msg)
            if match and progress_bar and progress_task is not None:
                current = int(match.group(1))
                progress_bar.update(progress_task, completed=current)

    logger.debug(f"Starting download: {url}")
    benchmark = scraper.download_benchmark(
        url, progress_callback=progress_callback, max_workers=workers
    )

    if progress_bar:
        progress_bar.stop()

    logger.debug(f"Download complete: {benchmark.title}")
    return benchmark


def download_with_shared_progress(
    scraper,
    url,
    progress_bar: Progress,
    prefix: str = "",
    workers: int = 1,
    progress_lock: threading.Lock | None = None,
):
    """Download a benchmark while updating a shared multi-task progress display."""
    active_lock = progress_lock or _NoOpLock()
    task_id = None
    benchmark_title = None

    def progress_callback(msg):
        nonlocal task_id, benchmark_title

        if "Benchmark title:" in msg:
            benchmark_title = msg.split("Benchmark title:", 1)[1].strip()
            if task_id is not None:
                with active_lock:
                    progress_bar.update(task_id, description=f"{prefix} {benchmark_title}".strip())
        elif "Found" in msg and "recommendations" in msg:
            match = re.search(r"Found (\d+) recommendations", msg)
            if match:
                total = int(match.group(1))
                description = f"{prefix} {benchmark_title or f'Downloading {total} recommendations'}".strip()
                with active_lock:
                    task_id = progress_bar.add_task(description, total=total)
        elif msg.startswith("["):
            match = re.search(r"\[(\d+)/(\d+)\]", msg)
            if match and task_id is not None:
                current = int(match.group(1))
                with active_lock:
                    progress_bar.update(task_id, completed=current)

    logger.debug(f"Starting shared-progress download: {url}")
    benchmark = scraper.download_benchmark(url, progress_callback=progress_callback, max_workers=workers)

    if task_id is not None:
        with active_lock:
            progress_bar.remove_task(task_id)

    logger.debug(f"Shared-progress download complete: {benchmark.title}")
    return benchmark
