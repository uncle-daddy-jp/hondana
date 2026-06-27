"""Plain text parser: triple-newline blocks become L2 sections."""

from __future__ import annotations

import re
from pathlib import Path

from ._models import (
    ParsedDocument,
    _INTRO_HEADING,
    _sections_to_document,
)


def _parse_txt(path: Path) -> ParsedDocument:
    """Parse plain text; triple-newline blocks become L2 sections."""
    text = path.read_text(encoding="utf-8", errors="replace")
    blocks = [b.strip() for b in re.split(r"\n{3,}", text) if b.strip()]
    raw_sections = [(f"Block {i + 1}", block) for i, block in enumerate(blocks)]
    if not raw_sections:
        raw_sections = [(_INTRO_HEADING, text.strip())]
    return _sections_to_document(path.stem, "", raw_sections)
