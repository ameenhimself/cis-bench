#!/usr/bin/env python3
"""Package downloaded CIS benchmarks into GPT-friendly knowledge bundles."""

from __future__ import annotations

from cis_bench.gpt_knowledge import (
    build_parser,
    extract_family_name,
    html_to_markdown,
    main,
    package_downloads,
)

__all__ = [
    "build_parser",
    "extract_family_name",
    "html_to_markdown",
    "main",
    "package_downloads",
]


if __name__ == "__main__":
    raise SystemExit(main())
