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
import hashlib
import json
import re
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import Iterable

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
    bundle_files: int = 0
    benchmark_count: int = 0
    skip_reasons: dict[str, str] = field(default_factory=dict)
    bundles: list[dict] = field(default_factory=list)


@dataclass
class RenderedDocument:
    """One benchmark or benchmark-part document ready for bundling."""

    text: str
    benchmark_id: str
    title: str
    version: str
    recommendation_start: str | None
    recommendation_end: str | None


DEFAULT_MAX_TOKENS = 1_800_000
DEFAULT_MAX_FILES = 20
TOKEN_ENCODING = "o200k_base"
_TOKENIZER = None


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


def extract_family_name(title: str) -> str:
    family = re.sub(r"^\s*CIS\s+", "", title, flags=re.IGNORECASE).strip()
    family = re.sub(r"\s+v(?:ersion\s+)?[0-9][\w.\-()]*\s*$", "", family, flags=re.IGNORECASE).strip()
    family = re.sub(r"\s+", " ", family).strip(" -")
    return family or title.strip()


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


def render_recommendation(rec: Recommendation) -> str:
    description = html_to_markdown(rec.description)
    rationale = html_to_markdown(rec.rationale)
    audit = html_to_markdown(rec.audit)
    remediation = html_to_markdown(rec.remediation)
    default_value = html_to_markdown(rec.default_value)
    references = html_to_markdown(rec.references)

    sections: list[str] = [f"### {rec.ref} {rec.title}"]

    metadata = [f"Assessment: {rec.assessment_status}"]
    if rec.profiles:
        metadata.append(f"Profiles: {', '.join(rec.profiles)}")

    mappings: list[str] = []
    if rec.cis_controls:
        controls = ", ".join(f"v{c.version} {c.control} {c.title}" for c in rec.cis_controls)
        mappings.append(f"CIS: {controls}")
    if rec.nist_controls:
        mappings.append(f"NIST: {', '.join(rec.nist_controls)}")
    if rec.mitre_mapping:
        mitre_parts = []
        if rec.mitre_mapping.techniques:
            mitre_parts.append(f"Techniques: {', '.join(rec.mitre_mapping.techniques)}")
        if rec.mitre_mapping.tactics:
            mitre_parts.append(f"Tactics: {', '.join(rec.mitre_mapping.tactics)}")
        if rec.mitre_mapping.mitigations:
            mitre_parts.append(f"Mitigations: {', '.join(rec.mitre_mapping.mitigations)}")
        if mitre_parts:
            mappings.append("MITRE: " + ", ".join(mitre_parts))
    if mappings:
        metadata.append("Mappings: " + " | ".join(mappings))
    sections.append("\n".join(metadata))

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


def _get_tokenizer():
    global _TOKENIZER
    if _TOKENIZER is None:
        try:
            import tiktoken
        except ImportError as exc:
            raise RuntimeError(
                "Token-aware packaging requires tiktoken. Reinstall cis-bench dependencies."
            ) from exc
        _TOKENIZER = tiktoken.get_encoding(TOKEN_ENCODING)
    return _TOKENIZER


def count_tokens(value: str) -> int:
    """Count tokens using tokenizer used by current OpenAI model families."""
    return len(_get_tokenizer().encode(value, disallowed_special=()))


def _within_limits(
    token_count: int,
    char_count: int,
    *,
    max_tokens: int,
    max_chars: int | None,
) -> bool:
    return token_count <= max_tokens and (max_chars is None or char_count <= max_chars)


def _benchmark_header(benchmark: Benchmark, part: tuple[int, int] | None = None) -> str:
    lines = [
        f"## {benchmark.title}",
        f"Benchmark ID: {benchmark.benchmark_id}",
        f"Version: {benchmark.version}",
        f"Source: {benchmark.url}",
        f"Recommendations Included: {benchmark.total_recommendations}",
    ]
    if part is not None:
        lines.append(f"Benchmark Part: {part[0]} of {part[1]}")
    return "\n".join(lines)


def render_benchmark(benchmark: Benchmark) -> str:
    recommendations = "\n\n".join(render_recommendation(rec) for rec in benchmark.recommendations)
    return _benchmark_header(benchmark) + "\n\n" + recommendations.strip() + "\n"


def render_benchmark_documents(
    benchmark: Benchmark,
    *,
    max_tokens: int,
    max_chars: int | None,
    file_header_tokens: int = 0,
    file_header_chars: int = 0,
) -> list[RenderedDocument]:
    """Render benchmark, splitting only between complete recommendations."""
    rendered_recommendations = [render_recommendation(rec) for rec in benchmark.recommendations]
    full_text = render_benchmark(benchmark)
    if _within_limits(
        count_tokens(full_text) + file_header_tokens,
        len(full_text) + file_header_chars,
        max_tokens=max_tokens,
        max_chars=max_chars,
    ):
        return [
            RenderedDocument(
                text=full_text,
                benchmark_id=benchmark.benchmark_id,
                title=benchmark.title,
                version=benchmark.version,
                recommendation_start=(
                    benchmark.recommendations[0].ref if benchmark.recommendations else None
                ),
                recommendation_end=(
                    benchmark.recommendations[-1].ref if benchmark.recommendations else None
                ),
            )
        ]

    base_header = _benchmark_header(benchmark)
    header_tokens = count_tokens(base_header) + file_header_tokens + 20
    header_chars = len(base_header) + file_header_chars + 40
    groups: list[list[tuple[Recommendation, str, int]]] = []
    current: list[tuple[Recommendation, str, int]] = []
    current_tokens = header_tokens
    current_chars = header_chars

    for rec, rendered in zip(benchmark.recommendations, rendered_recommendations, strict=True):
        rec_tokens = count_tokens(rendered) + 2
        rec_chars = len(rendered) + 2
        if not _within_limits(
            header_tokens + rec_tokens,
            header_chars + rec_chars,
            max_tokens=max_tokens,
            max_chars=max_chars,
        ):
            raise ValueError(
                f"Recommendation {benchmark.benchmark_id}/{rec.ref} exceeds knowledge file limit"
            )
        if current and not _within_limits(
            current_tokens + rec_tokens,
            current_chars + rec_chars,
            max_tokens=max_tokens,
            max_chars=max_chars,
        ):
            groups.append(current)
            current = []
            current_tokens = header_tokens
            current_chars = header_chars
        current.append((rec, rendered, rec_tokens))
        current_tokens += rec_tokens
        current_chars += rec_chars
    if current:
        groups.append(current)

    documents: list[RenderedDocument] = []
    total_parts = len(groups)
    for index, group in enumerate(groups, start=1):
        text = _benchmark_header(benchmark, (index, total_parts))
        text += "\n\n" + "\n\n".join(rendered for _, rendered, _ in group).strip() + "\n"
        final_tokens = count_tokens(text) + file_header_tokens
        if not _within_limits(
            final_tokens,
            len(text) + file_header_chars,
            max_tokens=max_tokens,
            max_chars=max_chars,
        ):
            raise ValueError(f"Benchmark part {benchmark.benchmark_id}/{index} exceeds file limit")
        documents.append(
            RenderedDocument(
                text=text,
                benchmark_id=benchmark.benchmark_id,
                title=benchmark.title,
                version=benchmark.version,
                recommendation_start=group[0][0].ref,
                recommendation_end=group[-1][0].ref,
            )
        )
    return documents


def chunk_rendered_documents(
    title: str,
    documents: Iterable[RenderedDocument],
    *,
    max_tokens: int,
    max_chars: int | None,
) -> list[tuple[str, list[RenderedDocument]]]:
    """Pack rendered documents into token-safe knowledge files."""
    header = f"# {title}\n\n"
    header_tokens = count_tokens(header)
    chunks: list[tuple[str, list[RenderedDocument]]] = []
    current: list[RenderedDocument] = []
    current_tokens = header_tokens
    current_chars = len(header)

    for document in documents:
        document_tokens = count_tokens(document.text) + 2
        document_chars = len(document.text) + 2
        if current and not _within_limits(
            current_tokens + document_tokens,
            current_chars + document_chars,
            max_tokens=max_tokens,
            max_chars=max_chars,
        ):
            text = header + "\n".join(item.text for item in current).strip() + "\n"
            chunks.append((text, current))
            current = []
            current_tokens = header_tokens
            current_chars = len(header)
        current.append(document)
        current_tokens += document_tokens
        current_chars += document_chars

    if current:
        text = header + "\n".join(item.text for item in current).strip() + "\n"
        chunks.append((text, current))
    return chunks


def cleanup_output_dir(output_dir: Path) -> None:
    """Remove only files known to have been generated by this packager."""
    if not output_dir.exists():
        return

    generated: set[str] = set()
    recognized = False
    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists():
        try:
            prior = json.loads(manifest_path.read_text(encoding="utf-8"))
            recognized = "packaging_mode" in prior or "bundles" in prior
            generated.update(
                bundle["filename"]
                for bundle in prior.get("bundles", [])
                if isinstance(bundle, dict) and bundle.get("filename")
            )
        except Exception:
            pass

    for pattern in ("cis-benchmarks-knowledge-*.md", "all-benchmarks-*.md"):
        matches = [path.name for path in output_dir.glob(pattern)]
        if matches:
            recognized = True
            generated.update(matches)
    if recognized:
        generated.update({"README.md", "manifest.json", "CUSTOM_GPT_INSTRUCTIONS.md"})
    elif any((output_dir / name).exists() for name in ("README.md", "manifest.json")):
        raise ValueError(f"Refusing to overwrite unrecognized packaging output: {output_dir}")
    for filename in generated:
        path = output_dir / filename
        if path.is_file():
            path.unlink()


def build_manifest(
    output_dir: Path,
    grouped: dict[str, list[Benchmark]],
    skip_reasons: dict[str, str],
    stats: PackageStats,
    packaging_mode: str,
    manifest_context: dict | None = None,
) -> None:
    manifest = {
        "processed_files": stats.processed_files,
        "skipped_files": stats.skipped_files,
        "bundle_files": stats.bundle_files,
        "content_mode": "source-faithful-compact",
        "packaging_mode": packaging_mode,
        "token_encoding": TOKEN_ENCODING,
        "bundles": stats.bundles,
        "families": {
            family: [
                {
                    "benchmark_id": benchmark.benchmark_id,
                    "title": benchmark.title,
                    "version": benchmark.version,
                    "recommendations": benchmark.total_recommendations,
                    "category": infer_category(benchmark.title),
                    **(
                        {"expected_recommendations": benchmark.expected_recommendations}
                        if benchmark.expected_recommendations is not None
                        else {}
                    ),
                }
                for benchmark in benchmarks
            ]
            for family, benchmarks in grouped.items()
        },
        "skipped_paths": sorted(skip_reasons),
        "skip_reasons": dict(sorted(skip_reasons.items())),
    }
    if manifest_context:
        manifest.update(manifest_context)
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def package_downloads(
    input_dir: Path,
    output_dir: Path,
    max_chars: int | None = None,
    single_file: bool | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_files: int = DEFAULT_MAX_FILES,
    manifest_context: dict | None = None,
) -> PackageStats:
    if max_tokens < 1:
        raise ValueError("max_tokens must be at least 1")
    if max_files < 1:
        raise ValueError("max_files must be at least 1")
    output_dir.mkdir(parents=True, exist_ok=True)
    cleanup_output_dir(output_dir)
    loaded_benchmarks, skip_reasons = iter_downloaded_benchmarks(input_dir)

    grouped: dict[str, list[Benchmark]] = defaultdict(list)
    sorted_grouped: dict[str, list[Benchmark]] = {}
    for item in loaded_benchmarks:
        family = (
            "all-benchmarks"
            if single_file is not False
            else extract_family_name(item.benchmark.title)
        )
        grouped[family].append(item.benchmark)

    for family, benchmarks in grouped.items():
        sorted_grouped[family] = sorted(
            benchmarks,
            key=lambda b: (b.title.lower(), b.version.lower(), b.benchmark_id),
        )

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
        "Content mode: source-faithful-compact",
        "",
        "These files are intended for Custom GPT knowledge uploads.",
        "Recommendation content is source-faithful, compact, and converted into readable Markdown.",
        "Invalid files and benchmarks with verified recommendation-count mismatches were excluded.",
        "",
        "Generated bundles:",
        "Packaging mode: "
        + (
            "single-file"
            if single_file is True
            else "family-bundles"
            if single_file is False
            else "upload-safe"
        ),
    ]

    file_header = "# CIS Benchmarks Knowledge\n\n"
    render_queue = [
        benchmark for family in sorted(sorted_grouped) for benchmark in sorted_grouped[family]
    ]
    documents_by_family: dict[str, list[RenderedDocument]] = defaultdict(list)
    for benchmark in track(render_queue, description="Rendering benchmarks"):
        family = (
            "all-benchmarks"
            if single_file is not False
            else extract_family_name(benchmark.title)
        )
        documents_by_family[family].extend(
            render_benchmark_documents(
                benchmark,
                max_tokens=max_tokens,
                max_chars=max_chars,
                file_header_tokens=count_tokens(file_header),
                file_header_chars=len(file_header),
            )
        )

    prepared: list[tuple[str, str, list[RenderedDocument]]] = []
    if single_file is True:
        documents = documents_by_family.get("all-benchmarks", [])
        content = file_header + "\n".join(document.text for document in documents).strip() + "\n"
        if not _within_limits(
            count_tokens(content),
            len(content),
            max_tokens=max_tokens,
            max_chars=max_chars,
        ):
            raise ValueError(
                "Single-file output exceeds Custom GPT file limit. Use upload-safe default mode."
            )
        prepared.append(("all-benchmarks", content, documents))
        packaging_mode = "single-file"
    elif single_file is False:
        packaging_mode = "family-bundles"
        for family in sorted(documents_by_family):
            for content, documents in chunk_rendered_documents(
                f"CIS Benchmarks - {family}",
                documents_by_family[family],
                max_tokens=max_tokens,
                max_chars=max_chars,
            ):
                prepared.append((family, content, documents))
    else:
        packaging_mode = "upload-safe"
        all_documents = documents_by_family.get("all-benchmarks", [])
        for content, documents in chunk_rendered_documents(
            "CIS Benchmarks Knowledge",
            all_documents,
            max_tokens=max_tokens,
            max_chars=max_chars,
        ):
            prepared.append(("all-benchmarks", content, documents))

    if len(prepared) > max_files:
        raise ValueError(
            f"Knowledge output requires {len(prepared)} files, exceeding max_files={max_files}."
        )

    family_indexes: dict[str, int] = defaultdict(int)
    total_files = len(prepared)
    for family, content, documents in track(prepared, description="Writing bundles"):
        family_indexes[family] += 1
        if packaging_mode == "upload-safe":
            filename = (
                f"cis-benchmarks-knowledge-{family_indexes[family]:02d}"
                f"-of-{total_files:02d}.md"
            )
        elif packaging_mode == "single-file":
            filename = "all-benchmarks-01.md"
        else:
            filename = f"{slugify(family)}-{family_indexes[family]:02d}.md"
        encoded = content.encode("utf-8")
        token_count = count_tokens(content)
        if not _within_limits(
            token_count,
            len(content),
            max_tokens=max_tokens,
            max_chars=max_chars,
        ):
            raise RuntimeError(f"Internal chunking error: {filename} exceeds configured limit")
        (output_dir / filename).write_bytes(encoded)
        stats.bundle_files += 1
        benchmark_entries = [
            {
                "benchmark_id": document.benchmark_id,
                "title": document.title,
                "version": document.version,
                "recommendation_start": document.recommendation_start,
                "recommendation_end": document.recommendation_end,
            }
            for document in documents
        ]
        stats.bundles.append(
            {
                "filename": filename,
                "tokens": token_count,
                "bytes": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "benchmarks": benchmark_entries,
            }
        )
        index_lines.append(
            f"- {filename}: {len(documents)} benchmark document(s), {token_count} tokens"
        )

    if skip_reasons:
        index_lines.extend(["", "Skipped files:"])
        for filename, reason in sorted(skip_reasons.items()):
            index_lines.append(f"- {filename}: {reason}")

    (output_dir / "README.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    instructions = """# Custom GPT Instructions

You are a CIS Benchmark advisory assistant. Base benchmark-specific answers only on the uploaded CIS Benchmark knowledge files and the user's question.

For every substantive answer, identify each source in this format:

- Document: <exact benchmark title and version>
- Recommendation: <reference> <title>
- Evidence section: <Description|Rationale|Audit|Remediation|Default Value|References>

Keep recommendations from different products or versions separate. Never merge them into an invented recommendation. Prefer original Audit and Remediation steps for technical guidance. Preserve commands and configuration values exactly enough to execute safely. Clearly label any operational interpretation as interpretation, not source text.

Use compact Profiles and CIS, NIST, and MITRE mappings only as applicability metadata. Do not treat mappings as remediation evidence. If uploaded files lack enough evidence, say so instead of guessing. State version differences and ambiguity explicitly.
"""
    (output_dir / "CUSTOM_GPT_INSTRUCTIONS.md").write_text(instructions, encoding="utf-8")
    build_manifest(
        output_dir,
        sorted_grouped,
        skip_reasons,
        stats,
        packaging_mode,
        manifest_context=manifest_context,
    )
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
        default=None,
        help="Optional secondary character limit per output Markdown file",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        help="Maximum tokens per output Markdown file",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=DEFAULT_MAX_FILES,
        help="Maximum number of Markdown knowledge files",
    )
    parser.add_argument(
        "--single-file",
        dest="single_file",
        action="store_true",
        default=None,
        help="Require one file; fails when content exceeds file limits",
    )
    parser.add_argument(
        "--family-bundles",
        dest="single_file",
        action="store_false",
        help="Package benchmarks into family-based bundles instead of one single bundle",
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
        max_tokens=args.max_tokens,
        max_files=args.max_files,
    )

    print(f"Processed {stats.processed_files} benchmark files")
    print(f"Skipped {stats.skipped_files} files that were incomplete or invalid")
    print(f"Wrote {stats.bundle_files} Markdown knowledge bundle(s) to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
