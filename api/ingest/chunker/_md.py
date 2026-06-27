"""Markdown parser with YAML frontmatter and H2 section splitting."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from ._models import (
    ParsedDocument,
    _sections_to_document,
    _split_by_heading,
)

logger = logging.getLogger(__name__)


def _parse_md(path: Path) -> ParsedDocument:
    """Parse Obsidian-style Markdown. Reads title/source_url from YAML frontmatter,
    strips frontmatter from body, then splits on ## headings."""
    content = path.read_text(encoding="utf-8")
    title = path.stem
    source_url = ""

    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            try:
                fm = yaml.safe_load(content[3:end].strip()) or {}
                title = str(fm.get("title", title))
                source_url = str(fm.get("source", ""))
            except yaml.YAMLError as exc:
                logger.warning("YAML frontmatter parse failed: %s", exc)
            content = content[end + 3 :].strip()

    content = re.sub(r"!\[.*?\]\(.*?\)", "", content)  # strip image embeds
    content = re.sub(r"\n{3,}", "\n\n", content)
    raw_sections = _split_by_heading(content, pattern=r"^##\s+(.+)$")
    return _sections_to_document(title, source_url, raw_sections)
