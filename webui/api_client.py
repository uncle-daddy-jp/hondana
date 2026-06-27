"""api_client.py — HONDANA API 呼び出しラッパ。"""
from __future__ import annotations

import httpx

from utils import API_URL, _headers


def _api_get(path: str) -> dict:
    r = httpx.get(f"{API_URL}{path}", headers=_headers(), timeout=60)
    r.raise_for_status()
    return r.json()


def _api_post(path: str, body: dict | None = None) -> dict:
    r = httpx.post(f"{API_URL}{path}", json=body or {}, headers=_headers(), timeout=None)
    r.raise_for_status()
    return r.json()


def _api_patch(path: str, body: dict | None = None) -> dict:
    r = httpx.patch(f"{API_URL}{path}", json=body or {}, headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def _api_delete(path: str) -> dict:
    r = httpx.delete(f"{API_URL}{path}", headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def _api_upload(path: str, filename: str, content: bytes, fields: dict | None = None) -> dict:
    files = {"file": (filename, content)}
    data = fields or {}
    r = httpx.post(f"{API_URL}{path}", files=files, data=data, headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()
