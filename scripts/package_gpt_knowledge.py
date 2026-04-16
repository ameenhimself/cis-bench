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
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import Iterable
import warnings

from bs4 import (
    BeautifulSoup,
    MarkupResemblesLocatorWarning,
    NavigableString,
    Tag,
    XMLParsedAsHTMLWarning,
)
from rich.progress import track

from cis_bench.models.benchmark import Benchmark, Recommendation

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

BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "ul",
    "ol",
    "li",
    "pre",
    "table",
    "tr",
    "td",
    "th",
    "blockquote",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
}


@dataclass
class LoadedBenchmark:
    path: Path
    benchmark: Benchmark


@dataclass
class PackageStats:
    processed_files: int = 0
    skipped_files: int = 0
    category_files: int = 0
    benchmark_count: int = 0
    skip_reasons: dict[str, str] = field(default_factory=dict)


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "bundle"


def collapse_blank_lines(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _normalize_inline_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _render_inline(node) -> str:
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""

    name = node.name.lower()
    if name == "br":
        return "\n"

    child_text = "".join(_render_inline(child) for child in node.children)
    child_text = _normalize_inline_text(child_text)

    if name in {"strong", "b"} and child_text:
        return f"**{child_text}**"
    if name in {"em", "i"} and child_text:
        return f"*{child_text}*"
    if name == "code" and child_text:
        return f"`{child_text}`"
    if name == "a":
        href = node.get("href", "").strip()
        if child_text and href:
            return f"[{child_text}]({href})"
        return child_text or href

    return child_text


def _render_list(tag: Tag, ordered: bool, depth: int = 0) -> list[str]:
    lines: list[str] = []
    index = 1
    for item in tag.find_all("li", recursive=False):
        indent = "  " * depth
        marker = f"{index}." if ordered else "-"
        index += 1

        inline_parts: list[str] = []
        child_lists: list[Tag] = []
        for child in item.children:
            if isinstance(child, Tag) and child.name.lower() in {"ul", "ol"}:
                child_lists.append(child)
            else:
                rendered = _render_block(child)
                if rendered:
                    inline_parts.append(rendered)
        inline_text = collapse_blank_lines("\n".join(part for part in inline_parts if part))
        if inline_text:
            inline_text = re.sub(r"\n", " ", inline_text).strip()
            lines.append(f"{indent}{marker} {inline_text}")
        else:
            lines.append(f"{indent}{marker}")

        for child_list in child_lists:
            lines.extend(_render_list(child_list, child_list.name.lower() == "ol", depth + 1))
    return lines


def _render_block(node) -> str:
    if isinstance(node, NavigableString):
        return _normalize_inline_text(str(node))
    if not isinstance(node, Tag):
        return ""

    name = node.name.lower()

    if name == "pre":
        code = node.get_text("\n")
        code = unescape(code).strip("\n")
        return f"```text\n{code}\n```" if code else ""

    if name in {"ul", "ol"}:
        return "\n".join(_render_list(node, name == "ol"))

    if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = min(int(name[1]), 6)
        title = _normalize_inline_text("".join(_render_inline(child) for child in node.children))
        return f"{'#' * level} {title}" if title else ""

    if name == "blockquote":
        text = collapse_blank_lines("\n".join(_render_block(child) for child in node.children))
        if not text:
            return ""
        return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())

    if name == "table":
        rows: list[str] = []
        for row in node.find_all("tr"):
            cells = [collapse_blank_lines(_render_block(cell)) for cell in row.find_all(["th", "td"])]
            cells = [cell.replace("\n", " ").strip() for cell in cells if cell.strip()]
            if cells:
                rows.append(" | ".join(cells))
        return "\n".join(rows)

    if name in {"p", "div", "section", "article", "li"}:
        child_blocks: list[str] = []
        inline_chunks: list[str] = []
        for child in node.children:
            if isinstance(child, Tag) and child.name and child.name.lower() in BLOCK_TAGS:
                if inline_chunks:
                    inline_text = _normalize_inline_text("".join(inline_chunks))
                    if inline_text:
                        child_blocks.append(inline_text)
                    inline_chunks = []
                block = _render_block(child)
                if block:
                    child_blocks.append(block)
            else:
                inline_chunks.append(_render_inline(child))

        if inline_chunks:
            inline_text = _normalize_inline_text("".join(inline_chunks))
            if inline_text:
                child_blocks.append(inline_text)

        return collapse_blank_lines("\n\n".join(part for part in child_blocks if part))

    return collapse_blank_lines("\n\n".join(_render_block(child) for child in node.children))


def html_to_markdown(value: str | None) -> str:
    if not value:
        return ""

    raw_value = unescape(value).strip()
    if not raw_value:
        return ""
    if "<" not in raw_value or ">" not in raw_value:
        return collapse_blank_lines(raw_value)

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)
        warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(raw_value, "html.parser")
    root = soup.body or soup
    pieces: list[str] = []
    for child in root.children:
        rendered = _render_block(child)
        if rendered:
            pieces.append(rendered)

    return collapse_blank_lines("\n\n".join(pieces))


def infer_category(title: str) -> str:
    lowered = title.lower()
    for hints, category in CATEGORY_HINTS:
        if any(hint in lowered for hint in hints):
            return CATEGORY_LABELS[category]
    return CATEGORY_LABELS[None]


def validate_benchmark(benchmark: Benchmark) -> str | None:
    if len(benchmark.recommendations) != benchmark.total_recommendations:
        return (
            "recommendation list length does not match total_recommendations "
            f"({len(benchmark.recommendations)}/{benchmark.total_recommendations})"
        )

    expected = getattr(benchmark, "expected_recommendations", None)
    if expected is not None and expected != benchmark.total_recommendations:
        return (
            "expected_recommendations does not match total_recommendations "
            f"({expected}/{benchmark.total_recommendations})"
        )

    return None


def load_benchmark(path: Path) -> tuple[Benchmark | None, str | None]:
    try:
        benchmark = Benchmark.from_json_file(str(path))
    except Exception:
        return None, "invalid benchmark JSON"

    return benchmark, validate_benchmark(benchmark)


def iter_downloaded_benchmarks(input_dir: Path) -> tuple[list[LoadedBenchmark], dict[str, str]]:
    benchmarks: list[LoadedBenchmark] = []
    skipped: dict[str, str] = {}

    paths = sorted(input_dir.glob("*.json"))
    for path in track(paths, description="Loading benchmarks"):
        benchmark, error = load_benchmark(path)
        if benchmark is None or error is not None:
            skipped[path.name] = error or "invalid benchmark JSON"
            continue
        benchmarks.append(LoadedBenchmark(path=path, benchmark=benchmark))

    return benchmarks, skipped


def _strip_summary_noise(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    cleaned_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.match(r"^[-*+]\s+", line):
            line = re.sub(r"^[-*+]\s+", "", line)
        line = re.sub(r"^\d+\.\s+", "", line)
        line = re.sub(r"^#+\s+", "", line)
        line = re.sub(r"^(#|\$)\s+", "", line)
        line = line.strip("` ")
        if line:
            cleaned_lines.append(line)
    return " ".join(cleaned_lines).strip()


def first_sentence(text: str) -> str:
    cleaned = _strip_summary_noise(text)
    if not cleaned:
        return ""

    match = re.search(r"(.+?[.!?])(?:\s|$)", cleaned)
    if match:
        return match.group(1).strip()
    return cleaned.splitlines()[0].strip()


def build_summary(description: str, rationale: str, remediation: str, audit: str) -> list[str]:
    summary_lines: list[str] = []

    purpose = first_sentence(description)
    if purpose:
        summary_lines.append(f"- Purpose: {purpose}")

    why = first_sentence(rationale)
    if why:
        summary_lines.append(f"- Why it matters: {why}")

    what_to_do = first_sentence(remediation) or first_sentence(audit)
    if what_to_do:
        summary_lines.append(f"- What to do: {what_to_do}")

    return summary_lines


def render_recommendation(rec: Recommendation) -> str:
    description = html_to_markdown(rec.description)
    rationale = html_to_markdown(rec.rationale)
    audit = html_to_markdown(rec.audit)
    remediation = html_to_markdown(rec.remediation)
    default_value = html_to_markdown(rec.default_value)
    references = html_to_markdown(rec.references)

    sections: list[str] = [f"### {rec.ref} {rec.title}"]

    summary_lines = build_summary(description, rationale, remediation, audit)
    if summary_lines:
        sections.append("Summary:\n" + "\n".join(summary_lines))

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
        ("Description", description),
        ("Rationale", rationale),
        ("Audit", audit),
        ("Remediation", remediation),
        ("Default Value", default_value),
        ("References", references),
    ]
    for label, value in field_map:
        if value:
            sections.append(f"#### {label}\n{value}")

    return "\n\n".join(sections)


def render_benchmark(benchmark: Benchmark) -> str:
    header = [
        f"## {benchmark.title}",
        f"Benchmark ID: {benchmark.benchmark_id}",
        f"Version: {benchmark.version}",
        f"Source: {benchmark.url}",
        f"Recommendations Included: {benchmark.total_recommendations}",
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
    skip_reasons: dict[str, str],
    stats: PackageStats,
) -> None:
    manifest = {
        "processed_files": stats.processed_files,
        "skipped_files": stats.skipped_files,
        "category_files": stats.category_files,
        "summary_mode": "light-extractive",
        "packaging_mode": "single-file" if len(grouped) == 1 and "all-benchmarks" in grouped else "category-bundles",
        "categories": {
            category: [
                {
                    "benchmark_id": benchmark.benchmark_id,
                    "title": benchmark.title,
                    "version": benchmark.version,
                    "recommendations": benchmark.total_recommendations,
                    **(
                        {"expected_recommendations": benchmark.expected_recommendations}
                        if benchmark.expected_recommendations is not None
                        else {}
                    ),
                }
                for benchmark in benchmarks
            ]
            for category, benchmarks in grouped.items()
        },
        "skipped_paths": sorted(skip_reasons),
        "skip_reasons": dict(sorted(skip_reasons.items())),
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def package_downloads(
    input_dir: Path,
    output_dir: Path,
    max_chars: int = 1_200_000,
    single_file: bool = False,
) -> PackageStats:
    output_dir.mkdir(parents=True, exist_ok=True)
    loaded_benchmarks, skip_reasons = iter_downloaded_benchmarks(input_dir)

    grouped: dict[str, list[Benchmark]] = defaultdict(list)
    for item in loaded_benchmarks:
        category = "all-benchmarks" if single_file else infer_category(item.benchmark.title)
        grouped[category].append(item.benchmark)

    stats = PackageStats(
        processed_files=len(loaded_benchmarks),
        skipped_files=len(skip_reasons),
        benchmark_count=len(loaded_benchmarks),
        skip_reasons=dict(skip_reasons),
    )

    index_lines = [
        "# CIS Benchmark GPT Knowledge Bundles",
        "",
        f"Processed benchmark files: {stats.processed_files}",
        f"Skipped files: {stats.skipped_files}",
        "Summary mode: light-extractive",
        "",
        "These files are intended for Custom GPT knowledge uploads.",
        "Recommendation content is source-faithful, lightly summarized, and converted into readable Markdown.",
        "Invalid files and benchmarks with verified recommendation-count mismatches were excluded.",
        "",
        "Generated bundles:",
        f"Packaging mode: {'single-file' if single_file else 'category-bundles'}",
    ]

    for category in track(sorted(grouped), description="Writing bundles"):
        benchmarks_in_category = sorted(
            grouped[category],
            key=lambda b: (b.title.lower(), b.version.lower(), b.benchmark_id),
        )
        rendered = [render_benchmark(benchmark) for benchmark in benchmarks_in_category]
        chunks = chunk_documents(f"CIS Benchmarks - {category}", rendered, max_chars=max_chars)

        index_lines.append(f"- {category}: {len(benchmarks_in_category)} benchmarks")
        for idx, chunk in enumerate(chunks, start=1):
            filename = f"{slugify(category)}-{idx:02d}.md"
            (output_dir / filename).write_text(chunk, encoding="utf-8")
            stats.category_files += 1
            index_lines.append(f"  File: {filename}")

    if skip_reasons:
        index_lines.extend(["", "Skipped files:"])
        for filename, reason in sorted(skip_reasons.items()):
            index_lines.append(f"- {filename}: {reason}")

    (output_dir / "README.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    build_manifest(output_dir, grouped, skip_reasons, stats)
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
    parser.add_argument(
        "--single-file",
        action="store_true",
        help="Package all benchmarks into one logical bundle instead of category bundles",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if not args.input_dir.exists():
        parser.error(f"Input directory not found: {args.input_dir}")

    stats = package_downloads(
        args.input_dir,
        args.output_dir,
        max_chars=args.max_chars,
        single_file=args.single_file,
    )

    print(f"Processed {stats.processed_files} benchmark files")
    print(f"Skipped {stats.skipped_files} files that were incomplete or invalid")
    print(f"Wrote {stats.category_files} Markdown knowledge bundle(s) to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
