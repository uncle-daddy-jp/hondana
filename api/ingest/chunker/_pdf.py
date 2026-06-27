"""PDF parser using pdfplumber with font-size based heading detection."""

from __future__ import annotations

from pathlib import Path

import pdfplumber

from ._models import (
    ParsedDocument,
    _INTRO_HEADING,
    _sections_to_document,
)


def _parse_pdf(path: Path) -> ParsedDocument:
    """Parse PDF using pdfplumber; estimate section boundaries by font size."""
    title = path.stem
    source_url = ""
    raw_sections: list[tuple[str, str]] = []
    current_heading = _INTRO_HEADING
    current_lines: list[str] = []

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            words = page.extract_words(extra_attrs=["size"])
            if not words:
                continue
            sizes = sorted({round(w.get("size", 0)) for w in words}, reverse=True)
            heading_threshold = sizes[max(0, len(sizes) // 10)] if sizes else 0

            line_buf: dict[float, list] = {}
            for w in words:
                line_buf.setdefault(round(w["top"], 1), []).append(w)

            for y in sorted(line_buf):
                line_words = line_buf[y]
                text = " ".join(w["text"] for w in line_words)
                avg_size = sum(w.get("size", 0) for w in line_words) / len(line_words)
                if avg_size >= heading_threshold and len(text) < 120:
                    if current_lines:
                        raw_sections.append((current_heading, "\n".join(current_lines)))
                    current_heading = text
                    current_lines = []
                else:
                    current_lines.append(text)

    if current_lines:
        raw_sections.append((current_heading, "\n".join(current_lines)))
    return _sections_to_document(title, source_url, raw_sections)
