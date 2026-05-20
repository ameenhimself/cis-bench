"""Unit tests for the Custom GPT bundle workflow."""

from datetime import datetime
from pathlib import Path

import pytest

from cis_bench.gpt_bundle import (
    BundleOptions,
    default_deliverable_dir,
    run_gpt_bundle,
    validate_staged_downloads,
)
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

    def packager(input_dir, knowledge_dir, *, max_chars, single_file):
        packaged["input_dir"] = input_dir
        packaged["knowledge_dir"] = knowledge_dir
        (knowledge_dir / "all-benchmarks-01.md").write_text("bundle", encoding="utf-8")
        return type(
            "Stats",
            (),
            {"processed_files": 2, "skipped_files": 0, "bundle_files": 1},
        )()

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
    )

    assert calls == [["1", "2"], ["2"]]
    assert result.completed_count == 2
    assert sorted(path.name for path in (output_dir / "Raw Files").glob("*.json")) == [
        "one.json",
        "two.json",
    ]
    assert packaged["input_dir"] == output_dir / "Raw Files"
    assert (output_dir / "RECEIPT.md").exists()
    assert "Next step: upload `all-benchmarks-01.md`" in (
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
        )


def test_run_gpt_bundle_overwrites_existing_output_folder(tmp_path):
    output_dir = tmp_path / "CIS Benchmarks 200526"
    staging_dir = tmp_path / "staging"
    output_dir.mkdir()
    (output_dir / "stale.md").write_text("old", encoding="utf-8")

    def downloader(ids, destination, **kwargs):
        destination.mkdir(parents=True, exist_ok=True)
        make_benchmark("1").to_json_file(destination / "one.json")

    def packager(input_dir, knowledge_dir, **kwargs):
        (knowledge_dir / "all-benchmarks-01.md").write_text("bundle", encoding="utf-8")
        return type(
            "Stats",
            (),
            {"processed_files": 1, "skipped_files": 0, "bundle_files": 1},
        )()

    run_gpt_bundle(
        BundleOptions(
            output_dir=output_dir,
            staging_dir=staging_dir,
            overwrite=True,
        ),
        catalog_resolver=lambda: [{"benchmark_id": "1", "title": "CIS Test", "version": "v1"}],
        downloader=downloader,
        packager=packager,
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
        )
