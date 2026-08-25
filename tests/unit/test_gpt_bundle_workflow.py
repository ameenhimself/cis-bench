"""Unit tests for the Custom GPT bundle workflow."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from cis_bench.gpt_bundle import (
    BUNDLE_MARKER,
    BundleOptions,
    default_deliverable_dir,
    refresh_catalog_if_needed,
    run_gpt_bundle,
    validate_staged_downloads,
)
from cis_bench.catalog.database import CatalogDatabase
from cis_bench.config import Config
from cis_bench.models.benchmark import Benchmark, Recommendation


def make_benchmark(
    benchmark_id: str,
    title: str = "CIS Ubuntu Linux 22.04 Benchmark",
    total_recommendations: int = 1,
    expected_recommendations: int | None = None,
) -> Benchmark:
    recommendations = [
        Recommendation(
            ref="1.1",
            title="Test recommendation",
            url=f"https://workbench.cisecurity.org/sections/{benchmark_id}/recommendations/1",
            assessment_status="Manual",
            profiles=["Level 1"],
            description="Description sentence.",
            rationale="Rationale sentence.",
            audit="Audit sentence.",
            remediation="Remediation sentence.",
        )
    ]
    return Benchmark(
        title=title,
        benchmark_id=benchmark_id,
        url=f"https://workbench.cisecurity.org/benchmarks/{benchmark_id}",
        version="v1.0.0",
        downloaded_at=datetime(2026, 5, 20, 12, 0, 0),
        scraper_version="test",
        total_recommendations=total_recommendations,
        expected_recommendations=expected_recommendations,
        recommendations=recommendations,
    )


def catalog_fresh(_mode):
    return {"catalog_refreshed": False, "catalog_timestamp": "2026-05-20T00:00:00+00:00"}


def package_stats(count: int):
    return SimpleNamespace(
        processed_files=count,
        skipped_files=0,
        bundle_files=1,
        bundles=[{"filename": "cis-benchmarks-knowledge-01-of-01.md"}],
    )


def test_default_deliverable_dir_uses_day_month_year():
    assert default_deliverable_dir(datetime(2026, 5, 20, 9, 0, 0)) == Path(
        "CIS Benchmarks 200526"
    )


def test_validate_staged_downloads_detects_missing_invalid_and_incomplete(tmp_path):
    make_benchmark("1", expected_recommendations=1).to_json_file(tmp_path / "complete.json")
    (tmp_path / "invalid.json").write_text("{invalid", encoding="utf-8")
    (tmp_path / "mismatch.json").write_text(
        make_benchmark("2", expected_recommendations=2).model_copy(
            update={"expected_recommendations": 2, "total_recommendations": 2}
        ).model_dump_json(),
        encoding="utf-8",
    )

    report = validate_staged_downloads(tmp_path, expected_ids={"1", "2", "3"})

    assert set(report.complete_files) == {"1"}
    assert report.missing_ids == ["3"]
    assert report.incomplete_ids == ["2"]
    assert report.is_complete is False
    assert report.issues["invalid.json"] == "invalid benchmark JSON"
    assert "recommendation list length does not match total_recommendations" in report.issues[
        "mismatch.json"
    ]


def test_validate_staged_downloads_reports_duplicate_benchmark_ids(tmp_path):
    make_benchmark("1", expected_recommendations=1).to_json_file(tmp_path / "one.json")
    newer = make_benchmark("1", expected_recommendations=1).model_copy(
        update={"downloaded_at": datetime(2026, 5, 21, 12, 0, 0)}
    )
    newer.to_json_file(tmp_path / "two.json")

    report = validate_staged_downloads(tmp_path, expected_ids={"1"})

    assert report.is_complete is True
    assert report.complete_files["1"].name == "two.json"
    assert any("duplicate benchmark_id 1" in reason for reason in report.issues.values())


def test_run_gpt_bundle_retries_until_staged_files_are_complete(tmp_path):
    output_dir = tmp_path / "CIS Benchmarks 200526"
    staging_dir = tmp_path / "staging"
    calls: list[list[str]] = []

    catalog_rows = [
        {
            "benchmark_id": "1",
            "title": "CIS Ubuntu Linux 22.04 Benchmark",
            "version": "v1.0.0",
            "url": "https://workbench.cisecurity.org/benchmarks/1",
        },
        {
            "benchmark_id": "2",
            "title": "CIS AWS Foundations Benchmark",
            "version": "v1.0.0",
            "url": "https://workbench.cisecurity.org/benchmarks/2",
        },
    ]

    def downloader(ids, destination, *, workers, benchmarks, force):
        calls.append(list(ids))
        destination.mkdir(parents=True, exist_ok=True)
        make_benchmark("1").to_json_file(destination / "one.json")
        if len(calls) > 1:
            make_benchmark("2", title="CIS AWS Foundations Benchmark").to_json_file(
                destination / "two.json"
            )

    packaged = {}

    def packager(input_dir, knowledge_dir, **kwargs):
        packaged["input_dir"] = input_dir
        packaged["knowledge_dir"] = knowledge_dir
        packaged["manifest_context"] = kwargs["manifest_context"]
        (knowledge_dir / "cis-benchmarks-knowledge-01-of-01.md").write_text(
            "bundle", encoding="utf-8"
        )
        return package_stats(2)

    result = run_gpt_bundle(
        BundleOptions(
            output_dir=output_dir,
            staging_dir=staging_dir,
            max_retries=2,
            workers=4,
            benchmarks=2,
        ),
        catalog_resolver=lambda: catalog_rows,
        downloader=downloader,
        packager=packager,
        catalog_refresher=catalog_fresh,
        sleeper=lambda _delay: None,
    )

    assert calls == [["1", "2"], ["2"]]
    assert result.completed_count == 2
    assert sorted(path.name for path in (output_dir / "Raw Files").glob("*.json")) == [
        "one.json",
        "two.json",
    ]
    assert packaged["input_dir"].name == "Raw Files"
    assert packaged["manifest_context"]["download_history"] == [
        {"pass": 1, "queued_ids": ["1", "2"], "remaining_ids": ["2"]},
        {"pass": 2, "queued_ids": ["2"], "remaining_ids": []},
    ]
    assert (output_dir / "RECEIPT.md").exists()
    assert "upload every `cis-benchmarks-knowledge-*.md`" in (
        output_dir / "RECEIPT.md"
    ).read_text(encoding="utf-8")


def test_run_gpt_bundle_fails_safely_when_output_exists_without_overwrite(tmp_path):
    output_dir = tmp_path / "CIS Benchmarks 200526"
    output_dir.mkdir()

    with pytest.raises(FileExistsError):
        run_gpt_bundle(
            BundleOptions(output_dir=output_dir, staging_dir=tmp_path / "staging"),
            catalog_resolver=lambda: [],
            downloader=lambda *args, **kwargs: None,
            packager=lambda *args, **kwargs: None,
            catalog_refresher=catalog_fresh,
        )


def test_run_gpt_bundle_overwrites_existing_output_folder(tmp_path):
    output_dir = tmp_path / "CIS Benchmarks 200526"
    staging_dir = tmp_path / "staging"
    output_dir.mkdir()
    (output_dir / "stale.md").write_text("old", encoding="utf-8")
    (output_dir / BUNDLE_MARKER).write_text("{}", encoding="utf-8")

    def downloader(ids, destination, **kwargs):
        destination.mkdir(parents=True, exist_ok=True)
        make_benchmark("1").to_json_file(destination / "one.json")

    def packager(input_dir, knowledge_dir, **kwargs):
        (knowledge_dir / "cis-benchmarks-knowledge-01-of-01.md").write_text(
            "bundle", encoding="utf-8"
        )
        return package_stats(1)

    run_gpt_bundle(
        BundleOptions(
            output_dir=output_dir,
            staging_dir=staging_dir,
            overwrite=True,
        ),
        catalog_resolver=lambda: [{"benchmark_id": "1", "title": "CIS Test", "version": "v1"}],
        downloader=downloader,
        packager=packager,
        catalog_refresher=catalog_fresh,
    )

    assert not (output_dir / "stale.md").exists()
    assert (output_dir / "Raw Files" / "one.json").exists()


def test_run_gpt_bundle_raises_after_max_retries(tmp_path):
    output_dir = tmp_path / "CIS Benchmarks 200526"
    staging_dir = tmp_path / "staging"

    with pytest.raises(RuntimeError, match="Incomplete downloads remain"):
        run_gpt_bundle(
            BundleOptions(output_dir=output_dir, staging_dir=staging_dir, max_retries=1),
            catalog_resolver=lambda: [{"benchmark_id": "1", "title": "CIS Test", "version": "v1"}],
            downloader=lambda ids, destination, **kwargs: destination.mkdir(
                parents=True, exist_ok=True
            ),
            packager=lambda *args, **kwargs: None,
            catalog_refresher=catalog_fresh,
            sleeper=lambda _delay: None,
        )

    assert (staging_dir / ".cis-bench-staging.json").exists()


def test_run_gpt_bundle_resumes_verified_staging_without_download(tmp_path):
    output_dir = tmp_path / "CIS Benchmarks 200526"
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    (staging_dir / ".cis-bench-staging.json").write_text("{}", encoding="utf-8")
    make_benchmark("1", expected_recommendations=1).to_json_file(staging_dir / "one.json")

    def unexpected_download(*args, **kwargs):
        raise AssertionError("complete staged benchmark must not be downloaded again")

    def packager(input_dir, knowledge_dir, **kwargs):
        (knowledge_dir / "cis-benchmarks-knowledge-01-of-01.md").write_text(
            "bundle", encoding="utf-8"
        )
        return package_stats(1)

    result = run_gpt_bundle(
        BundleOptions(output_dir=output_dir, staging_dir=staging_dir),
        catalog_resolver=lambda: [{"benchmark_id": "1", "title": "CIS Test", "version": "v1"}],
        downloader=unexpected_download,
        packager=packager,
        catalog_refresher=catalog_fresh,
    )

    assert result.completed_count == 1
    assert not staging_dir.exists()
    assert (output_dir / "Raw Files" / "one.json").exists()


def test_run_gpt_bundle_refuses_unsafe_unrecognized_overwrite(tmp_path):
    output_dir = tmp_path / "important-files"
    output_dir.mkdir()
    (output_dir / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="unrecognized directory"):
        run_gpt_bundle(
            BundleOptions(output_dir=output_dir, overwrite=True),
            catalog_resolver=lambda: [],
            catalog_refresher=catalog_fresh,
        )

    assert (output_dir / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_packaging_failure_preserves_existing_deliverable_and_staging(tmp_path):
    output_dir = tmp_path / "CIS Benchmarks 200526"
    staging_dir = tmp_path / "staging"
    output_dir.mkdir()
    (output_dir / BUNDLE_MARKER).write_text("{}", encoding="utf-8")
    (output_dir / "old.md").write_text("old", encoding="utf-8")

    def downloader(ids, destination, **kwargs):
        make_benchmark("1", expected_recommendations=1).to_json_file(destination / "one.json")

    with pytest.raises(RuntimeError, match="packaging failed"):
        run_gpt_bundle(
            BundleOptions(output_dir=output_dir, staging_dir=staging_dir, overwrite=True),
            catalog_resolver=lambda: [{"benchmark_id": "1", "title": "CIS Test", "version": "v1"}],
            downloader=downloader,
            packager=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("packaging failed")),
            catalog_refresher=catalog_fresh,
        )

    assert (output_dir / "old.md").read_text(encoding="utf-8") == "old"
    assert (staging_dir / ".cis-bench-staging.json").exists()


def test_catalog_auto_mode_skips_fresh_catalog(tmp_path, monkeypatch):
    db_path = tmp_path / "catalog.db"
    db = CatalogDatabase(db_path)
    db.initialize_schema()
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    db.set_metadata("last_full_scrape", (now - timedelta(hours=1)).isoformat())
    monkeypatch.setattr(Config, "get_catalog_db_path", staticmethod(lambda: db_path))

    result = refresh_catalog_if_needed("auto", now=now)

    assert result["catalog_refreshed"] is False
    assert result["catalog_timestamp"] == (now - timedelta(hours=1)).isoformat()


def test_catalog_never_mode_rejects_missing_catalog(tmp_path, monkeypatch):
    db_path = tmp_path / "missing.db"
    monkeypatch.setattr(Config, "get_catalog_db_path", staticmethod(lambda: db_path))

    with pytest.raises(RuntimeError, match="refresh is disabled"):
        refresh_catalog_if_needed("never")


def test_catalog_auto_mode_refreshes_stale_catalog(tmp_path, monkeypatch):
    from cis_bench.catalog.scraper import CatalogScraper
    from cis_bench.fetcher.auth import AuthManager

    db_path = tmp_path / "catalog.db"
    db = CatalogDatabase(db_path)
    db.initialize_schema()
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    db.set_metadata("last_full_scrape", (now - timedelta(days=2)).isoformat())
    monkeypatch.setattr(Config, "get_catalog_db_path", staticmethod(lambda: db_path))
    monkeypatch.setattr(Config, "ensure_directories", staticmethod(lambda: None))
    monkeypatch.setattr(
        AuthManager,
        "get_or_create_session",
        staticmethod(lambda **kwargs: SimpleNamespace(verify=False)),
    )
    monkeypatch.setattr(AuthManager, "validate_session", staticmethod(lambda *args, **kwargs: True))

    def fake_scrape(self, rate_limit_seconds):
        self.db.set_metadata("last_full_scrape", now.isoformat())
        return {"failed_pages": [], "total_benchmarks": 1}

    monkeypatch.setattr(CatalogScraper, "scrape_full_catalog", fake_scrape)

    result = refresh_catalog_if_needed("auto", now=now)

    assert result["catalog_refreshed"] is True
    assert result["catalog_timestamp"] == now.isoformat()
