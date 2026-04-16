"""Download command for CIS Benchmark CLI."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import logging
import threading
import os
import re
import sys
import time

import click
from rich.console import Console

from cis_bench.config import Config
from cis_bench.exporters import ExporterFactory
from cis_bench.fetcher.auth import AuthManager
from cis_bench.fetcher.workbench import WorkbenchScraper

console = Console()
logger = logging.getLogger(__name__)


def _collect_urls(benchmark_ids, urls_file):
    """Build benchmark URLs from CLI input."""
    urls = []

    if urls_file:
        with open(urls_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    if line.startswith("http"):
                        urls.append(line)
                    else:
                        urls.append(f"https://workbench.cisecurity.org/benchmarks/{line}")
    elif benchmark_ids:
        for item in benchmark_ids:
            if item.startswith("http"):
                urls.append(item)
            else:
                urls.append(f"https://workbench.cisecurity.org/benchmarks/{item}")

    return urls


def _filter_latest_urls(urls):
    """Filter benchmark URLs to latest catalog versions only."""
    catalog_db_path = Config.get_catalog_db_path()
    if not catalog_db_path.exists():
        console.print("[red]Error: --latest requires a catalog database.[/red]")
        console.print("[cyan]Run 'cis-bench catalog refresh' first.[/cyan]")
        sys.exit(1)

    try:
        from cis_bench.catalog.database import CatalogDatabase

        db = CatalogDatabase(catalog_db_path)
        filtered_urls = []
        skipped_count = 0
        missing_ids = []
        seen_urls = set()

        for url in urls:
            benchmark_id = url.split("/")[-1]
            benchmark = db.get_benchmark(benchmark_id)

            if not benchmark:
                missing_ids.append(benchmark_id)
                continue

            if not benchmark.get("is_latest"):
                skipped_count += 1
                continue

            if url not in seen_urls:
                filtered_urls.append(url)
                seen_urls.add(url)

        if missing_ids:
            console.print(
                f"[yellow]Warning:[/yellow] Skipped {len(missing_ids)} benchmark(s) not found in catalog metadata"
            )

        if skipped_count:
            console.print(
                f"[cyan]Filtered out {skipped_count} non-latest benchmark version(s).[/cyan]"
            )

        return filtered_urls

    except Exception as e:
        logger.error(f"Latest-version filtering failed: {e}", exc_info=True)
        console.print(f"[red]Error applying --latest filter:[/red] {e}")
        sys.exit(1)


def _get_cached_download(benchmark_id, catalog_db_path):
    """Return cached download metadata when available."""
    if not catalog_db_path.exists():
        return None

    try:
        from cis_bench.catalog.database import CatalogDatabase

        db = CatalogDatabase(catalog_db_path)
        return db.get_downloaded(benchmark_id)
    except Exception as e:
        logger.debug(f"Cache check failed for {benchmark_id}: {e}, will download anyway")
        return None


def _is_cached_download_complete(existing):
    """Return True when a cached download is known to be complete."""
    if not existing:
        return False

    expected = existing.get("expected_recommendation_count")
    actual = existing.get("recommendation_count")
    is_complete = existing.get("is_complete")

    # Legacy cache rows without completeness metadata are treated as incomplete
    # so they are refreshed once and then become restart-safe.
    if expected is None or is_complete is None:
        return False

    return bool(is_complete) and actual == expected


def _print_cached_download(prefix, benchmark_id, existing):
    """Print a cached-download skip message."""
    console.print(f"{prefix} [yellow]Benchmark {benchmark_id} already cached[/yellow]")
    console.print(f"      Downloaded: {existing['downloaded_at']}")
    console.print(
        "      Recommendations: "
        f"{existing['recommendation_count']}/{existing['expected_recommendation_count']}"
    )
    console.print("\n[dim]Use --force to re-download[/dim]\n")


def _print_incomplete_cached_download(prefix, benchmark_id, existing):
    """Explain why an existing cache row will be re-downloaded."""
    actual = existing.get("recommendation_count")
    expected = existing.get("expected_recommendation_count")
    if expected is None:
        console.print(
            f"{prefix} [yellow]Benchmark {benchmark_id} cache is missing completeness metadata; re-downloading to verify it.[/yellow]"
        )
    else:
        console.print(
            f"{prefix} [yellow]Benchmark {benchmark_id} cache is incomplete ({actual}/{expected}); re-downloading missing content.[/yellow]"
        )


def _save_benchmark_to_catalog(benchmark_id, benchmark, catalog_db_path):
    """Persist a downloaded benchmark in the local catalog cache."""
    if not catalog_db_path.exists():
        return None

    try:
        import hashlib

        from cis_bench.catalog.database import CatalogDatabase

        content_json = benchmark.model_dump_json()
        content_hash = hashlib.sha256(content_json.encode()).hexdigest()
        recommendation_count = len(benchmark.recommendations)

        db = CatalogDatabase(catalog_db_path)
        expected_recommendation_count = benchmark.expected_recommendations or recommendation_count
        db.save_downloaded(
            benchmark_id=benchmark_id,
            content_json=content_json,
            content_hash=content_hash,
            recommendation_count=recommendation_count,
            expected_recommendation_count=expected_recommendation_count,
            is_complete=recommendation_count == expected_recommendation_count,
        )
        logger.debug(f"Saved benchmark {benchmark_id} to catalog database")
        return recommendation_count
    except Exception as e:
        logger.warning(f"Failed to save to catalog database: {e}", exc_info=True)
        return e


def _export_benchmark(benchmark, output_dir, export_formats):
    """Export benchmark to the requested output formats."""
    exported = []

    for fmt in export_formats:
        try:
            logger.debug(f"Exporting to format: {fmt}")
            exporter = ExporterFactory.create(fmt)
            ext = exporter.get_file_extension()

            safe_title = re.sub(r"[^\w\s-]", "", benchmark.title).strip()
            safe_title = re.sub(r"[-\s]+", "_", safe_title).lower()
            output_file = os.path.join(output_dir, f"{safe_title}.{ext}")

            os.makedirs(output_dir, exist_ok=True)
            exporter.export(benchmark, output_file)

            file_size = os.path.getsize(output_file) / 1024
            logger.debug(f"Exported {fmt} format: {output_file} ({file_size:.1f} KB)")
            exported.append((exporter.format_name(), output_file, file_size, None))
        except Exception as e:
            logger.error(f"Export failed for format {fmt}: {e}", exc_info=True)
            exported.append((fmt, None, None, e))

    return exported


def _format_elapsed(elapsed_seconds):
    """Format elapsed seconds as MM:SS or HH:MM:SS."""
    total_seconds = max(0, int(round(elapsed_seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _download_benchmark_with_shared_progress(
    scraper,
    url,
    shared_progress,
    prefix,
    workers,
    progress_lock,
):
    """Run a shared-progress download and return benchmark + elapsed seconds."""
    started_at = time.perf_counter()
    from cis_bench.cli.helpers.download_helper import download_with_shared_progress

    benchmark = download_with_shared_progress(
        scraper,
        url,
        shared_progress,
        prefix,
        workers,
        progress_lock,
    )
    return benchmark, time.perf_counter() - started_at


def _print_download_success(
    prefix,
    benchmark_id,
    benchmark,
    cache_result,
    exported_files,
    elapsed_seconds,
    console_obj=console,
):
    """Print the standard post-download success summary."""
    console_obj.print(f"{prefix} [green]OK[/green] Downloaded: [bold]{benchmark.title}[/bold]")
    console_obj.print(f"      Time Elapsed: {_format_elapsed(elapsed_seconds)}")
    console_obj.print(f"      Recommendations: {benchmark.total_recommendations}")
    console_obj.print(
        f"      CIS Controls: {sum(len(r.cis_controls) for r in benchmark.recommendations)}"
    )
    console_obj.print(
        f"      MITRE Mappings: {sum(1 for r in benchmark.recommendations if r.mitre_mapping)}"
    )
    console_obj.print(
        f"      NIST Controls: {sum(len(r.nist_controls) for r in benchmark.recommendations)}"
    )

    if cache_result is not None:
        if isinstance(cache_result, Exception):
            console_obj.print(f"      [yellow]?[/yellow] Could not cache in database: {cache_result}")
        else:
            console_obj.print(f"      [green]OK[/green] Cached in database (ID: {benchmark_id})")

    for format_name, output_file, file_size, error in exported_files:
        if error is not None:
            console_obj.print(f"      [red]?[/red] {format_name} export failed: {error}")
        else:
            console_obj.print(
                f"      [green]OK[/green] Exported {format_name}: {output_file} ({file_size:.1f} KB)"
            )

    console_obj.print()


@click.command(name="download")
@click.argument("benchmark_ids", nargs=-1, required=False)
@click.option(
    "--file",
    "-f",
    "urls_file",
    type=click.Path(exists=True),
    help="File containing benchmark URLs or IDs (one per line)",
)
@click.option(
    "--output-dir", "-o", default="./benchmarks", help="Output directory for downloaded benchmarks"
)
@click.option(
    "--format",
    "-fmt",
    "export_formats",
    multiple=True,
    type=click.Choice(["json", "yaml", "csv", "markdown", "xccdf"]),
    default=["json"],
    help="Export formats (can specify multiple)",
)
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose (DEBUG level) logging")
@click.option("--debug", "-d", is_flag=True, help="Enable debug logging (same as --verbose)")
@click.option("--quiet", "-q", is_flag=True, help="Quiet mode (warnings and errors only)")
@click.option("--force", is_flag=True, help="Force re-download even if already cached in database")
@click.option("--latest", is_flag=True, help="Only download latest benchmark versions from catalog")
@click.option(
    "--workers",
    type=click.IntRange(1, 32),
    default=1,
    show_default=True,
    help="Number of recommendation fetch workers to use per benchmark",
)
@click.option(
    "--benchmarks",
    type=click.IntRange(1, 16),
    default=1,
    show_default=True,
    help="Number of benchmarks to download concurrently",
)
def download(
    benchmark_ids,
    urls_file,
    output_dir,
    export_formats,
    verbose,
    debug,
    quiet,
    force,
    latest,
    workers,
    benchmarks,
):
    """Download CIS benchmarks by ID or URL.

    Uses saved session from 'cis-bench auth login'.

    \b
    Examples:
        # First, authenticate (one time)
        cis-bench auth login --browser chrome

        # Then download
        cis-bench download 23598
        cis-bench download 23598 22605 --format json --format xccdf
        cis-bench download --file urls.txt
    """
    if verbose or debug or quiet:
        from cis_bench.utils.logging_config import LoggingConfig

        LoggingConfig.setup_from_flags(quiet=quiet, verbose=(verbose or debug))

    logger.debug(
        "Starting download command: output_dir=%s, formats=%s, latest=%s, workers=%s, benchmarks=%s",
        output_dir,
        export_formats,
        latest,
        workers,
        benchmarks,
    )

    try:
        logger.debug("Getting authenticated session")
        with console.status("[bold green]Authenticating..."):
            session = AuthManager.get_or_create_session()

        console.print("[green]OK[/green] Authenticated successfully\n")
        logger.debug("Authentication successful")

    except ValueError:
        console.print("\n[bold red]Authentication Required[/bold red]\n")
        console.print("[bold cyan]Please log in first:[/bold cyan]")
        console.print("  cis-bench auth login --browser chrome")
        console.print("\n[dim]This saves your session for future commands.[/dim]")
        console.print(
            "\n[dim]Windows users: If Chrome fails, try Firefox or --cookies option.[/dim]"
        )
        sys.exit(1)
    except Exception as e:
        logger.error(f"Authentication failed: {e}", exc_info=True)
        console.print("\n[bold red]Authentication Failed[/bold red]\n")
        console.print(f"[yellow]Error: {e}[/yellow]\n")
        console.print("[bold cyan]Your session may have expired. To refresh:[/bold cyan]")
        console.print("  cis-bench auth login --browser chrome")
        sys.exit(1)

    urls = _collect_urls(benchmark_ids, urls_file)

    if not urls:
        if benchmark_ids or urls_file:
            console.print("[red]Error: No benchmarks to download[/red]")
        else:
            console.print("[red]Error: Must specify benchmark IDs or --file[/red]")
        sys.exit(1)

    if latest:
        urls = _filter_latest_urls(urls)
        if not urls:
            console.print("[red]Error: No latest-version benchmarks matched the input.[/red]")
            sys.exit(1)

    total_urls = len(urls)
    catalog_db_path = Config.get_catalog_db_path()

    logger.debug(f"Starting download of {total_urls} benchmark(s)")
    console.print(f"[bold]Downloading {total_urls} benchmark(s)...[/bold]\n")

    pending_downloads = []
    for idx, url in enumerate(urls, 1):
        prefix = f"[{idx}/{total_urls}]"
        benchmark_id = url.split("/")[-1]

        if not force:
            existing = _get_cached_download(benchmark_id, catalog_db_path)
            if existing and _is_cached_download_complete(existing):
                _print_cached_download(prefix, benchmark_id, existing)
                logger.debug(f"Skipping {benchmark_id} - already cached")
                continue
            if existing:
                _print_incomplete_cached_download(prefix, benchmark_id, existing)

        pending_downloads.append((idx, url, benchmark_id, prefix))

    if benchmarks > 1 and len(pending_downloads) > 1:
        from cis_bench.cli.helpers.download_helper import _create_progress

        console.print(
            f"[cyan]Using up to {benchmarks} parallel benchmark download(s) with {workers} recommendation worker(s) each.[/cyan]"
        )
        console.print(
            f"[cyan]Started {min(benchmarks, len(pending_downloads))} active download(s) for {len(pending_downloads)} benchmark(s).[/cyan]\n"
        )

        progress_lock = threading.Lock()
        shared_progress = _create_progress(console)

        with shared_progress:
            with ThreadPoolExecutor(max_workers=benchmarks) as executor:
                future_to_item = {}
                for idx, url, benchmark_id, prefix in pending_downloads:
                    logger.debug(f"Queueing benchmark {idx}/{total_urls}: {url}")
                    scraper = WorkbenchScraper(WorkbenchScraper._clone_session(session))
                    started_at = time.perf_counter()
                    future = executor.submit(
                        _download_benchmark_with_shared_progress,
                        scraper,
                        url,
                        shared_progress,
                        prefix,
                        workers,
                        progress_lock,
                    )
                    future_to_item[future] = (idx, url, benchmark_id, prefix, started_at)

                remaining_futures = set(future_to_item)
                completed_downloads = 0

                while remaining_futures:
                    done, remaining_futures = wait(
                        remaining_futures, timeout=15, return_when=FIRST_COMPLETED
                    )

                    if not done:
                        shared_progress.console.print(
                            f"[cyan]Progress:[/cyan] Completed {completed_downloads}/{len(pending_downloads)} benchmark(s); {len(remaining_futures)} still active..."
                        )
                        continue

                    for future in done:
                        idx, url, benchmark_id, prefix, started_at = future_to_item[future]
                        completed_downloads += 1
                        try:
                            benchmark, elapsed_seconds = future.result()
                            logger.debug(f"Successfully downloaded benchmark: {benchmark.title}")
                            cache_result = _save_benchmark_to_catalog(
                                benchmark_id, benchmark, catalog_db_path
                            )
                            exported_files = _export_benchmark(benchmark, output_dir, export_formats)
                            shared_progress.console.print(
                                f"[cyan]Progress:[/cyan] Completed {completed_downloads}/{len(pending_downloads)} benchmark(s)."
                            )
                            _print_download_success(
                                prefix,
                                benchmark_id,
                                benchmark,
                                cache_result,
                                exported_files,
                                elapsed_seconds,
                                console_obj=shared_progress.console,
                            )
                        except Exception as e:
                            logger.error(f"Benchmark download failed: {e}", exc_info=True)
                            shared_progress.console.print(
                                f"[cyan]Progress:[/cyan] Completed {completed_downloads}/{len(pending_downloads)} benchmark(s)."
                            )
                            shared_progress.console.print(f"{prefix} [red]? Error:[/red] {e}\n")
    else:
        scraper = WorkbenchScraper(session)
        from cis_bench.cli.helpers.download_helper import download_with_progress

        for idx, url, benchmark_id, prefix in pending_downloads:
            logger.debug(f"Processing benchmark {idx}/{total_urls}: {url}")
            try:
                console.print(f"{prefix} [cyan]Starting download...[/cyan]")
                started_at = time.perf_counter()
                benchmark = download_with_progress(scraper, url, prefix=prefix, workers=workers)
                elapsed_seconds = time.perf_counter() - started_at
                logger.debug(f"Successfully downloaded benchmark: {benchmark.title}")
                cache_result = _save_benchmark_to_catalog(benchmark_id, benchmark, catalog_db_path)
                exported_files = _export_benchmark(benchmark, output_dir, export_formats)
                _print_download_success(
                    prefix,
                    benchmark_id,
                    benchmark,
                    cache_result,
                    exported_files,
                    elapsed_seconds,
                )
            except Exception as e:
                logger.error(f"Benchmark download failed: {e}", exc_info=True)
                console.print(f"{prefix} [red]? Error:[/red] {e}\n")
                import traceback

                traceback.print_exc()
                continue

    logger.debug("Download command completed")
    console.print("[bold green]Download complete![/bold green]")
