"""Custom GPT knowledge bundle command."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from cis_bench.gpt_bundle import BundleOptions, default_deliverable_dir, run_gpt_bundle
from cis_bench.gpt_knowledge import DEFAULT_MAX_FILES, DEFAULT_MAX_TOKENS

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
    default=5,
    show_default=True,
    help="Retry passes for missing or incomplete benchmark downloads.",
)
@click.option("--overwrite", is_flag=True, help="Replace an existing output folder.")
@click.option("--keep-temp", is_flag=True, help="Keep staging files after a successful run.")
@click.option(
    "--catalog-refresh",
    type=click.Choice(["auto", "always", "never"]),
    default="auto",
    show_default=True,
    help="Catalog freshness policy before resolving latest benchmarks.",
)
@click.option(
    "--max-chars",
    type=int,
    default=None,
    help="Optional secondary character limit per knowledge file.",
)
@click.option("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, show_default=True)
@click.option("--max-files", type=int, default=DEFAULT_MAX_FILES, show_default=True)
@click.option(
    "--single-file",
    is_flag=True,
    help="Require one knowledge file; fails when content exceeds upload limits.",
)
def gpt_bundle(
    output_dir,
    staging_dir,
    workers,
    benchmarks,
    max_retries,
    overwrite,
    keep_temp,
    catalog_refresh,
    max_chars,
    max_tokens,
    max_files,
    single_file,
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
        catalog_refresh=catalog_refresh,
        max_chars=max_chars,
        max_tokens=max_tokens,
        max_files=max_files,
        single_file=True if single_file else None,
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
