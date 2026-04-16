# CIS Benchmark CLI

> Download, search, cache, and export CIS Benchmarks from CIS WorkBench.

[![PyPI version](https://img.shields.io/pypi/v/cis-bench.svg)](https://pypi.org/project/cis-bench/)
[![Python Version](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![CI](https://github.com/mitre/cis-bench/actions/workflows/ci.yml/badge.svg)](https://github.com/mitre/cis-bench/actions/workflows/ci.yml)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

## Overview

`cis-bench` is a Python CLI for working with CIS Benchmarks from CIS WorkBench. It helps you:

- search the benchmark catalog locally
- authenticate once and reuse a saved session
- download benchmark content into a local cache
- export benchmarks to JSON, YAML, CSV, Markdown, and XCCDF
- generate XCCDF in either CIS-native or DISA-style layouts

This repository is best thought of as both:

- a user-facing CLI for security and compliance workflows
- a developer project with a scraper, local catalog database, exporters, and tests

## Quick Start

```bash
# Install one way
pipx install cis-bench
# or
uv tool install cis-bench
# or
pip install cis-bench

# Authenticate once
cis-bench auth login --browser chrome

# Build the local catalog metadata cache
cis-bench catalog refresh

# Search locally
cis-bench search "ubuntu 22"

# Download and export a benchmark
cis-bench download 23598
cis-bench export 23598 --format xccdf --style cis
```

If you want the all-in-one path:

```bash
cis-bench get "ubuntu 22" --format xccdf --style cis
```

## How It Works

### Authentication

`cis-bench` uses your CIS WorkBench session. Typical flow:

```bash
cis-bench auth login --browser chrome
```

If browser cookie extraction is awkward on your machine, the login command also supports importing cookies from a file.

### Catalog Refresh

`catalog refresh` builds a local metadata database for search and discovery. It does not download every full benchmark document.

```bash
cis-bench catalog refresh
```

Use the catalog when you want fast local search, latest-version filtering, and platform discovery.

### Benchmark Download

`download` fetches the actual benchmark content and stores it in the local downloaded-benchmark cache.

```bash
cis-bench download 23598
cis-bench download --file ids.txt --latest
```

### Export

Downloaded benchmarks can be exported without re-fetching from CIS WorkBench.

```bash
cis-bench export 23598 --format json
cis-bench export 23598 --format xccdf --style disa
```

## Common Workflows

### Search for the latest published benchmark

```bash
cis-bench search "amazon linux" --latest
```

### Download many latest benchmarks

```bash
cis-bench search --latest --output-format json | jq -r '.[].benchmark_id' > ids.txt
cis-bench download --file ids.txt --latest -o ./all-benchmarks
```

### Faster bulk downloads

`download` supports two layers of concurrency:

- `--benchmarks`: how many benchmarks download at the same time
- `--workers`: how many recommendation pages are fetched in parallel inside each benchmark

Good starting point:

```bash
cis-bench download --file ids.txt --latest \
  --benchmarks 2 \
  --workers 4 \
  -o ./all-benchmarks
```

Increase gradually if CIS WorkBench stays responsive for you.

### Export for SCAP tools

```bash
cis-bench export 23598 --format xccdf --style cis -o benchmark.xml
```

### Export for spreadsheets or downstream automation

```bash
cis-bench export 23598 --format csv -o benchmark.csv
cis-bench export 23598 --format json -o benchmark.json
```

## Key Commands

```bash
cis-bench auth login
cis-bench catalog refresh
cis-bench search <query>
cis-bench get <query>
cis-bench download <benchmark-id>
cis-bench export <benchmark-id>
cis-bench list
cis-bench info <benchmark-id>
```

For full option details, use `--help`:

```bash
cis-bench --help
cis-bench download --help
```

## Output and Caching Model

There are two important local stores:

- catalog database: search metadata produced by `cis-bench catalog refresh`
- downloaded benchmark cache: full benchmark content produced by `cis-bench download` or `cis-bench get`

That distinction matters:

- `catalog refresh` is for discovery and latest-version selection
- `download` is for pulling benchmark content to your machine
- `export` can work from cached downloaded content

## XCCDF Support

Two XCCDF styles are supported:

- `--style cis`: CIS-native XCCDF with richer benchmark metadata
- `--style disa`: DISA-style layout for environments that expect STIG-oriented output

Example:

```bash
cis-bench export 23598 --format xccdf --style cis
cis-bench export 23598 --format xccdf --style disa
```

## Developer Quick Start

```bash
# Clone and enter repo
git clone https://github.com/mitre/cis-bench.git
cd cis-bench

# Install dev dependencies
pip install -e ".[dev]"
# or
uv sync --dev

# Run tests
python -m pytest

# Run lint
python -m ruff check .
```

### Useful project paths

- `src/cis_bench/cli/`: CLI entrypoints and command handlers
- `src/cis_bench/catalog/`: catalog scraping and database logic
- `src/cis_bench/fetcher/`: WorkBench auth and scraping
- `src/cis_bench/exporters/`: JSON, YAML, CSV, Markdown, and XCCDF exporters
- `tests/`: unit, integration, and script tests
- `docs/`: MkDocs documentation source

### Developer notes

- Python requirement is `3.12+`
- the packaged CLI entrypoint is `cis-bench`
- tests are driven by `pytest`
- linting is driven by `ruff`
- script tests under `tests/scripts/` are not part of default pytest discovery

## Documentation

Full docs site:

- [https://mitre.github.io/cis-bench/](https://mitre.github.io/cis-bench/)

Useful starting points:

- [Getting Started](https://mitre.github.io/cis-bench/getting-started/)
- [Commands Reference](https://mitre.github.io/cis-bench/user-guide/commands-reference/)
- [Catalog Guide](https://mitre.github.io/cis-bench/user-guide/catalog-guide/)
- [XCCDF Guide](https://mitre.github.io/cis-bench/user-guide/xccdf-guide/)
- [Troubleshooting](https://mitre.github.io/cis-bench/user-guide/troubleshooting/)
- [Architecture Overview](https://mitre.github.io/cis-bench/developer-guide/architecture/)
- [Contributing Guide](https://mitre.github.io/cis-bench/developer-guide/contributing/)
- [Testing Guide](https://mitre.github.io/cis-bench/developer-guide/testing/)

## Why this README changed

This top-level README is intentionally optimized for:

- fast onboarding
- accurate command examples
- clear cache and download behavior
- a short developer handoff for contributors and coding agents

Deeper detail belongs in `docs/` and command-specific help output.

## License

Apache 2.0. See [LICENSE](LICENSE).
