"""
auth.py — API key authentication dependency.

Usage:
  app = FastAPI(dependencies=[Depends(auth_require_key)])

Behaviour:
  - If no api_keys are configured in config.yml → open access (setup mode).
  - If keys are configured, request must include one of:
      X-API-Key: <key>                  (direct header)
      Authorization: Bearer <key>       (static token — e.g. MCP clients)
  - The bookmarklet GET endpoint /clip and the permalink prefix /k/ are exempt
    (browsers cannot attach an auth header).
"""

from __future__ import annotations

from fastapi import Request, HTTPException

# Paths exempt from key validation
_AUTH_EXEMPT_PATHS = frozenset(
    [
        "/clip",  # ブックマークレット用 GET エンドポイント（ブラウザからヘッダー付与不可）
    ]
)

# パスプレフィックスで認証免除（動的セグメントを含むパス用）
_AUTH_EXEMPT_PREFIXES = ("/k/",)


def auth_require_key(request: Request) -> None:
    """FastAPI dependency — validates API key from X-API-Key or Authorization: Bearer."""
    path = request.url.path
    if path in _AUTH_EXEMPT_PATHS:
        return
    if any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES):
        return

    # Use state_ref for live updates (cfg on app.state is set once at startup)
    state = getattr(request.app.state, "state_ref", None)
    cfg = state.cfg if state is not None else getattr(request.app.state, "cfg", {})
    api_keys_cfg: list[dict] = cfg.get("api_keys", [])

    # No keys configured → open (backward-compatible / initial setup)
    if not api_keys_cfg:
        return

    valid_keys: set[str] = {entry["key"] for entry in api_keys_cfg if isinstance(entry, dict) and entry.get("key")}

    # Accept X-API-Key header (direct) or Authorization: Bearer <token> (OAuth)
    incoming = request.headers.get("X-API-Key", "")
    if not incoming:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            incoming = auth_header[len("Bearer ") :]

    if incoming not in valid_keys:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
