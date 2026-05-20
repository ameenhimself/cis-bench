"""End-to-end workflow for creating Custom GPT CIS Benchmark bundles."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
from typing import Callable, Iterable

from rich.console import Console
from rich.progress import track

from cis_bench.catalog.database import CatalogDatabase
from cis_bench.config import Config
from cis_bench.gpt_knowledge import PackageStats, package_downloads, validate_benchmark
from cis_bench.models.benchmark import Benchmark

console = Console()

CatalogResolver = Callable[[], list[dict]]
Downloader = Callable[[Iterable[str], Path], None]
Packager = Callable[..., PackageStats]


@dataclass
class BundleOptions:
    """Options for the Custom GPT bundle workflow."""

    output_dir: Path = field(default_factory=lambda: default_deliverable_dir())
    staging_dir: Path | None = None
    raw_dir_name: str = "Raw Files"
    max_retries: int = 2
    workers: int = 4
    benchmarks: int = 2
    overwrite: bool = False
    keep_temp: bool = False
    max_chars: int = 1_200_000
    single_file: bool = True


@dataclass
class ValidationReport:
    """Completeness status for staged raw benchmark JSON files."""

    complete_files: dict[str, Path]
    missing_ids: list[str]
    incomplete_ids: list[str]
    issues: dict[str, str]

    @property
    def is_complete(self) -> bool:
        return not self.missing_ids and not self.incomplete_ids

    @property
    def retry_ids(self) -> list[str]:
        return sorted(set(self.missing_ids + self.incomplete_ids), key=_sort_id)


@dataclass
class BundleResult:
    """Summary of a completed GPT bundle run."""

    output_dir: Path
    raw_dir: Path
    completed_count: int
    retry_count: int
    bundle_files: int
    receipt_path: Path


def default_deliverable_dir(now: datetime | None = None) -> Path:
    """Return the default dated deliverable directory."""
    now = now or datetime.now()
    return Path(f"CIS Benchmarks {now.strftime('%d%m%y')}")


def _sort_id(value: str) -> tuple[int, str]:
    return (int(value), value) if str(value).isdigit() else (10**12, str(value))


def _safe_reset_dir(path: Path) -> None:
    """Remove an existing output directory after basic safety checks."""
    resolved = path.resolve()
    if resolved.parent == resolved or str(resolved) in {resolved.anchor, ""}:
        raise ValueError(f"Refusing to overwrite unsafe path: {path}")
    if path.exists():
        shutil.rmtree(path)


def resolve_latest_catalog_entries() -> list[dict]:
    """Load latest published benchmark entries from the local catalog database."""
    catalog_db_path = Config.get_catalog_db_path()
    if not catalog_db_path.exists():
        raise RuntimeError("Catalog database not found. Run 'cis-bench catalog refresh' first.")

    db = CatalogDatabase(catalog_db_path)
    rows = db.search("", status="Published", latest_only=True, limit=10000)
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("title") or "").lower(),
            str(row.get("version") or "").lower(),
            str(row.get("benchmark_id") or ""),
        ),
    )


def _raw_validation_error(data: dict) -> str | None:
    recommendations = data.get("recommendations")
    total = data.get("total_recommendations")
    expected = data.get("expected_recommendations")

    if not isinstance(recommendations, list):
        return "recommendations is not a list"
    if not isinstance(total, int):
        return "total_recommendations is not an integer"
    if len(recommendations) != total:
        return (
            "recommendation list length does not match total_recommendations "
            f"({len(recommendations)}/{total})"
        )
    if expected is not None and expected != total:
        return (
            "expected_recommendations does not match total_recommendations "
            f"({expected}/{total})"
        )
    return None


def validate_staged_downloads(input_dir: Path, expected_ids: set[str]) -> ValidationReport:
    """Validate raw downloaded benchmark files and identify retry candidates."""
    complete_files: dict[str, Path] = {}
    incomplete_ids: set[str] = set()
    issues: dict[str, str] = {}

    for path in sorted(input_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            issues[path.name] = "invalid benchmark JSON"
            continue

        benchmark_id = str(data.get("benchmark_id") or "")
        if not benchmark_id:
            issues[path.name] = "missing benchmark_id"
            continue
        if expected_ids and benchmark_id not in expected_ids:
            issues[path.name] = f"unexpected benchmark_id {benchmark_id}"
            continue

        raw_error = _raw_validation_error(data)
        if raw_error:
            issues[path.name] = raw_error
            incomplete_ids.add(benchmark_id)
            continue

        try:
            benchmark = Benchmark(**data)
        except Exception:
            issues[path.name] = "invalid benchmark JSON"
            incomplete_ids.add(benchmark_id)
            continue

        model_error = validate_benchmark(benchmark)
        if model_error:
            issues[path.name] = model_error
            incomplete_ids.add(benchmark_id)
            continue

        complete_files[benchmark_id] = path
        incomplete_ids.discard(benchmark_id)

    missing_ids = sorted(expected_ids - set(complete_files) - incomplete_ids, key=_sort_id)
    return ValidationReport(
        complete_files=complete_files,
        missing_ids=missing_ids,
        incomplete_ids=sorted(incomplete_ids - set(complete_files), key=_sort_id),
        issues=issues,
    )


def _download_one(
    session,
    benchmark_id: str,
    destination: Path,
    workers: int,
    shared_progress=None,
    progress_lock: threading.Lock | None = None,
):
    from cis_bench.cli.commands.download import _export_benchmark, _save_benchmark_to_catalog
    from cis_bench.cli.helpers.download_helper import (
        download_with_progress,
        download_with_shared_progress,
    )
    from cis_bench.fetcher.workbench import WorkbenchScraper

    url = f"https://workbench.cisecurity.org/benchmarks/{benchmark_id}"
    scraper = WorkbenchScraper(WorkbenchScraper._clone_session(session))
    started_at = time.perf_counter()
    if shared_progress is not None:
        benchmark = download_with_shared_progress(
            scraper,
            url,
            shared_progress,
            prefix=f"[{benchmark_id}]",
            workers=workers,
            progress_lock=progress_lock,
        )
    else:
        benchmark = download_with_progress(scraper, url, prefix=f"[{benchmark_id}]", workers=workers)

    _save_benchmark_to_catalog(benchmark_id, benchmark, Config.get_catalog_db_path())
    _export_benchmark(benchmark, str(destination), ["json"])
    return benchmark, time.perf_counter() - started_at


def download_benchmark_ids(
    ids: Iterable[str],
    destination: Path,
    *,
    workers: int,
    benchmarks: int,
    force: bool = True,
) -> None:
    """Download benchmark IDs to JSON files using existing scraper/export helpers."""
    from cis_bench.cli.helpers.download_helper import _create_progress
    from cis_bench.fetcher.auth import AuthManager

    ids = [str(value) for value in ids]
    if not ids:
        return

    destination.mkdir(parents=True, exist_ok=True)
    session = AuthManager.get_or_create_session()

    if benchmarks > 1 and len(ids) > 1:
        progress_lock = threading.Lock()
        shared_progress = _create_progress(console)
        with shared_progress:
            with ThreadPoolExecutor(max_workers=benchmarks) as executor:
                future_to_id = {
                    executor.submit(
                        _download_one,
                        session,
                        benchmark_id,
                        destination,
                        workers,
                        shared_progress,
                        progress_lock,
                    ): benchmark_id
                    for benchmark_id in ids
                }
                remaining = set(future_to_id)
                while remaining:
                    done, remaining = wait(remaining, timeout=15, return_when=FIRST_COMPLETED)
                    for future in done:
                        benchmark_id = future_to_id[future]
                        try:
                            benchmark, elapsed = future.result()
                            shared_progress.console.print(
                                f"[green]OK[/green] {benchmark_id} {benchmark.title} "
                                f"({elapsed:.1f}s)"
                            )
                        except Exception as exc:
                            shared_progress.console.print(
                                f"[red]Error[/red] {benchmark_id}: {exc}"
                            )
    else:
        for benchmark_id in ids:
            try:
                benchmark, elapsed = _download_one(session, benchmark_id, destination, workers)
                console.print(f"[green]OK[/green] {benchmark_id} {benchmark.title} ({elapsed:.1f}s)")
            except Exception as exc:
                console.print(f"[red]Error[/red] {benchmark_id}: {exc}")


def _publish_raw_files(report: ValidationReport, raw_dir: Path) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    for benchmark_id in sorted(report.complete_files, key=_sort_id):
        source = report.complete_files[benchmark_id]
        target = raw_dir / source.name
        if target.exists():
            target.unlink()
        shutil.move(str(source), target)


def _write_receipt(
    output_dir: Path,
    raw_dir: Path,
    latest_count: int,
    retry_count: int,
    package_stats: PackageStats,
    validation_report: ValidationReport,
) -> Path:
    receipt_path = output_dir / "RECEIPT.md"
    bundle_names = sorted(path.name for path in output_dir.glob("all-benchmarks-*.md"))
    upload_target = bundle_names[0] if bundle_names else "the generated Markdown knowledge file"
    lines = [
        "# CIS Benchmarks Custom GPT Receipt",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Latest benchmarks verified: {latest_count}",
        f"Raw files: {len(list(raw_dir.glob('*.json')))} in `{raw_dir.name}`",
        f"Knowledge bundles: {package_stats.bundle_files}",
        f"Download retry passes used: {retry_count}",
        f"Skipped or invalid files during packaging: {package_stats.skipped_files}",
        "",
        f"Next step: upload `{upload_target}` to the Custom GPT knowledge section.",
        "",
        "Credits: CIS Benchmarks and CIS WorkBench provide the benchmark source material; "
        "this fork builds on MITRE SAF Team's `cis-bench` project under Apache 2.0; "
        "implementation uses Python packages including Click, Rich, Requests, "
        "Beautiful Soup, Pydantic, and SQLModel.",
    ]
    if validation_report.issues:
        lines.extend(["", "Validation notes:"])
        for filename, reason in sorted(validation_report.issues.items()):
            lines.append(f"- {filename}: {reason}")
    receipt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return receipt_path


def run_gpt_bundle(
    options: BundleOptions,
    *,
    catalog_resolver: CatalogResolver = resolve_latest_catalog_entries,
    downloader=download_benchmark_ids,
    packager=package_downloads,
) -> BundleResult:
    """Run the latest-download, validate, publish, and GPT-package workflow."""
    output_dir = Path(options.output_dir)
    if output_dir.exists():
        if not options.overwrite:
            raise FileExistsError(
                f"Output directory already exists: {output_dir}. Use --overwrite to replace it."
            )

    rows = catalog_resolver()
    latest_ids = sorted({str(row["benchmark_id"]) for row in rows}, key=_sort_id)
    if not latest_ids:
        raise RuntimeError("No latest published benchmarks found in the local catalog.")

    if output_dir.exists() and options.overwrite:
        _safe_reset_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    def run_with_staging(staging_dir: Path) -> BundleResult:
        staging_dir.mkdir(parents=True, exist_ok=True)
        queue = latest_ids
        retry_count = 0
        validation_report = ValidationReport({}, latest_ids, [], {})

        for attempt in range(options.max_retries + 1):
            console.print(
                f"[cyan]Downloading pass {attempt + 1}:[/cyan] {len(queue)} benchmark(s)"
            )
            downloader(
                queue,
                staging_dir,
                workers=options.workers,
                benchmarks=options.benchmarks,
                force=True,
            )

            validation_report = validate_staged_downloads(staging_dir, set(latest_ids))
            if validation_report.is_complete:
                break

            queue = validation_report.retry_ids
            if attempt >= options.max_retries:
                raise RuntimeError(
                    "Incomplete downloads remain after retry limit: " + ", ".join(queue)
                )
            retry_count += 1
            console.print(f"[yellow]Retrying {len(queue)} incomplete benchmark(s).[/yellow]")

        raw_dir = output_dir / options.raw_dir_name
        for _ in track([1], description="Publishing raw files"):
            _publish_raw_files(validation_report, raw_dir)

        package_stats = packager(
            raw_dir,
            output_dir,
            max_chars=options.max_chars,
            single_file=options.single_file,
        )
        receipt_path = _write_receipt(
            output_dir,
            raw_dir,
            len(latest_ids),
            retry_count,
            package_stats,
            validation_report,
        )
        return BundleResult(
            output_dir=output_dir,
            raw_dir=raw_dir,
            completed_count=len(validation_report.complete_files),
            retry_count=retry_count,
            bundle_files=package_stats.bundle_files,
            receipt_path=receipt_path,
        )

    if options.staging_dir is not None:
        staging_path = Path(options.staging_dir)
        if staging_path.exists() and options.overwrite:
            _safe_reset_dir(staging_path)
        return run_with_staging(staging_path)

    with tempfile.TemporaryDirectory(prefix="cis-bench-gpt-") as temp_dir:
        result = run_with_staging(Path(temp_dir))
        if options.keep_temp:
            keep_path = output_dir / "_staging"
            if keep_path.exists():
                _safe_reset_dir(keep_path)
            shutil.copytree(temp_dir, keep_path)
        return result
