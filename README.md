# CIS Benchmark GPT Bundle Toolkit

> Bulk-download the latest CIS Benchmarks and package them into source-faithful Markdown for Custom GPT knowledge.

[![Python Version](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

## Purpose

This fork is optimized for one practical workflow: pull the latest CIS Benchmark content from CIS WorkBench, verify that downloaded benchmarks are complete, and convert the raw benchmark JSON into a Custom GPT-friendly Markdown knowledge file.

The generated GPT knowledge keeps the original CIS recommendation substance intact, especially:

- Description
- Rationale
- Audit
- Remediation
- Default Value
- References

It adds only light deterministic summaries and structure so a Custom GPT can retrieve, summarize, and cite the benchmark evidence more reliably.

## Recommended Workflow

Authenticate once:

```bash
python -m cis_bench auth login --browser edge
```

If browser cookie extraction does not work on Windows, use the login command's cookie-file import support.

Refresh the local catalog metadata:

```bash
python -m cis_bench catalog refresh
```

Create the full Custom GPT deliverable:

```bash
python -m cis_bench gpt-bundle --benchmarks 2 --workers 4
```

By default, this creates a dated folder such as:

```text
CIS Benchmarks 200526/
  Raw Files/
    <raw benchmark JSON files>
  all-benchmarks-01.md
  README.md
  manifest.json
  RECEIPT.md
```

Upload `all-benchmarks-01.md` to the Custom GPT knowledge section. Keep `Raw Files` as the verified source archive.

## What `gpt-bundle` Does

`gpt-bundle` is the safest path for bulk download and GPT packaging:

- selects only benchmarks marked latest in the local catalog
- downloads into a staging folder first
- validates JSON/model parsing and recommendation counts
- retries missing or incomplete benchmarks
- moves verified raw files into `Raw Files`
- generates a source-faithful Markdown knowledge bundle
- writes a short receipt with counts, next steps, and credits

Use `--overwrite` if the dated output folder already exists:

```bash
python -m cis_bench gpt-bundle --overwrite --benchmarks 2 --workers 4
```

## Speed Tuning

There are two layers of parallelism:

- `--benchmarks`: how many benchmark documents download at the same time
- `--workers`: how many recommendation pages are fetched in parallel inside each benchmark

Start conservatively:

```bash
python -m cis_bench gpt-bundle --benchmarks 2 --workers 4
```

If CIS WorkBench remains responsive, try:

```bash
python -m cis_bench gpt-bundle --benchmarks 4 --workers 6
```

Avoid extreme values unless you are prepared for throttling, timeouts, or incomplete downloads that need retrying.

## Existing CLI Capabilities

This fork still keeps the general `cis-bench` CLI behavior from upstream:

```bash
python -m cis_bench catalog refresh
python -m cis_bench search "ubuntu 22" --latest
python -m cis_bench download 23598
python -m cis_bench export 23598 --format xccdf --style cis
```

Useful commands:

```bash
python -m cis_bench auth login
python -m cis_bench catalog refresh
python -m cis_bench search <query>
python -m cis_bench download <benchmark-id>
python -m cis_bench export <benchmark-id>
python -m cis_bench gpt-bundle
python -m cis_bench --help
```

`catalog refresh` downloads search metadata only. `download` and `gpt-bundle` pull full benchmark content to your machine.

## Developer Setup

```bash
git clone <this-fork-url>
cd cis-bench
pip install -e ".[dev]"
python -m pytest
python -m ruff check .
```

Useful paths:

- `src/cis_bench/cli/commands/`: CLI command handlers
- `src/cis_bench/gpt_bundle.py`: latest-download and Custom GPT deliverable workflow
- `src/cis_bench/gpt_knowledge.py`: Markdown knowledge packaging logic
- `src/cis_bench/fetcher/`: CIS WorkBench authentication and scraping
- `src/cis_bench/exporters/`: JSON, YAML, CSV, Markdown, and XCCDF exporters
- `tests/`: unit, integration, e2e, regression, and script tests

## Upstream Attribution

This repository is a fork of MITRE SAF Team's `cis-bench` project:

- Upstream repository: [https://github.com/mitre/cis-bench](https://github.com/mitre/cis-bench)
- Upstream documentation: [https://mitre.github.io/cis-bench/](https://mitre.github.io/cis-bench/)

The upstream project provides the core CIS WorkBench CLI, scraper, catalog, cache, exporter, and XCCDF foundations. This fork adds workflow automation for latest-benchmark bulk download, completeness validation, retry handling, and Custom GPT knowledge packaging.

CIS Benchmarks and CIS WorkBench are provided by the Center for Internet Security. This project does not replace CIS licensing, access requirements, or official benchmark distribution terms.

## License

Apache 2.0. See [LICENSE](LICENSE).
