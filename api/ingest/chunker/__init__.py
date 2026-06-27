"""Format-aware parser and hierarchical chunk builder.

Formats:
  .md        — self-parse: YAML frontmatter → title/url, ## headings → L2
  .html/.htm — trafilatura: noise removal + H2/H3 structure via XML output
  .pdf       — pdfplumber: font-size based heading detection
  .docx      — python-docx: Heading 2 style → L2
  .pptx      — python-pptx: one slide per L2
  .txt       — paragraph blocks as L2
  .xlsx      — MarkItDown: one sheet per L2

Public entry points:
  chunker_parse(file_path)               — file-based dispatch
  chunker_parse_text(title, text, url)   — plain text with optional source_url
  chunker_fetch_url(url)                 — low-level fetch (HTML or binary)

H1-level text (before first heading) → heading="__intro__" in all formats.
content_hash = MD5 of full raw_text; stored on L1 to detect any L2-level changes.
"""

from ._dispatch import chunker_parse, chunker_parse_text
from ._fetch import chunker_fetch_url
from ._models import ChunkL2, ChunkL3, ParsedDocument, _build_l3_chunks

__all__ = [
    "chunker_parse",
    "chunker_parse_text",
    "chunker_fetch_url",
    "ParsedDocument",
    "ChunkL2",
    "ChunkL3",
    "_build_l3_chunks",
]
