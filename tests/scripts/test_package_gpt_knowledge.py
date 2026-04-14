"""Tests for scripts/package_gpt_knowledge.py."""

import sys
from pathlib import Path

import pytest

from cis_bench.models.benchmark import Benchmark, Recommendation

pytestmark = pytest.mark.scripts

sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'scripts'))

from package_gpt_knowledge import package_downloads  # noqa: E402


def make_benchmark(benchmark_id: str, title: str, version: str, recommendation_count: int = 1):
    recommendations = [
        Recommendation(
            ref=f'1.{idx + 1}',
            title=f'Recommendation {idx + 1}',
            url=f'https://workbench.cisecurity.org/sections/{benchmark_id}/recommendations/{idx + 1}',
            assessment_status='Manual',
            profiles=['Level 1'],
            description='<p>Description</p>',
            remediation='<p>Remediation</p>',
        )
        for idx in range(recommendation_count)
    ]
    return Benchmark(
        title=title,
        benchmark_id=benchmark_id,
        url=f'https://workbench.cisecurity.org/benchmarks/{benchmark_id}',
        version=version,
        scraper_version='test',
        total_recommendations=len(recommendations),
        recommendations=recommendations,
    )


class TestPackageDownloads:
    def test_package_downloads_groups_by_category_and_skips_invalid(self, tmp_path):
        input_dir = tmp_path / 'downloads'
        output_dir = tmp_path / 'knowledge'
        input_dir.mkdir()

        make_benchmark('1', 'CIS Ubuntu Linux 22.04 Benchmark', 'v1.0.0').to_json_file(input_dir / 'ubuntu.json')
        make_benchmark('2', 'CIS AWS Foundations Benchmark', 'v1.0.0').to_json_file(input_dir / 'aws.json')
        (input_dir / 'partial.json').write_text('{invalid', encoding='utf-8')

        stats = package_downloads(input_dir, output_dir, max_chars=100000)

        assert stats.processed_files == 2
        assert stats.skipped_files == 1
        assert (output_dir / 'README.md').exists()
        assert (output_dir / 'manifest.json').exists()
        files = sorted(path.name for path in output_dir.glob('*.md'))
        assert 'cloud-01.md' in files
        assert 'operating-systems-01.md' in files

    def test_package_downloads_splits_large_output(self, tmp_path):
        input_dir = tmp_path / 'downloads'
        output_dir = tmp_path / 'knowledge'
        input_dir.mkdir()

        make_benchmark('1', 'CIS Ubuntu Linux 22.04 Benchmark', 'v1.0.0', recommendation_count=4).to_json_file(input_dir / 'ubuntu-a.json')
        make_benchmark('2', 'CIS Ubuntu Linux 24.04 Benchmark', 'v1.0.0', recommendation_count=4).to_json_file(input_dir / 'ubuntu-b.json')

        stats = package_downloads(input_dir, output_dir, max_chars=500)

        assert stats.category_files >= 2
        os_files = sorted(path.name for path in output_dir.glob('operating-systems-*.md'))
        assert len(os_files) >= 2
