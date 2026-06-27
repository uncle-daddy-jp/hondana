"""Reddit post + all comments fetcher via PRAW.

Handles: https://www.reddit.com/r/<sub>/comments/<id>/...
         https://old.reddit.com/r/<sub>/comments/<id>/...

Environment variables:
  REDDIT_CLIENT_ID     — Reddit app client ID (required)
  REDDIT_CLIENT_SECRET — Reddit app client secret (required)
  REDDIT_USER_AGENT    — User-agent string (default: hondana:ingest:1.0)
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._models import ParsedDocument

logger = logging.getLogger(__name__)

_REDDIT_URL_RE = re.compile(
    r"https?://(?:www\.|old\.)?reddit\.com/r/[^/]+/comments/[a-zA-Z0-9_]+",
    re.IGNORECASE,
)

_REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID", "")
_REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")
_REDDIT_USER_AGENT = os.environ.get("REDDIT_USER_AGENT", "hondana:ingest:1.0")


def is_reddit_url(url: str) -> bool:
    return bool(_REDDIT_URL_RE.match(url))


def is_reddit_configured() -> bool:
    return bool(_REDDIT_CLIENT_ID and _REDDIT_CLIENT_SECRET)


def chunker_fetch_reddit(url: str) -> ParsedDocument:
    """Fetch a Reddit post and all its comments via PRAW.

    replace_more(limit=None) expands every "load more comments" stub so the
    full comment tree is retrieved before building the document.
    """
    try:
        import praw
        import praw.models
    except ImportError as exc:
        raise ValueError("praw is not installed; add it to requirements.txt") from exc

    from ._models import _sections_to_document

    reddit = praw.Reddit(
        client_id=_REDDIT_CLIENT_ID,
        client_secret=_REDDIT_CLIENT_SECRET,
        user_agent=_REDDIT_USER_AGENT,
    )

    submission = reddit.submission(url=url)

    # Expand every "load more comments" stub — fetches the full comment tree
    submission.comments.replace_more(limit=None)

    subreddit_name = submission.subreddit.display_name
    author = str(submission.author) if submission.author else "[deleted]"

    logger.info(
        "Reddit: r/%s — %d top-level comments",
        subreddit_name,
        len(submission.comments),
    )

    # __intro__: post metadata + body (self-post) or destination link
    intro_parts = [f"r/{subreddit_name} | u/{author} | score: {submission.score}"]
    if submission.is_self and submission.selftext:
        intro_parts += ["", submission.selftext]
    elif not submission.is_self:
        intro_parts += ["", f"Link: {submission.url}"]

    raw_sections: list[tuple[str, str]] = [("__intro__", "\n".join(intro_parts))]

    # One L2 section per top-level comment; replies rendered as indented text
    for comment in submission.comments:
        text = _render_comment_tree(comment, depth=0)
        if not text.strip():
            continue
        comment_author = str(comment.author) if comment.author else "[deleted]"
        heading = f"u/{comment_author} (score: {comment.score})"
        raw_sections.append((heading, text))

    return _sections_to_document(
        title=submission.title,
        source_url=url,
        raw_sections=raw_sections,
    )


def _render_comment_tree(comment, depth: int = 0) -> str:
    """Render a comment and all nested replies as indented plain text."""
    try:
        import praw.models

        if isinstance(comment, praw.models.MoreComments):
            return ""
    except ImportError:
        return ""

    indent = "  " * depth
    author = str(comment.author) if comment.author else "[deleted]"
    lines: list[str] = []

    if depth > 0:
        lines.append(f"{indent}u/{author} (score: {comment.score}):")

    for line in (comment.body or "").splitlines():
        lines.append(f"{indent}{line}" if line else "")

    for reply in comment.replies:
        child = _render_comment_tree(reply, depth + 1)
        if child.strip():
            lines.append("")
            lines.append(child)

    return "\n".join(lines)
