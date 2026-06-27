"""HTML parser using trafilatura for noise removal and structural extraction."""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import trafilatura

from ._models import (
    ParsedDocument,
    _INTRO_HEADING,
    _sections_to_document,
)

logger = logging.getLogger(__name__)


def _parse_html(path: Path) -> ParsedDocument:
    """Parse HTML using trafilatura for noise removal (ads/nav/footer stripped)
    and XML output to preserve H2/H3 heading structure."""
    html = path.read_text(encoding="utf-8", errors="replace")
    return _parse_html_string(html, fallback_title=path.stem)


def _parse_html_string(html: str | bytes, fallback_title: str = "", fallback_url: str = "") -> ParsedDocument:
    """Core HTML parsing logic shared by file parser and URL fetcher."""
    metadata = trafilatura.extract_metadata(html)
    title = (metadata.title if metadata and metadata.title else None) or fallback_title
    source_url = (metadata.url if metadata and metadata.url else None) or fallback_url

    xml_str = trafilatura.extract(
        html,
        output_format="xml",
        include_links=False,
        include_images=False,
        favor_recall=True,
    )
    raw_sections = _parse_trafilatura_xml(xml_str) if xml_str else [(_INTRO_HEADING, "")]
    return _sections_to_document(title, source_url, raw_sections)


def _parse_trafilatura_xml(xml_str: str) -> list[tuple[str, str]]:
    """Parse trafilatura XML output into (heading, text) pairs."""
    xml_str = re.sub(r"<\?xml[^?]*\?>", "", xml_str).strip()
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return [(_INTRO_HEADING, xml_str)]

    main = root.find("main") or root
    sections: list[tuple[str, str]] = []
    current_heading = _INTRO_HEADING
    current_texts: list[str] = []

    for elem in list(main):
        tag = elem.tag if isinstance(elem.tag, str) else ""
        if tag == "head":
            if current_texts:
                sections.append((current_heading, "\n\n".join(current_texts)))
            current_heading = ("".join(elem.itertext())).strip() or _INTRO_HEADING
            current_texts = []
        elif tag == "list":
            items = [("".join(item.itertext())).strip() for item in elem.findall("item")]
            joined = "\n".join(f"- {item}" for item in items if item)
            if joined:
                current_texts.append(joined)
        elif tag in ("p", "lb"):
            text = ("".join(elem.itertext())).strip()
            if text:
                current_texts.append(text)
        elif tag == "table":
            rows = [
                " | ".join(("".join(cell.itertext())).strip() for cell in row.findall("cell"))
                for row in elem.findall("row")
            ]
            if rows:
                current_texts.append("\n".join(rows))

    if current_texts:
        sections.append((current_heading, "\n\n".join(current_texts)))
    return sections or [(_INTRO_HEADING, "")]
