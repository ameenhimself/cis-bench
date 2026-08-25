# CIS Benchmark GPT Bundle Toolkit

> Download the latest published CIS Benchmarks, verify completeness, and package them as source-faithful Custom GPT knowledge files.

[![Python Version](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

## Recommended Workflow

Authenticate once:

```powershell
python -m cis_bench auth login --browser edge
```

Create a complete Custom GPT deliverable:

```powershell
python -m cis_bench gpt-bundle
```

The command refreshes a missing or stale catalog, resolves the latest published version of every benchmark, resumes verified staged downloads, retries incomplete files, and writes upload-safe knowledge files.

Default parallelism is conservative. Increase it only while CIS WorkBench remains responsive:

```powershell
python -m cis_bench gpt-bundle --benchmarks 4 --workers 6
```

`--benchmarks` controls concurrent benchmark documents. `--workers` controls recommendation requests inside each benchmark. Their product approximates active request concurrency.

## Generated Deliverable

A run on 25 August 2026 creates:

```text
CIS Benchmarks 250826/
  Raw Files/
    <verified benchmark JSON files>
  cis-benchmarks-knowledge-01-of-N.md
  cis-benchmarks-knowledge-02-of-N.md
  CUSTOM_GPT_INSTRUCTIONS.md
  README.md
  manifest.json
  RECEIPT.md
```

Upload every `cis-benchmarks-knowledge-*.md` file to Custom GPT Knowledge. Paste the contents of `CUSTOM_GPT_INSTRUCTIONS.md` into the GPT Instructions field. Keep `Raw Files` as the verified source archive.

Knowledge files preserve each recommendation's Description, Rationale, Audit, Remediation, Default Value, and References. Generated summaries are intentionally omitted because they duplicate source text. Profiles and CIS, NIST, and MITRE mappings remain in compact metadata.

Packaging defaults to at most 1,800,000 tokens per file and 20 files. This stays below OpenAI's documented [2 million token limit per text file](https://help.openai.com/en/articles/8555545-file-uploads-faq) and [20-file Custom GPT knowledge limit](https://help.openai.com/en/articles/8554397-creating-with-chatgpt). Use `--single-file` only for smaller collections; the command rejects a single file that exceeds upload limits instead of writing an unusable artifact.

## Reliability And Recovery

`gpt-bundle` stores working downloads under the CIS Bench application-data directory. If a run stops, rerun the same command. Existing staged JSON is validated first, and only missing or incomplete benchmark IDs are queued.

Catalog behavior:

```powershell
python -m cis_bench gpt-bundle --catalog-refresh auto
python -m cis_bench gpt-bundle --catalog-refresh always
python -m cis_bench gpt-bundle --catalog-refresh never
```

`auto` is the default and performs a full refresh when the catalog is missing or older than 24 hours. A partial catalog refresh is never accepted as current.

Existing dated output is protected. `--overwrite` replaces only a directory recognized as a prior CIS bundle:

```powershell
python -m cis_bench gpt-bundle --overwrite
```

The replacement is built in a partial directory and published only after download validation and knowledge packaging succeed.

## Authentication

Browser-cookie extraction can require elevated access on Windows. If extraction fails, export CIS WorkBench cookies as JSON and import them with the authentication command's cookie-file option. Check current options with:

```powershell
python -m cis_bench auth login --help
```

## Existing CLI Capabilities

This fork retains upstream search, download, cache, and export commands:

```powershell
python -m cis_bench catalog refresh
python -m cis_bench search "ubuntu 24" --latest
python -m cis_bench download 23598 --workers 4
python -m cis_bench export 23598 --format xccdf --style cis
python -m cis_bench --help
```

`catalog refresh` downloads metadata. `download` and `gpt-bundle` download full recommendation content.

## Developer Setup

```powershell
git clone https://github.com/ameenhimself/cis-bench.git
cd cis-bench
pip install -e ".[dev]"
python -m pytest
python -m ruff check .
```

Core workflow code lives in `src/cis_bench/gpt_bundle.py`. Knowledge rendering and token-aware packaging live in `src/cis_bench/gpt_knowledge.py`.

## Fork Notice And Attribution

This repository is a fork of the [MITRE SAF Team cis-bench project](https://github.com/mitre/cis-bench). Upstream provides the CIS WorkBench scraper, authentication, catalog, cache, exporter, and XCCDF foundations. This fork adds resumable latest-benchmark bulk download, completeness validation, safe publication, and Custom GPT knowledge packaging.

CIS Benchmarks and CIS WorkBench are provided by the Center for Internet Security. Users remain responsible for CIS licensing, distribution rights, and ChatGPT workspace data controls. This tool does not replace official CIS guidance.

Key packages include Click, Rich, Requests, Beautiful Soup, Pydantic, SQLModel, and tiktoken.

## License

Apache License 2.0. See [LICENSE](LICENSE).
