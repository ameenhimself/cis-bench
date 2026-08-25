"""Tests for scripts/package_gpt_knowledge.py."""

import hashlib
import json
import sys
from pathlib import Path

import pytest

from cis_bench.models.benchmark import Benchmark, CISControl, MITREMapping, Recommendation

pytestmark = pytest.mark.scripts

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

from package_gpt_knowledge import extract_family_name, package_downloads  # noqa: E402


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
    def test_package_downloads_defaults_to_upload_safe_bundle(self, tmp_path):
        input_dir = tmp_path / "downloads"
        output_dir = tmp_path / "knowledge"
        input_dir.mkdir()

        make_benchmark("1", "CIS Ubuntu Linux 22.04 Benchmark", "v1.0.0").to_json_file(
            input_dir / "ubuntu.json"
        )
        make_benchmark("2", "CIS AWS Foundations Benchmark", "v1.0.0").to_json_file(
            input_dir / "aws.json"
        )

        stats = package_downloads(input_dir, output_dir, max_chars=100000)

        assert stats.bundle_files == 1
        assert (output_dir / "cis-benchmarks-knowledge-01-of-01.md").exists()
        manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["packaging_mode"] == "upload-safe"
        assert manifest["content_mode"] == "source-faithful-compact"

    def test_extract_family_name_preserves_meaningful_suffixes(self):
        assert extract_family_name("CIS Ubuntu Linux 22.04 LTS Benchmark v3.0.0") == "Ubuntu Linux 22.04 LTS Benchmark"
        assert extract_family_name("CIS Amazon Linux 2 STIG Benchmark v2.0.0") == "Amazon Linux 2 STIG Benchmark"
        assert extract_family_name(
            "CIS Apple macOS 14.0 Sonoma Cloud-tailored Benchmark v1.1.0"
        ) == "Apple macOS 14.0 Sonoma Cloud-tailored Benchmark"

    def test_upload_safe_manifest_records_hash_size_and_token_limit(self, tmp_path):
        input_dir = tmp_path / "downloads"
        output_dir = tmp_path / "knowledge"
        input_dir.mkdir()
        make_benchmark(
            "1", "CIS Ubuntu Linux 22.04 Benchmark", "v1.0.0", recommendation_count=6
        ).to_json_file(input_dir / "ubuntu.json")

        package_downloads(input_dir, output_dir, max_tokens=300, max_files=20)

        manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
        assert len(manifest["bundles"]) >= 2
        for bundle in manifest["bundles"]:
            content = (output_dir / bundle["filename"]).read_bytes()
            assert bundle["tokens"] <= 300
            assert bundle["bytes"] == len(content)
            assert bundle["sha256"] == hashlib.sha256(content).hexdigest()
            assert content.decode("utf-8").count("```") % 2 == 0

    def test_package_downloads_groups_by_family_and_skips_invalid(self, tmp_path):
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

        stats = package_downloads(input_dir, output_dir, max_chars=100000, single_file=False)

        assert stats.processed_files == 2
        assert stats.skipped_files == 1
        assert stats.skip_reasons["partial.json"] == "invalid benchmark JSON"
        assert (output_dir / "README.md").exists()
        assert (output_dir / "manifest.json").exists()
        files = sorted(path.name for path in output_dir.glob("*.md"))
        assert "aws-foundations-benchmark-01.md" in files
        assert "ubuntu-linux-22-04-benchmark-01.md" in files

        manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["content_mode"] == "source-faithful-compact"
        assert manifest["packaging_mode"] == "family-bundles"
        assert manifest["skip_reasons"]["partial.json"] == "invalid benchmark JSON"
        assert "Ubuntu Linux 22.04 Benchmark" in manifest["families"]

    def test_package_downloads_splits_large_output_within_family(self, tmp_path):
        input_dir = tmp_path / "downloads"
        output_dir = tmp_path / "knowledge"
        input_dir.mkdir()

        make_benchmark(
            "1", "CIS Ubuntu Linux 22.04 Benchmark", "v1.0.0", recommendation_count=4
        ).to_json_file(input_dir / "ubuntu-a.json")
        make_benchmark(
            "2", "CIS Ubuntu Linux 22.04 Benchmark", "v1.0.1", recommendation_count=4
        ).to_json_file(input_dir / "ubuntu-b.json")

        stats = package_downloads(input_dir, output_dir, max_chars=1500, single_file=False)

        assert stats.bundle_files >= 2
        family_files = sorted(path.name for path in output_dir.glob("ubuntu-linux-22-04-benchmark-*.md"))
        assert len(family_files) >= 2

    def test_recommendation_rendering_is_source_faithful_and_structured(self, tmp_path):
        input_dir = tmp_path / "downloads"
        output_dir = tmp_path / "knowledge"
        input_dir.mkdir()

        benchmark = make_benchmark("1", "CIS Ubuntu Linux 22.04 Benchmark", "v1.0.0")
        benchmark.to_json_file(input_dir / "ubuntu.json")

        package_downloads(input_dir, output_dir, max_chars=100000, single_file=False)

        content = (output_dir / "ubuntu-linux-22-04-benchmark-01.md").read_text(encoding="utf-8")
        assert "## CIS Ubuntu Linux 22.04 Benchmark" in content
        assert "Recommendations Included: 1" in content
        assert "### 1.1 Recommendation 1" in content
        assert "Summary:" not in content
        assert "Assessment: Manual" in content
        assert "Profiles: Level 1" in content
        assert "Mappings: CIS: v8 4.1 Establish and Maintain a Secure Configuration Process" in content
        assert "NIST: AC-1" in content
        assert "MITRE: Techniques: T1078, Tactics: TA0001, Mitigations: M1030" in content
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

        package_downloads(input_dir, output_dir, max_chars=100000, single_file=False)

        content = (output_dir / "ubuntu-linux-22-04-benchmark-01.md").read_text(encoding="utf-8")
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

        stats = package_downloads(input_dir, output_dir, max_chars=100000, single_file=False)

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

    def test_rerun_preserves_unrecognized_markdown_files(self, tmp_path):
        input_dir = tmp_path / "downloads"
        output_dir = tmp_path / "knowledge"
        input_dir.mkdir()
        output_dir.mkdir()

        (output_dir / "old-generated-01.md").write_text("old", encoding="utf-8")
        (output_dir / "notes.md").write_text("keep", encoding="utf-8")
        (output_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "packaging_mode": "upload-safe",
                    "bundles": [{"filename": "old-generated-01.md"}],
                }
            ),
            encoding="utf-8",
        )
        (output_dir / "README.md").write_text("old readme", encoding="utf-8")

        make_benchmark("1", "CIS Ubuntu Linux 22.04 Benchmark", "v1.0.0").to_json_file(
            input_dir / "ubuntu.json"
        )

        package_downloads(input_dir, output_dir, max_chars=100000, single_file=False)

        assert not (output_dir / "old-generated-01.md").exists()
        assert (output_dir / "notes.md").read_text(encoding="utf-8") == "keep"
        assert (output_dir / "README.md").exists()
        assert (output_dir / "manifest.json").exists()
        assert (output_dir / "ubuntu-linux-22-04-benchmark-01.md").exists()

    def test_packager_refuses_unrecognized_existing_readme(self, tmp_path):
        input_dir = tmp_path / "downloads"
        output_dir = tmp_path / "knowledge"
        input_dir.mkdir()
        output_dir.mkdir()
        (output_dir / "README.md").write_text("important", encoding="utf-8")
        make_benchmark("1", "CIS Ubuntu Linux 22.04 Benchmark", "v1.0.0").to_json_file(
            input_dir / "ubuntu.json"
        )

        with pytest.raises(ValueError, match="unrecognized packaging output"):
            package_downloads(input_dir, output_dir)

        assert (output_dir / "README.md").read_text(encoding="utf-8") == "important"

    def test_single_file_mode_rejects_output_over_limit(self, tmp_path):
        input_dir = tmp_path / "downloads"
        output_dir = tmp_path / "knowledge"
        input_dir.mkdir()

        make_benchmark(
            "1", "CIS Ubuntu Linux 22.04 Benchmark", "v1.0.0", recommendation_count=4
        ).to_json_file(input_dir / "ubuntu-a.json")
        make_benchmark(
            "2", "CIS AWS Foundations Benchmark", "v1.0.0", recommendation_count=4
        ).to_json_file(input_dir / "aws.json")

        with pytest.raises(ValueError, match="Single-file output exceeds"):
            package_downloads(input_dir, output_dir, max_chars=1500, single_file=True)

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

        assert stats.bundle_files == 1
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

        package_downloads(input_dir, output_dir, max_chars=2000000, single_file=False)

        content = (output_dir / "almalinux-os-8-benchmark-01.md").read_text(encoding="utf-8")
        assert "#### Audit" in content
        assert "#### Remediation" in content
        assert "```text" in content
        assert "# rpm -q aide" in content
        assert "# dnf install aide" in content
