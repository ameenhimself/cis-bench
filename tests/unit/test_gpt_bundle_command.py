"""CLI tests for the Custom GPT bundle command."""

from pathlib import Path
from unittest.mock import Mock, patch

from click.testing import CliRunner

from cis_bench.cli.app import cli


def test_gpt_bundle_command_is_registered():
    result = CliRunner().invoke(cli, ["gpt-bundle", "--help"])

    assert result.exit_code == 0
    assert "Download latest CIS Benchmarks and package them for Custom GPT knowledge" in result.output
    assert "--benchmarks" in result.output
    assert "--workers" in result.output
    assert "--catalog-refresh" in result.output
    assert "--max-tokens" in result.output
    assert "--max-files" in result.output


def test_gpt_bundle_command_invokes_workflow_with_defaults(tmp_path):
    fake_result = Mock(
        output_dir=Path("CIS Benchmarks 200526"),
        completed_count=2,
        retry_count=1,
        bundle_files=1,
    )

    with patch("cis_bench.cli.commands.gpt_bundle.run_gpt_bundle", return_value=fake_result) as run:
        result = CliRunner().invoke(
            cli,
            [
                "gpt-bundle",
                "--output-dir",
                str(tmp_path / "bundle"),
                "--staging-dir",
                str(tmp_path / "staging"),
                "--workers",
                "4",
                "--benchmarks",
                "2",
                "--max-retries",
                "3",
                "--overwrite",
                "--catalog-refresh",
                "never",
            ],
        )

    assert result.exit_code == 0, result.output
    options = run.call_args.args[0]
    assert options.output_dir == tmp_path / "bundle"
    assert options.staging_dir == tmp_path / "staging"
    assert options.workers == 4
    assert options.benchmarks == 2
    assert options.max_retries == 3
    assert options.overwrite is True
    assert options.catalog_refresh == "never"
    assert options.single_file is None
    assert "Completed 2 benchmark(s)" in result.output
