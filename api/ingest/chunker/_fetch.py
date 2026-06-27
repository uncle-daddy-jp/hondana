"""URL fetching with binary detection and Playwright fallback for JS SPAs."""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

import requests

from constants import MIN_EXTRACT_CHARS

from ._html import _parse_html_string
from ._models import (
    ParsedDocument,
    _BINARY_CONTENT_TYPES,
    _BINARY_EXTENSIONS,
)
from ._reddit import chunker_fetch_reddit, is_reddit_configured, is_reddit_url

logger = logging.getLogger(__name__)


# Playwright フォールバック: trafilatura が抽出できない JS SPA 向け
# インストール済みの場合のみ有効（未インストール時はサイレントにスキップ）
try:
    import playwright as _pw_check  # noqa: F401

    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False


def chunker_fetch_url(url: str) -> tuple[ParsedDocument | None, str | None, bytes | None]:
    """GET 1回でバイナリ判定とHTMLパースを行う（HEAD + GET の二重リクエストを解消）。

    フォールバック戦略:
      1. requests で通常フェッチ
      2. フェッチ失敗（403/bot block等）または抽出テキストが少ない（JS SPA）場合は Playwright を試みる

    Returns:
      (ParsedDocument, None, None)  — HTML
      (None, suffix, bytes)         — バイナリ（suffix は ".pdf" など）
    Raises:
      ValueError — フェッチ失敗時（Playwright も失敗した場合を含む）
    """
    # Reddit URLs: PRAW fetches post body + full comment tree reliably
    if is_reddit_url(url) and is_reddit_configured():
        try:
            return chunker_fetch_reddit(url), None, None
        except Exception as exc:
            logger.warning("PRAW fetch failed — falling through to HTML: %s", exc)

    html: str | bytes | None = None
    try:
        resp = requests.get(
            url,
            allow_redirects=True,
            timeout=(10, 30),
            headers={"User-Agent": "Mozilla/5.0 (compatible; HONDANA/1.0)"},
        )
        resp.raise_for_status()

        ct = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        suffix = _BINARY_CONTENT_TYPES.get(ct)
        if suffix is None:
            ext = Path(urlparse(url).path).suffix.lower()
            suffix = ext if ext in _BINARY_EXTENSIONS else None

        if suffix:
            return None, suffix, resp.content

        # resp.content (bytes) を渡す: trafilatura が <meta charset> から
        # 正しいエンコーディングを検出する。resp.text は Content-Type に
        # charset が無い場合に ISO-8859-1 をデフォルトにするため日本語が化ける。
        html = resp.content
    except requests.RequestException as exc:
        if not _PLAYWRIGHT_AVAILABLE:
            raise ValueError(f"Failed to fetch URL: {url}") from exc
        logger.warning("requests failed (%s) — trying Playwright: %s", type(exc).__name__, url)

    doc = _parse_html_string(html, fallback_url=url) if html is not None else None

    # Playwright フォールバック（2段目）:
    #   (1) requests 失敗時（403/bot block等）→ doc is None
    #   (2) フェッチ成功だが抽出テキストが少ない（JS SPA）→ len < threshold
    if _PLAYWRIGHT_AVAILABLE and (doc is None or len(doc.raw_text.strip()) < MIN_EXTRACT_CHARS):
        if doc is not None:
            logger.warning("trafilatura extracted <%d chars — trying Playwright: %s", MIN_EXTRACT_CHARS, url)
        try:
            rendered = _playwright_fetch(url)
            doc = _parse_html_string(rendered, fallback_url=url)
            logger.info("Playwright extracted %d chars", len(doc.raw_text))
        except Exception as pw_exc:
            logger.warning("Playwright fallback failed: %s", pw_exc)

    # Jina Reader フォールバック（3段目）:
    # Playwright 失敗または抽出テキストが依然少ない場合に試みる。
    # 認証不要・無料で SPA 以外の抽出網羅率を補完する。
    if doc is None or len(doc.raw_text.strip()) < MIN_EXTRACT_CHARS:
        try:
            doc = _jina_fetch(url)
            logger.info("Jina Reader extracted %d chars", len(doc.raw_text))
        except Exception as jina_exc:
            logger.warning("Jina Reader fallback failed: %s", jina_exc)

    if doc is None:
        raise ValueError(f"Failed to fetch URL: {url}")

    return doc, None, None


def _jina_fetch(url: str) -> ParsedDocument:
    """Fetch page text via Jina Reader (r.jina.ai) and return a ParsedDocument.

    Jina Reader converts any URL to clean markdown text without requiring auth.
    Used as a third fallback when requests+Playwright both fail or yield too little text.
    """
    from ._models import _sections_to_document, _split_by_heading

    jina_url = f"https://r.jina.ai/{url}"
    resp = requests.get(
        jina_url,
        timeout=(10, 30),
        headers={"User-Agent": "Mozilla/5.0 (compatible; HONDANA/1.0)"},
    )
    resp.raise_for_status()
    text = resp.text.strip()
    raw_sections = _split_by_heading(text, r"^#+\s+(.+)")
    return _sections_to_document(title="", source_url=url, raw_sections=raw_sections)


def _playwright_fetch(url: str) -> str:
    """Render a JavaScript SPA with headless Chromium and return the full HTML.

    networkidle を待機することで Vue/React 等の非同期レンダリング完了後の DOM を取得する。
    sync_playwright を使用（chunker 関数は asyncio.to_thread 経由で呼ばれる前提）。
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=30_000)
            return page.content()
        finally:
            browser.close()
