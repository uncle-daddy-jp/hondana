"""Public dispatch entry points: file-path, URL, and raw-text parsing."""

from __future__ import annotations

import re
from pathlib import Path

from ._html import _parse_html
from ._md import _parse_md
from ._models import (
    ParsedDocument,
    _INTRO_HEADING,
    _sections_to_document,
    _split_by_heading,
)
from ._office import _parse_docx, _parse_pptx, _parse_xlsx
from ._pdf import _parse_pdf
from ._text import _parse_txt


def chunker_parse(file_path: Path) -> ParsedDocument:
    """Dispatch to the appropriate parser based on file extension."""
    parsers = {
        ".md": _parse_md,
        ".html": _parse_html,
        ".htm": _parse_html,
        ".pdf": _parse_pdf,
        ".docx": _parse_docx,
        ".pptx": _parse_pptx,
        ".txt": _parse_txt,
        ".xlsx": _parse_xlsx,
    }
    parser = parsers.get(file_path.suffix.lower())
    if parser is None:
        raise ValueError(f"Unsupported file format: {file_path.suffix}")
    return parser(file_path)


def chunker_parse_text(title: str, text: str, source_url: str = "") -> ParsedDocument:
    """Parse plain text with ## headings as L2 sections."""
    text = re.sub(r"\n{3,}", "\n\n", text)
    raw_sections = _split_by_heading(text, pattern=r"^##\s+(.+)$")
    if not raw_sections or all(not t.strip() for _, t in raw_sections):
        raw_sections = [(_INTRO_HEADING, text.strip())]
    return _sections_to_document(title, source_url, raw_sections)
