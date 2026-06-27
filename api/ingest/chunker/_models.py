"""Data models, constants, and shared helpers for chunk building."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from markitdown import MarkItDown

# ── Data models ───────────────────────────────────────────────────────────────


@dataclass
class ChunkL3:
    text: str
    position: int = 0


@dataclass
class ChunkL2:
    heading: str
    text: str  # full section text for context retrieval
    position: int = 0
    children: list[ChunkL3] = field(default_factory=list)


@dataclass
class ParsedDocument:
    title: str
    source_url: str
    raw_text: str  # full plain text for LLM summarisation
    content_hash: str  # MD5 of raw_text; detects content changes at any L2 level
    sections: list[ChunkL2] = field(default_factory=list)


# ── Constants ─────────────────────────────────────────────────────────────────

_INTRO_HEADING = "__intro__"  # heading for text appearing before the first section
_MARKITDOWN = MarkItDown()  # reuse single instance (thread-safe, no internal state)

_BINARY_CONTENT_TYPES: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}
_BINARY_EXTENSIONS: frozenset[str] = frozenset(_BINARY_CONTENT_TYPES.values())


# ── Shared helpers ────────────────────────────────────────────────────────────


def _build_l3_chunks(text: str, max_chars: int = 500, overlap: int = 100) -> list[ChunkL3]:
    """Split section text into overlapping L3 paragraph chunks."""
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    chunks: list[ChunkL3] = []
    buf = ""
    pos = 0
    for para in paragraphs:
        candidate = f"{buf}\n\n{para}".strip() if buf else para
        if len(candidate) <= max_chars:
            buf = candidate
        else:
            if buf:
                chunks.append(ChunkL3(text=buf, position=pos))
                pos += 1
                buf = buf[-overlap:] + "\n\n" + para if len(buf) > overlap else para
            else:
                chunks.append(ChunkL3(text=para, position=pos))
                pos += 1
                buf = ""
    if buf:
        chunks.append(ChunkL3(text=buf, position=pos))
    return chunks or [ChunkL3(text=text.strip(), position=0)]


def _sections_to_document(
    title: str,
    source_url: str,
    raw_sections: list[tuple[str, str]],
) -> ParsedDocument:
    """Convert (heading, text) pairs into a ParsedDocument with L2/L3 chunks."""
    sections = [
        ChunkL2(heading=heading, text=text, position=pos, children=_build_l3_chunks(text))
        for pos, (heading, text) in enumerate((h, t) for h, t in raw_sections if t.strip())
    ]

    raw_text = "\n\n".join(t for _, t in raw_sections if t.strip())
    content_hash = hashlib.md5(raw_text.encode("utf-8")).hexdigest()
    return ParsedDocument(
        title=title,
        source_url=source_url,
        raw_text=raw_text,
        content_hash=content_hash,
        sections=sections,
    )


def _split_by_heading(text: str, pattern: str) -> list[tuple[str, str]]:
    """Split text into (heading, body) pairs using a regex heading pattern."""
    lines = text.splitlines()
    sections: list[tuple[str, str]] = []
    current_heading = _INTRO_HEADING
    current_lines: list[str] = []

    for line in lines:
        m = re.match(pattern, line, re.MULTILINE)
        if m:
            if current_lines:
                sections.append((current_heading, "\n".join(current_lines).strip()))
            current_heading = m.group(1).strip()
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        sections.append((current_heading, "\n".join(current_lines).strip()))
    return sections
