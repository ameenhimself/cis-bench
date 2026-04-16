"""Tests for scripts/package_gpt_knowledge.py."""

import json
import sys
from pathlib import Path

import pytest

from cis_bench.models.benchmark import Benchmark, CISControl, MITREMapping, Recommendation

pytestmark = pytest.mark.scripts

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

from package_gpt_knowledge import package_downloads  # noqa: E402


def make_benchmark(
    benchmark_id: str,
    title: str,
    version: str,
    recommendation_count: int = 1,
    expected_recommendations: int | None = None,
):
    recommendations = []
    for idx in range(recommendation_count):
        recommendations.append(
            Recommendation(
                ref=f"1.{idx + 1}",
                title=f"Recommendation {idx + 1}",
                url=f"https://workbench.cisecurity.org/sections/{benchmark_id}/recommendations/{idx + 1}",
                assessment_status="Manual",
                profiles=["Level 1"],
                nist_controls=["AC-1"],
                cis_controls=[
                    CISControl(
                        version=8,
                        control="4.1",
                        title="Establish and Maintain a Secure Configuration Process",
                        ig1=True,
                        ig2=True,
                        ig3=False,
                    )
                ],
                mitre_mapping=MITREMapping(
                    techniques=["T1078"],
                    tactics=["TA0001"],
                    mitigations=["M1030"],
                ),
                description="<p>Description sentence one. Description sentence two.</p>",
                rationale="<p>Rationale sentence one. Rationale sentence two.</p>",
                audit="<p>Run the following command:</p><pre><code># audit-tool --check\n</code></pre>",
                remediation="<p>Install the package first.</p><pre><code># dnf install pkg\n</code></pre>",
                default_value="<p>Enabled by default.</p>",
                references='<p>See <a href="https://example.com/ref">vendor guidance</a>.</p>',
            )
        )
    return Benchmark(
        title=title,
        benchmark_id=benchmark_id,
        url=f"https://workbench.cisecurity.org/benchmarks/{benchmark_id}",
        version=version,
        scraper_version="test",
        total_recommendations=len(recommendations),
        expected_recommendations=expected_recommendations,
        recommendations=recommendations,
    )


class TestPackageDownloads:
    def test_package_downloads_groups_by_category_and_skips_invalid(self, tmp_path):
        input_dir = tmp_path / "downloads"
        output_dir = tmp_path / "knowledge"
        input_dir.mkdir()

        make_benchmark("1", "CIS Ubuntu Linux 22.04 Benchmark", "v1.0.0").to_json_file(
            input_dir / "ubuntu.json"
        )
        make_benchmark("2", "CIS AWS Foundations Benchmark", "v1.0.0").to_json_file(
            input_dir / "aws.json"
        )
        (input_dir / "partial.json").write_text("{invalid", encoding="utf-8")

        stats = package_downloads(input_dir, output_dir, max_chars=100000)

        assert stats.processed_files == 2
        assert stats.skipped_files == 1
        assert stats.skip_reasons["partial.json"] == "invalid benchmark JSON"
        assert (output_dir / "README.md").exists()
        assert (output_dir / "manifest.json").exists()
        files = sorted(path.name for path in output_dir.glob("*.md"))
        assert "cloud-01.md" in files
        assert "operating-systems-01.md" in files

        manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["summary_mode"] == "light-extractive"
        assert manifest["skip_reasons"]["partial.json"] == "invalid benchmark JSON"

    def test_package_downloads_splits_large_output(self, tmp_path):
        input_dir = tmp_path / "downloads"
        output_dir = tmp_path / "knowledge"
        input_dir.mkdir()

        make_benchmark(
            "1", "CIS Ubuntu Linux 22.04 Benchmark", "v1.0.0", recommendation_count=4
        ).to_json_file(input_dir / "ubuntu-a.json")
        make_benchmark(
            "2", "CIS Ubuntu Linux 24.04 Benchmark", "v1.0.0", recommendation_count=4
        ).to_json_file(input_dir / "ubuntu-b.json")

        stats = package_downloads(input_dir, output_dir, max_chars=1500)

        assert stats.category_files >= 2
        os_files = sorted(path.name for path in output_dir.glob("operating-systems-*.md"))
        assert len(os_files) >= 2

    def test_recommendation_rendering_is_source_faithful_and_structured(self, tmp_path):
        input_dir = tmp_path / "downloads"
        output_dir = tmp_path / "knowledge"
        input_dir.mkdir()

        benchmark = make_benchmark("1", "CIS Ubuntu Linux 22.04 Benchmark", "v1.0.0")
        benchmark.to_json_file(input_dir / "ubuntu.json")

        package_downloads(input_dir, output_dir, max_chars=100000)

        content = (output_dir / "operating-systems-01.md").read_text(encoding="utf-8")
        assert "## CIS Ubuntu Linux 22.04 Benchmark" in content
        assert "Recommendations Included: 1" in content
        assert "### 1.1 Recommendation 1" in content
        assert "Summary:" in content
        assert "- Purpose: Description sentence one." in content
        assert "- Why it matters: Rationale sentence one." in content
        assert "- What to do: Install the package first." in content
        assert "Assessment: Manual" in content
        assert "Profiles: Level 1" in content
        assert "NIST Controls: AC-1" in content
        assert "CIS Controls: v8 4.1 Establish and Maintain a Secure Configuration Process" in content
        assert "MITRE: Techniques: T1078 | Tactics: TA0001 | Mitigations: M1030" in content
        assert "#### Description" in content
        assert "#### Rationale" in content
        assert "#### Audit" in content
        assert "#### Remediation" in content
        assert "#### Default Value" in content
        assert "#### References" in content
        assert "```text" in content
        assert "# audit-tool --check" in content
        assert "[vendor guidance](https://example.com/ref)" in content

    def test_html_conversion_preserves_lists_and_links(self, tmp_path):
        input_dir = tmp_path / "downloads"
        output_dir = tmp_path / "knowledge"
        input_dir.mkdir()

        benchmark = make_benchmark("1", "CIS Ubuntu Linux 22.04 Benchmark", "v1.0.0")
        benchmark.recommendations[0].description = (
            "<p>Do these things:</p><ol><li>Step one</li><li>Step two</li></ol>"
        )
        benchmark.recommendations[0].references = (
            '<p><a href="https://example.com/doc">Reference doc</a></p>'
        )
        benchmark.to_json_file(input_dir / "ubuntu.json")

        package_downloads(input_dir, output_dir, max_chars=100000)

        content = (output_dir / "operating-systems-01.md").read_text(encoding="utf-8")
        assert "1. Step one" in content
        assert "2. Step two" in content
        assert "[Reference doc](https://example.com/doc)" in content

    def test_skips_verified_incomplete_benchmark_and_records_reason(self, tmp_path):
        input_dir = tmp_path / "downloads"
        output_dir = tmp_path / "knowledge"
        input_dir.mkdir()

        mismatched = make_benchmark(
            "1",
            "CIS Ubuntu Linux 22.04 Benchmark",
            "v1.0.0",
            recommendation_count=1,
            expected_recommendations=2,
        )
        mismatched.to_json_file(input_dir / "mismatch.json")

        stats = package_downloads(input_dir, output_dir, max_chars=100000)

        assert stats.processed_files == 0
        assert stats.skipped_files == 1
        assert "expected_recommendations does not match total_recommendations" in stats.skip_reasons[
            "mismatch.json"
        ]

        readme = (output_dir / "README.md").read_text(encoding="utf-8")
        manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
        assert "mismatch.json" in readme
        assert manifest["skip_reasons"]["mismatch.json"].startswith(
            "expected_recommendations does not match total_recommendations"
        )

    def test_package_downloads_can_emit_single_file_bundle(self, tmp_path):
        input_dir = tmp_path / "downloads"
        output_dir = tmp_path / "knowledge"
        input_dir.mkdir()

        make_benchmark("1", "CIS Ubuntu Linux 22.04 Benchmark", "v1.0.0").to_json_file(
            input_dir / "ubuntu.json"
        )
        make_benchmark("2", "CIS AWS Foundations Benchmark", "v1.0.0").to_json_file(
            input_dir / "aws.json"
        )

        stats = package_downloads(input_dir, output_dir, max_chars=100000, single_file=True)

        assert stats.category_files == 1
        assert (output_dir / "all-benchmarks-01.md").exists()
        manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["packaging_mode"] == "single-file"

    def test_real_fixture_preserves_code_blocks(self, tmp_path):
        input_dir = tmp_path / "downloads"
        output_dir = tmp_path / "knowledge"
        input_dir.mkdir()

        fixture_path = Path("tests/fixtures/benchmarks/almalinux_complete.json")
        (input_dir / "almalinux_complete.json").write_text(
            fixture_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        package_downloads(input_dir, output_dir, max_chars=2000000)

        content = (output_dir / "operating-systems-01.md").read_text(encoding="utf-8")
        assert "#### Audit" in content
        assert "#### Remediation" in content
        assert "```text" in content
        assert "# rpm -q aide" in content
        assert "# dnf install aide" in content
