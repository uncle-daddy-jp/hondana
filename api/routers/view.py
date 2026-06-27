"""routers/view.py — Permalink HTML view for individual knowledge articles."""

from __future__ import annotations

import html
import logging

import markdown as _md_lib

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from manager import manager_find_article_chunks

logger = logging.getLogger(__name__)

router = APIRouter(tags=["view"])

_PAGE_CSS = """
:root {
  --bg: #fafaf8;
  --fg: #1a1a1a;
  --muted: #666;
  --accent: #2563eb;
  --border: #e5e5e0;
  --tag-bg: #f0f0ec;
  --summary-bg: #f5f3f0;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: var(--bg);
  color: var(--fg);
  line-height: 1.75;
  font-size: 16px;
}
.container { max-width: 720px; margin: 0 auto; padding: 2rem 1.5rem 5rem; }
.breadcrumb { font-size: 0.78rem; color: var(--muted); margin-bottom: 1.5rem; }
h1 { font-size: 1.6rem; font-weight: 700; line-height: 1.3; margin-bottom: 1rem; }
.meta {
  font-size: 0.85rem;
  color: var(--muted);
  margin-bottom: 1rem;
  display: flex;
  flex-wrap: wrap;
  gap: 0.6rem 1.25rem;
  align-items: baseline;
}
.meta a { color: var(--accent); text-decoration: none; word-break: break-all; }
.meta a:hover { text-decoration: underline; }
.tags { margin-bottom: 1.25rem; display: flex; flex-wrap: wrap; gap: 0.4rem; }
.tag {
  font-size: 0.72rem;
  background: var(--tag-bg);
  border: 1px solid var(--border);
  border-radius: 9999px;
  padding: 0.15rem 0.65rem;
  color: var(--muted);
}
.divider { border: none; border-top: 1px solid var(--border); margin: 1.5rem 0; }
.summary-box {
  background: var(--summary-bg);
  border-left: 3px solid var(--accent);
  padding: 0.75rem 1rem;
  margin-bottom: 2rem;
  border-radius: 0 4px 4px 0;
  font-size: 0.9rem;
  color: #333;
}
section { margin-bottom: 2rem; }
h2 {
  font-size: 1.05rem;
  font-weight: 600;
  color: var(--fg);
  margin-bottom: 0.6rem;
  padding-bottom: 0.3rem;
  border-bottom: 1px solid var(--border);
}
p {
  margin-bottom: 0.6rem;
  word-break: break-word;
}
ul, ol { padding-left: 1.5rem; margin-bottom: 0.6rem; }
li { margin-bottom: 0.2rem; }
code {
  background: #f0f0ec;
  padding: 0.1em 0.3em;
  border-radius: 3px;
  font-size: 0.88em;
  font-family: monospace;
}
pre {
  background: #f0f0ec;
  padding: 0.75rem 1rem;
  border-radius: 4px;
  overflow-x: auto;
  margin-bottom: 0.75rem;
}
pre code { background: none; padding: 0; }
blockquote {
  border-left: 3px solid var(--border);
  padding: 0.5rem 1rem;
  color: var(--muted);
  margin-bottom: 0.75rem;
}
.prose a { color: var(--accent); }
table { border-collapse: collapse; width: 100%; margin-bottom: 0.75rem; }
th, td { border: 1px solid var(--border); padding: 0.4rem 0.6rem; text-align: left; }
th { background: var(--tag-bg); font-weight: 600; }
.footer {
  margin-top: 3rem;
  font-size: 0.72rem;
  color: var(--muted);
  border-top: 1px solid var(--border);
  padding-top: 0.75rem;
  font-family: monospace;
}
"""


def _md(text: str) -> str:
    return _md_lib.markdown(text, extensions=["nl2br", "fenced_code", "tables"])


def _find_article(article_id: str) -> tuple[list[dict], str] | tuple[None, None]:
    chunks, table = manager_find_article_chunks(article_id)
    return (chunks, table) if chunks else (None, None)


def _render_article(chunks: list[dict], table_name: str) -> str:
    l1 = next((c for c in chunks if c.get("level") == 1), None)
    l2_chunks = [c for c in chunks if c.get("level") == 2]
    l3_chunks = [c for c in chunks if c.get("level") == 3]

    raw_title = (l1 or {}).get("title") or "（タイトルなし）"
    title = html.escape(raw_title)
    source_url = (l1 or {}).get("source_url", "")
    tags: list[str] = (l1 or {}).get("tags") or []
    recorded_at = ((l1 or {}).get("recorded_at") or "")[:10]
    summary_text = (l1 or {}).get("text", "")
    article_id = (l1 or chunks[0]).get("article_id", "")

    source_html = (
        f'<a href="{html.escape(source_url)}" target="_blank" rel="noopener">'
        f'{html.escape(source_url[:80])}{"…" if len(source_url) > 80 else ""}</a>'
        if source_url
        else "ソースURLなし"
    )
    tags_html = "".join(f'<span class="tag">{html.escape(t)}</span>' for t in tags)

    # Build L3 index keyed by parent_id (L2 chunk id)
    l3_by_parent: dict[str, list[dict]] = {}
    for c in l3_chunks:
        l3_by_parent.setdefault(c.get("parent_id", ""), []).append(c)

    sections_html = ""
    for l2 in l2_chunks:
        heading = html.escape(l2.get("heading") or l2.get("text", "")[:60] or "—")
        children = l3_by_parent.get(l2.get("id", ""), [])
        if children:
            paras = "\n".join(f'<div class="prose">{_md(c.get("text", ""))}</div>' for c in children)
        else:
            paras = f'<div class="prose">{_md(l2.get("text", ""))}</div>' if l2.get("text") else ""
        sections_html += f"\n<section>\n  <h2>{heading}</h2>\n  {paras}\n</section>"

    body_html = ""
    if summary_text:
        body_html += f'<div class="summary-box">{_md(summary_text)}</div>\n'
    if sections_html:
        body_html += sections_html
    elif not summary_text:
        body_html = "<p>（コンテンツなし）</p>"

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — HONDANA</title>
<style>{_PAGE_CSS}</style>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
</head>
<body>
<div class="container">
  <div class="breadcrumb">HONDANA / {html.escape(table_name)}</div>
  <h1>{title}</h1>
  <div class="meta">
    <span>📅 {html.escape(recorded_at)}</span>
    <span>📦 {html.escape(table_name)}</span>
    <span>🔗 {source_html}</span>
  </div>
  {f'<div class="tags">{tags_html}</div>' if tags_html else ''}
  <hr class="divider">
  {body_html}
  <div class="footer">article_id: {html.escape(article_id)}</div>
</div>
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js"></script>
<script>
renderMathInElement(document.body, {{
  delimiters: [
    {{left: '$$', right: '$$', display: true}},
    {{left: '$',  right: '$',  display: false}}
  ],
  throwOnError: false
}});
</script>
</body>
</html>"""


_NOT_FOUND_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>404 — HONDANA</title>
<style>
body {{ font-family: sans-serif; text-align: center; padding: 4rem; color: #666; }}
h1 {{ font-size: 3rem; margin-bottom: 1rem; }}
</style>
</head>
<body>
<h1>404</h1>
<p>記事が見つかりません。</p>
<p style="font-size:0.8rem;margin-top:1rem;font-family:monospace">{article_id}</p>
</body>
</html>"""


@router.get("/k/{article_id}", response_class=HTMLResponse)
def view_article(article_id: str):
    """パーマリンク — article_id でナレッジを HTML で表示する。認証不要。"""
    chunks, table_name = _find_article(article_id)
    if chunks is None:
        return HTMLResponse(
            content=_NOT_FOUND_HTML.format(article_id=html.escape(article_id)),
            status_code=404,
        )
    return HTMLResponse(content=_render_article(chunks, table_name))
