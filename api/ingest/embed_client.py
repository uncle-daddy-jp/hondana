"""embed_client.py — Embedding model construction + the low-level remote embeddings call.

Mirrors llm_client.py: embed_client_build(cfg) picks a local SentenceTransformer or a
remote OpenAI-compatible endpoint based on config. embed_texts() is the single home for
the POST /v1/embeddings transport, reused by the re-embed migration script.
"""

from __future__ import annotations

import logging

import numpy as np
import requests

logger = logging.getLogger(__name__)

# bge-m3 の最大コンテキストは 8192 トークン。日本語で ~1.5 chars/token として余裕を持たせた上限。
EMBED_MAX_CHARS = 8000


def embed_texts(
    base_url: str,
    model: str,
    texts: list[str],
    *,
    normalize: bool = True,
    max_chars: int | None = None,
    strip: bool = False,
    timeout: int = 120,
) -> np.ndarray:
    """POST texts to an OpenAI-compatible /v1/embeddings endpoint → (N, dim) float32 array.

    max_chars: truncate each text to this many chars (None = no truncation).
    strip:     True  → blank/whitespace-only texts become a single space (.strip() applied);
               False → only already-blank texts are replaced (non-blank text is sent verbatim).
    normalize: L2-normalize each row.
    """
    url = base_url.rstrip("/") + "/v1/embeddings"

    def _clean(t: str) -> str:
        if max_chars is not None and len(t) > max_chars:
            t = t[:max_chars]
        if strip:
            return t.strip() or " "
        return t if t.strip() else " "

    payload = [_clean(t) for t in texts]
    resp = requests.post(url, json={"model": model, "input": payload}, timeout=timeout)
    if not resp.ok:
        raise requests.HTTPError(
            f"{resp.status_code} Client Error for url: {resp.url} — {resp.text[:500]}",
            response=resp,
        )
    items = sorted(resp.json()["data"], key=lambda x: x["index"])
    arr = np.array([item["embedding"] for item in items], dtype=np.float32)
    if normalize:
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        arr = arr / np.clip(norms, 1e-12, None)
    return arr


class RemoteEmbedModel:
    """EmbedModel backed by a remote OpenAI-compatible embeddings endpoint (e.g. vLLM bge-m3)."""

    def __init__(self, model: str, base_url: str):
        self._model = model
        self._url = base_url
        self._dim = self._fetch_dim()

    def _fetch_dim(self) -> int:
        arr = embed_texts(self._url, self._model, ["dim-probe"], normalize=False, timeout=30)
        return int(arr.shape[1])

    def encode(self, sentences, *, normalize_embeddings: bool = False):
        texts = list(sentences) if not isinstance(sentences, list) else sentences
        return embed_texts(
            self._url,
            self._model,
            texts,
            normalize=normalize_embeddings,
            max_chars=EMBED_MAX_CHARS,
            strip=False,
            timeout=120,
        )

    def get_sentence_embedding_dimension(self) -> int:
        return self._dim


def embed_client_build(cfg: dict):
    """Return an EmbedModel: remote (embedding_url set) else local SentenceTransformer."""
    embedding_url = cfg.get("embedding_url")
    if embedding_url:
        model = cfg.get("embedding_model", "bge-m3")
        embed = RemoteEmbedModel(model, embedding_url)
        logger.info("Embedding: remote %s @ %s  dim=%d", model, embedding_url, embed.get_sentence_embedding_dimension())
        return embed

    from sentence_transformers import SentenceTransformer

    model = cfg.get("embedding_model", "intfloat/multilingual-e5-small")
    embed = SentenceTransformer(model)
    logger.info("Embedding: local %s  dim=%d", model, embed.get_sentence_embedding_dimension())
    return embed
