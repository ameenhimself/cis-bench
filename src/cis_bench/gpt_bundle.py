"""End-to-end workflow for creating Custom GPT CIS Benchmark bundles."""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

from rich.console import Console

from cis_bench.catalog.database import CatalogDatabase
from cis_bench.config import Config
from cis_bench.gpt_knowledge import (
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_TOKENS,
    PackageStats,
    package_downloads,
    validate_benchmark,
)
from cis_bench.models.benchmark import Benchmark

console = Console()

CatalogResolver = Callable[[], list[dict]]
Downloader = Callable[[Iterable[str], Path], None]
Packager = Callable[..., PackageStats]
CatalogRefresher = Callable[[str], dict]

BUNDLE_MARKER = ".cis-bench-bundle.json"
STAGING_MARKER = ".cis-bench-staging.json"
PARTIAL_SUFFIX = ".partial"


@dataclass
class BundleOptions:
    """Options for the Custom GPT bundle workflow."""

    output_dir: Path = field(default_factory=lambda: default_deliverable_dir())
    staging_dir: Path | None = None
    raw_dir_name: str = "Raw Files"
    max_retries: int = 5
    workers: int = 4
    benchmarks: int = 2
    overwrite: bool = False
    keep_temp: bool = False
    max_chars: int | None = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_files: int = DEFAULT_MAX_FILES
    single_file: bool | None = None
    catalog_refresh: str = "auto"


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


def _catalog_timestamp(db: CatalogDatabase) -> datetime | None:
    try:
        value = db.get_metadata("last_full_scrape")
    except Exception:
        return None
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def refresh_catalog_if_needed(mode: str, now: datetime | None = None) -> dict:
    """Refresh missing or stale catalog and return freshness metadata."""
    if mode not in {"auto", "always", "never"}:
        raise ValueError(f"Unsupported catalog refresh mode: {mode}")

    now = now or datetime.now(UTC)
    db_path = Config.get_catalog_db_path()
    db_exists = db_path.exists()
    if mode == "never" and not db_exists:
        raise RuntimeError("Catalog database not found and catalog refresh is disabled.")
    if not db_exists:
        Config.ensure_directories()
    db = CatalogDatabase(db_path)
    timestamp = _catalog_timestamp(db) if db_exists else None
    stale = timestamp is None or now - timestamp > timedelta(hours=24)
    refresh = mode == "always" or (mode == "auto" and stale)

    if not refresh:
        return {
            "catalog_refreshed": False,
            "catalog_timestamp": timestamp.isoformat() if timestamp else None,
        }

    from cis_bench.catalog.scraper import CatalogScraper
    from cis_bench.fetcher.auth import AuthManager

    Config.ensure_directories()
    db.initialize_schema()
    session = AuthManager.get_or_create_session(verify_ssl=Config.get_verify_ssl())
    if not AuthManager.validate_session(session, verify_ssl=Config.get_verify_ssl()):
        raise RuntimeError("Saved CIS WorkBench session is invalid. Run 'cis-bench auth login'.")

    stats = CatalogScraper(db, session).scrape_full_catalog(rate_limit_seconds=2.0)
    if stats.get("failed_pages"):
        raise RuntimeError(
            "Catalog refresh incomplete; failed pages: "
            + ", ".join(str(page) for page in stats["failed_pages"])
        )
    timestamp = _catalog_timestamp(db)
    return {
        "catalog_refreshed": True,
        "catalog_timestamp": timestamp.isoformat() if timestamp else None,
        "catalog_benchmarks": stats.get("total_benchmarks"),
    }


def _sort_id(value: str) -> tuple[int, str]:
    return (int(value), value) if str(value).isdigit() else (10**12, str(value))


def _assert_narrow_path(path: Path) -> Path:
    """Reject roots, home, current workspace, and application data root."""
    resolved = path.resolve()
    forbidden = {
        Path.cwd().resolve(),
        Path.home().resolve(),
        Config.get_data_dir().resolve(),
        Path(resolved.anchor).resolve(),
    }
    if resolved.parent == resolved or resolved in forbidden:
        raise ValueError(f"Refusing unsafe directory path: {path}")
    return resolved


def _is_legacy_bundle(path: Path) -> bool:
    return (
        (path / "manifest.json").is_file()
        and (path / "RECEIPT.md").is_file()
        and (path / "Raw Files").is_dir()
    )


def _reset_owned_dir(path: Path, marker: str, *, allow_legacy_bundle: bool = False) -> None:
    """Remove only a directory proven to belong to this workflow."""
    _assert_narrow_path(path)
    if not path.exists():
        return
    owned = (path / marker).is_file()
    if allow_legacy_bundle:
        owned = owned or _is_legacy_bundle(path)
    if not owned:
        raise ValueError(f"Refusing to remove unrecognized directory: {path}")
    shutil.rmtree(path)


def _write_marker(path: Path, marker: str, payload: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / marker).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _default_staging_dir(output_dir: Path) -> Path:
    digest = hashlib.sha256(str(output_dir.resolve()).encode()).hexdigest()[:12]
    return Config.get_data_dir() / "gpt-bundle-staging" / f"{output_dir.name}-{digest}"


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
    complete_downloaded_at: dict[str, str] = {}
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

        if benchmark_id in complete_files:
            previous = complete_files[benchmark_id]
            issues[path.name] = (
                f"duplicate benchmark_id {benchmark_id}; selected newest complete file"
            )
            previous_time = complete_downloaded_at[benchmark_id]
            current_time = benchmark.downloaded_at.isoformat()
            if current_time > previous_time:
                issues[previous.name] = (
                    f"duplicate benchmark_id {benchmark_id}; superseded by {path.name}"
                )
                complete_files[benchmark_id] = path
                complete_downloaded_at[benchmark_id] = current_time
            continue

        complete_files[benchmark_id] = path
        complete_downloaded_at[benchmark_id] = benchmark.downloaded_at.isoformat()
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
        shutil.copy2(source, target)


def _write_receipt(
    output_dir: Path,
    raw_dir: Path,
    latest_count: int,
    retry_count: int,
    package_stats: PackageStats,
    validation_report: ValidationReport,
    catalog_metadata: dict,
) -> Path:
    receipt_path = output_dir / "RECEIPT.md"
    bundle_names = [bundle["filename"] for bundle in package_stats.bundles]
    lines = [
        "# CIS Benchmarks Custom GPT Receipt",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Latest benchmarks verified: {latest_count}",
        f"Raw files: {len(list(raw_dir.glob('*.json')))} in `{raw_dir.name}`",
        f"Knowledge bundles: {package_stats.bundle_files}",
        f"Catalog timestamp: {catalog_metadata.get('catalog_timestamp') or 'unknown'}",
        f"Download retry passes used: {retry_count}",
        f"Skipped or invalid files during packaging: {package_stats.skipped_files}",
        "",
        "Next step: upload every `cis-benchmarks-knowledge-*.md` file to Custom GPT Knowledge,",
        "then paste `CUSTOM_GPT_INSTRUCTIONS.md` into Custom GPT Instructions.",
        "",
        "Knowledge files:",
        *[f"- `{name}`" for name in bundle_names],
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
    catalog_refresher: CatalogRefresher = refresh_catalog_if_needed,
    sleeper: Callable[[float], None] = time.sleep,
) -> BundleResult:
    """Run the latest-download, validate, publish, and GPT-package workflow."""
    output_dir = Path(options.output_dir)
    _assert_narrow_path(output_dir)
    if output_dir.exists():
        if not options.overwrite:
            raise FileExistsError(
                f"Output directory already exists: {output_dir}. Use --overwrite to replace it."
            )
        if not (output_dir / BUNDLE_MARKER).is_file() and not _is_legacy_bundle(output_dir):
            raise ValueError(f"Refusing to overwrite unrecognized directory: {output_dir}")

    console.print("[cyan]Resolving catalog freshness...[/cyan]")
    catalog_metadata = catalog_refresher(options.catalog_refresh)
    rows = catalog_resolver()
    latest_ids = sorted({str(row["benchmark_id"]) for row in rows}, key=_sort_id)
    if not latest_ids:
        raise RuntimeError("No latest published benchmarks found in the local catalog.")

    staging_dir = (
        Path(options.staging_dir)
        if options.staging_dir is not None
        else _default_staging_dir(output_dir)
    )
    _assert_narrow_path(staging_dir)
    if staging_dir.exists() and not (staging_dir / STAGING_MARKER).is_file():
        if any(staging_dir.iterdir()):
            raise ValueError(f"Refusing unrecognized staging directory: {staging_dir}")
    _write_marker(
        staging_dir,
        STAGING_MARKER,
        {
            "output_dir": str(output_dir.resolve()),
            "expected_ids": latest_ids,
            "catalog_timestamp": catalog_metadata.get("catalog_timestamp"),
            "state": "downloading",
        },
    )

    console.print("[cyan]Validating staged files...[/cyan]")
    validation_report = validate_staged_downloads(staging_dir, set(latest_ids))
    queue = validation_report.retry_ids
    retry_count = 0
    pass_number = 0
    retry_history: list[dict] = []
    while queue:
        if pass_number > options.max_retries:
            raise RuntimeError(
                "Incomplete downloads remain after retry limit: " + ", ".join(queue)
            )
        console.print(
            f"[cyan]Downloading pass {pass_number + 1}:[/cyan] {len(queue)} benchmark(s)"
        )
        queued_ids = list(queue)
        downloader(
            queue,
            staging_dir,
            workers=options.workers,
            benchmarks=options.benchmarks,
            force=True,
        )
        console.print("[cyan]Validating staged files...[/cyan]")
        validation_report = validate_staged_downloads(staging_dir, set(latest_ids))
        queue = validation_report.retry_ids
        retry_history.append(
            {
                "pass": pass_number + 1,
                "queued_ids": queued_ids,
                "remaining_ids": list(queue),
            }
        )
        if not queue:
            break
        if pass_number >= options.max_retries:
            raise RuntimeError(
                "Incomplete downloads remain after retry limit: " + ", ".join(queue)
            )
        retry_count += 1
        delay = min(30, 2 ** (retry_count + 1))
        console.print(
            f"[yellow]Retrying {len(queue)} incomplete benchmark(s) after {delay}s.[/yellow]"
        )
        sleeper(delay)
        pass_number += 1

    partial_dir = output_dir.with_name(output_dir.name + PARTIAL_SUFFIX)
    _assert_narrow_path(partial_dir)
    if partial_dir.exists():
        _reset_owned_dir(partial_dir, BUNDLE_MARKER)
    _write_marker(
        partial_dir,
        BUNDLE_MARKER,
        {"state": "building", "output_dir": str(output_dir.resolve())},
    )

    raw_dir = partial_dir / options.raw_dir_name
    console.print("[cyan]Publishing verified raw files...[/cyan]")
    _publish_raw_files(validation_report, raw_dir)
    manifest_context = {
        **catalog_metadata,
        "expected_benchmarks": len(latest_ids),
        "verified_benchmarks": len(validation_report.complete_files),
        "validation_complete": validation_report.is_complete,
        "download_retry_passes": retry_count,
        "download_history": retry_history,
    }
    console.print("[cyan]Rendering Custom GPT knowledge...[/cyan]")
    package_stats = packager(
        raw_dir,
        partial_dir,
        max_chars=options.max_chars,
        single_file=options.single_file,
        max_tokens=options.max_tokens,
        max_files=options.max_files,
        manifest_context=manifest_context,
    )
    receipt_path = _write_receipt(
        partial_dir,
        raw_dir,
        len(latest_ids),
        retry_count,
        package_stats,
        validation_report,
        catalog_metadata,
    )
    _write_marker(
        partial_dir,
        BUNDLE_MARKER,
        {
            "state": "complete",
            "generated_at": datetime.now(UTC).isoformat(),
            "verified_benchmarks": len(validation_report.complete_files),
        },
    )

    backup_dir = output_dir.with_name(output_dir.name + ".backup")
    if backup_dir.exists():
        _reset_owned_dir(backup_dir, BUNDLE_MARKER, allow_legacy_bundle=True)
    try:
        if output_dir.exists():
            output_dir.rename(backup_dir)
        partial_dir.rename(output_dir)
    except Exception:
        if backup_dir.exists() and not output_dir.exists():
            backup_dir.rename(output_dir)
        raise
    if backup_dir.exists():
        _reset_owned_dir(backup_dir, BUNDLE_MARKER, allow_legacy_bundle=True)

    if not options.keep_temp:
        _reset_owned_dir(staging_dir, STAGING_MARKER)

    return BundleResult(
        output_dir=output_dir,
        raw_dir=output_dir / options.raw_dir_name,
        completed_count=len(validation_report.complete_files),
        retry_count=retry_count,
        bundle_files=package_stats.bundle_files,
        receipt_path=output_dir / receipt_path.name,
    )
