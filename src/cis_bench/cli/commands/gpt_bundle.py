"""Custom GPT knowledge bundle command."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from cis_bench.gpt_bundle import BundleOptions, default_deliverable_dir, run_gpt_bundle

console = Console()


@click.command(name="gpt-bundle")
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Final dated folder to create. Defaults to 'CIS Benchmarks <ddMMyy>'.",
)
@click.option(
    "--staging-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional staging directory for raw downloads before validation.",
)
@click.option(
    "--workers",
    type=click.IntRange(1, 32),
    default=4,
    show_default=True,
    help="Recommendation fetch workers per benchmark.",
)
@click.option(
    "--benchmarks",
    type=click.IntRange(1, 16),
    default=2,
    show_default=True,
    help="Benchmarks to download concurrently.",
)
@click.option(
    "--max-retries",
    type=click.IntRange(0, 10),
    default=2,
    show_default=True,
    help="Retry passes for missing or incomplete benchmark downloads.",
)
@click.option("--overwrite", is_flag=True, help="Replace an existing output folder.")
@click.option("--keep-temp", is_flag=True, help="Copy temporary staging files into the output folder.")
@click.option(
    "--max-chars",
    type=int,
    default=1_200_000,
    show_default=True,
    help="Maximum characters per Markdown bundle when chunking is enabled.",
)
def gpt_bundle(
    output_dir,
    staging_dir,
    workers,
    benchmarks,
    max_retries,
    overwrite,
    keep_temp,
    max_chars,
):
    """Download latest CIS Benchmarks and package them for Custom GPT knowledge."""
    options = BundleOptions(
        output_dir=output_dir or default_deliverable_dir(),
        staging_dir=staging_dir,
        workers=workers,
        benchmarks=benchmarks,
        max_retries=max_retries,
        overwrite=overwrite,
        keep_temp=keep_temp,
        max_chars=max_chars,
        single_file=True,
    )

    try:
        result = run_gpt_bundle(options)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    console.print(f"[green]Completed {result.completed_count} benchmark(s).[/green]")
    console.print(f"Output folder: {result.output_dir}")
    console.print(f"Raw files: {result.raw_dir}")
    console.print(f"Knowledge bundles: {result.bundle_files}")
    console.print(f"Receipt: {result.receipt_path}")
