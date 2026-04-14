#!/usr/bin/env python3
"""Package downloaded CIS benchmarks into GPT-friendly knowledge bundles.

This utility reads benchmark JSON exports produced by ``cis-bench download`` and
consolidates them into a small number of text-forward Markdown files suitable
for upload as Custom GPT knowledge. It is intentionally tolerant of partially
written or invalid JSON files so it can be run while a bulk download is still
in progress.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Iterable

from bs4 import BeautifulSoup

from cis_bench.models.benchmark import Benchmark

CATEGORY_LABELS = {
    "cloud": "cloud",
    "os": "operating-systems",
    "database": "databases",
    "container": "containers-kubernetes",
    "application": "applications",
    None: "other",
}

CATEGORY_HINTS = [
    (["oracle cloud", "oci"], "cloud"),
    (["amazon web services", "aws", "foundation benchmark"], "cloud"),
    (["google cloud", "gcp"], "cloud"),
    (["azure"], "cloud"),
    (["alibaba cloud"], "cloud"),
    (["windows server", "windows"], "os"),
    (
        [
            "ubuntu",
            "red hat",
            "rhel",
            "oracle linux",
            "debian",
            "suse",
            "rocky linux",
            "almalinux",
            "amazon linux",
            "macos",
            "linux benchmark",
        ],
        "os",
    ),
    (["postgres", "postgresql", "mysql", "mongodb", "sql server", "database"], "database"),
    (["kubernetes", "k8s", "eks", "aks", "gke", "oke", "docker", "container"], "container"),
    (["nginx", "tomcat", "apache"], "application"),
]


@dataclass
class PackageStats:
    processed_files: int = 0
    skipped_files: int = 0
    category_files: int = 0
    benchmark_count: int = 0


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "bundle"


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    text = BeautifulSoup(value, "html.parser").get_text("\n")
    text = unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    return cleaned.strip()


def infer_category(title: str) -> str:
    lowered = title.lower()
    for hints, category in CATEGORY_HINTS:
        if any(hint in lowered for hint in hints):
            return CATEGORY_LABELS[category]
    return CATEGORY_LABELS[None]


def load_benchmark(path: Path) -> Benchmark | None:
    try:
        return Benchmark.from_json_file(str(path))
    except Exception:
        return None


def iter_downloaded_benchmarks(input_dir: Path) -> tuple[list[Benchmark], list[Path]]:
    benchmarks: list[Benchmark] = []
    skipped: list[Path] = []

    for path in sorted(input_dir.glob("*.json")):
        benchmark = load_benchmark(path)
        if benchmark is None:
            skipped.append(path)
            continue
        benchmarks.append(benchmark)

    return benchmarks, skipped


def render_recommendation(rec) -> str:
    sections: list[str] = [f"### {rec.ref} {rec.title}"]
    sections.append(f"Assessment: {rec.assessment_status}")

    if rec.profiles:
        sections.append(f"Profiles: {', '.join(rec.profiles)}")
    if rec.nist_controls:
        sections.append(f"NIST Controls: {', '.join(rec.nist_controls)}")
    if rec.cis_controls:
        controls = ", ".join(f"v{c.version} {c.control} {c.title}" for c in rec.cis_controls)
        sections.append(f"CIS Controls: {controls}")
    if rec.mitre_mapping:
        mitre_parts = []
        if rec.mitre_mapping.techniques:
            mitre_parts.append(f"Techniques: {', '.join(rec.mitre_mapping.techniques)}")
        if rec.mitre_mapping.tactics:
            mitre_parts.append(f"Tactics: {', '.join(rec.mitre_mapping.tactics)}")
        if rec.mitre_mapping.mitigations:
            mitre_parts.append(f"Mitigations: {', '.join(rec.mitre_mapping.mitigations)}")
        if mitre_parts:
            sections.append("MITRE: " + " | ".join(mitre_parts))

    field_map = [
        ("Description", rec.description),
        ("Rationale", rec.rationale),
        ("Impact", rec.impact),
        ("Audit", rec.audit),
        ("Remediation", rec.remediation),
        ("Additional Info", rec.additional_info),
        ("Default Value", rec.default_value),
        ("References", rec.references),
    ]
    for label, value in field_map:
        text = html_to_text(value)
        if text:
            sections.append(f"{label}:\n{text}")

    return "\n\n".join(sections)


def render_benchmark(benchmark: Benchmark) -> str:
    header = [
        f"## {benchmark.title}",
        f"Benchmark ID: {benchmark.benchmark_id}",
        f"Version: {benchmark.version}",
        f"Source: {benchmark.url}",
        f"Recommendations: {benchmark.total_recommendations}",
    ]
    recommendations = "\n\n".join(render_recommendation(rec) for rec in benchmark.recommendations)
    return "\n".join(header) + "\n\n" + recommendations.strip() + "\n"


def chunk_documents(title: str, docs: Iterable[str], max_chars: int) -> list[str]:
    chunks: list[str] = []
    current = [f"# {title}", ""]
    current_len = sum(len(part) for part in current)

    for doc in docs:
        if current_len > 0 and current_len + len(doc) + 2 > max_chars and len(current) > 2:
            chunks.append("\n".join(current).strip() + "\n")
            current = [f"# {title}", ""]
            current_len = sum(len(part) for part in current)

        current.append(doc)
        current_len += len(doc) + 1

    if len(current) > 2:
        chunks.append("\n".join(current).strip() + "\n")

    return chunks


def build_manifest(
    output_dir: Path,
    grouped: dict[str, list[Benchmark]],
    skipped: list[Path],
    stats: PackageStats,
) -> None:
    manifest = {
        "processed_files": stats.processed_files,
        "skipped_files": stats.skipped_files,
        "categories": {
            category: [
                {
                    "benchmark_id": benchmark.benchmark_id,
                    "title": benchmark.title,
                    "version": benchmark.version,
                    "recommendations": benchmark.total_recommendations,
                }
                for benchmark in benchmarks
            ]
            for category, benchmarks in grouped.items()
        },
        "skipped_paths": [str(path) for path in skipped],
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def package_downloads(input_dir: Path, output_dir: Path, max_chars: int = 1_200_000) -> PackageStats:
    output_dir.mkdir(parents=True, exist_ok=True)
    benchmarks, skipped = iter_downloaded_benchmarks(input_dir)

    grouped: dict[str, list[Benchmark]] = defaultdict(list)
    for benchmark in benchmarks:
        grouped[infer_category(benchmark.title)].append(benchmark)

    stats = PackageStats(
        processed_files=len(benchmarks),
        skipped_files=len(skipped),
        benchmark_count=len(benchmarks),
    )

    index_lines = [
        "# CIS Benchmark GPT Knowledge Bundles",
        "",
        f"Processed benchmark files: {stats.processed_files}",
        f"Skipped files: {stats.skipped_files}",
        "",
        "These files are intended for Custom GPT knowledge uploads. Each category file contains complete recommendation text in Markdown form.",
        "",
    ]

    for category in sorted(grouped):
        benchmarks_in_category = sorted(
            grouped[category],
            key=lambda b: (b.title.lower(), b.version.lower(), b.benchmark_id),
        )
        rendered = [render_benchmark(benchmark) for benchmark in benchmarks_in_category]
        chunks = chunk_documents(f"CIS Benchmarks - {category}", rendered, max_chars=max_chars)

        for idx, chunk in enumerate(chunks, start=1):
            filename = f"{slugify(category)}-{idx:02d}.md"
            (output_dir / filename).write_text(chunk, encoding="utf-8")
            stats.category_files += 1
            if idx == 1:
                index_lines.append(f"- {category}: {len(benchmarks_in_category)} benchmarks")
            index_lines.append(f"  File: {filename}")

    if skipped:
        index_lines.extend(["", "Skipped files:"])
        index_lines.extend(f"- {path.name}" for path in skipped)

    (output_dir / "README.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    build_manifest(output_dir, grouped, skipped, stats)
    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Package downloaded CIS benchmarks into GPT-friendly Markdown bundles."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("all-benchmarks"),
        help="Directory containing downloaded benchmark JSON files",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("gpt-knowledge"),
        help="Directory to write GPT-friendly Markdown bundles",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=1_200_000,
        help="Maximum characters per output Markdown file",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if not args.input_dir.exists():
        parser.error(f"Input directory not found: {args.input_dir}")

    stats = package_downloads(args.input_dir, args.output_dir, max_chars=args.max_chars)

    print(f"Processed {stats.processed_files} benchmark files")
    print(f"Skipped {stats.skipped_files} files that were incomplete or invalid")
    print(f"Wrote {stats.category_files} Markdown knowledge bundle(s) to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
