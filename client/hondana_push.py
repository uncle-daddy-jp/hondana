#!/usr/bin/env python3
"""
hondana-push — Lightweight HONDANA client for sending documents from any PC.

Usage:
  python hondana_push.py <file>               Send a file
  python hondana_push.py <url>               Send a URL
  python hondana_push.py watch <folder>      Watch folder daemon
  python hondana_push.py config              Show current config

Options:
  --server URL        HONDANA server URL (overrides config)
  --key KEY           API key (overrides config)
  --table TABLE       Target table name (overrides config)
  --action ACTION     duplicate_action: overwrite/skip/new (overrides config)

Config file: ~/.hondana-push.yml
Env vars:    HONDANA_SERVER_URL, HONDANA_API_KEY, HONDANA_TABLE
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# ── Constants ─────────────────────────────────────────────────────────────────

SUPPORTED_EXTENSIONS = {".md", ".html", ".htm", ".pdf", ".docx", ".pptx", ".txt", ".xlsx"}
CONFIG_PATH = Path.home() / ".hondana-push.yml"
DEFAULT_CONFIG = {
    "server_url":       "http://localhost:8200",
    "api_key":          "",
    "table":            "hondana_chunks",
    "duplicate_action": "overwrite",
    "watch_interval":   5,
    "watch_done_dir":   str(Path.home() / ".hondana-push" / "done"),
}


# ── Config ────────────────────────────────────────────────────────────────────

def _load_config(args: argparse.Namespace) -> dict:
    """Merge: defaults ← config file ← env vars ← CLI args."""
    cfg = dict(DEFAULT_CONFIG)

    # Config file (simple key: value YAML — no pyyaml dependency)
    if CONFIG_PATH.exists():
        for line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                key, _, val = line.partition(":")
                cfg[key.strip()] = val.strip().strip('"').strip("'")

    # Env vars
    if os.environ.get("HONDANA_SERVER_URL"):
        cfg["server_url"] = os.environ["HONDANA_SERVER_URL"]
    if os.environ.get("HONDANA_API_KEY"):
        cfg["api_key"] = os.environ["HONDANA_API_KEY"]
    if os.environ.get("HONDANA_TABLE"):
        cfg["table"] = os.environ["HONDANA_TABLE"]

    # CLI args
    if getattr(args, "server", None):
        cfg["server_url"] = args.server
    if getattr(args, "key", None):
        cfg["api_key"] = args.key
    if getattr(args, "table", None):
        cfg["table"] = args.table
    if getattr(args, "action", None):
        cfg["duplicate_action"] = args.action

    cfg["server_url"] = cfg["server_url"].rstrip("/")
    cfg["watch_interval"] = int(cfg.get("watch_interval", 5))
    cfg["watch_done_dir"] = str(Path(str(cfg["watch_done_dir"])).expanduser())
    return cfg


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _headers(api_key: str) -> dict:
    h = {"Accept": "application/json"}
    if api_key:
        h["X-API-Key"] = api_key
    return h


def _http_json(server_url: str, api_key: str, method: str, path: str, data: dict) -> dict:
    url = f"{server_url}{path}"
    body = json.dumps(data).encode()
    headers = {**_headers(api_key), "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def _http_upload(server_url: str, api_key: str, path: str, file_path: Path, fields: dict) -> dict:
    """Multipart file upload using stdlib only."""
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []

    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )

    filename = file_path.name
    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n".encode()
        + file_path.read_bytes()
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())

    body = b"".join(parts)
    headers = {
        **_headers(api_key),
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    url = f"{server_url}{path}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def _poll_job(server_url: str, api_key: str, job_id: str, interval: int = 2) -> dict:
    """Poll job status every `interval` seconds until done or error."""
    while True:
        job = _http_json(server_url, api_key, "GET", f"/api/jobs/{job_id}", {})
        status = job.get("status", "")
        if status == "done":
            return job
        if status == "error":
            return job
        print(f"  [{status}] job {job_id[:8]}...", flush=True)
        time.sleep(interval)


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_send_file(cfg: dict, file_path: Path) -> bool:
    """Send a single file to HONDANA. Returns True on success."""
    if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        print(f"[skip] unsupported format: {file_path.suffix}")
        return False

    print(f"[send] {file_path.name} ...", flush=True)
    try:
        result = _http_upload(
            cfg["server_url"], cfg["api_key"],
            "/api/ingest/file",
            file_path,
            {
                "duplicate_action": cfg["duplicate_action"],
                "table":            cfg["table"],
            },
        )
    except urllib.error.HTTPError as e:
        print(f"[error] HTTP {e.code}: {e.read().decode()}")
        return False
    except Exception as e:
        print(f"[error] {e}")
        return False

    job_id = result.get("job_id")
    if not job_id:
        print(f"[error] no job_id in response: {result}")
        return False

    job = _poll_job(cfg["server_url"], cfg["api_key"], job_id)
    if job["status"] == "done":
        r = job.get("result", {})
        print(f"[done]  {r.get('title', file_path.name)!r} — {r.get('status', '?')}")
        return True
    else:
        print(f"[error] {job.get('error', 'unknown error')}")
        return False


def cmd_send_url(cfg: dict, url: str) -> bool:
    """Send a URL to HONDANA. Returns True on success."""
    print(f"[send] {url} ...", flush=True)
    try:
        result = _http_json(
            cfg["server_url"], cfg["api_key"],
            "POST", "/api/ingest/url",
            {
                "url":              url,
                "duplicate_action": cfg["duplicate_action"],
                "table":            cfg["table"],
            },
        )
    except urllib.error.HTTPError as e:
        print(f"[error] HTTP {e.code}: {e.read().decode()}")
        return False
    except Exception as e:
        print(f"[error] {e}")
        return False

    job_id = result.get("job_id")
    if not job_id:
        print(f"[error] no job_id in response: {result}")
        return False

    job = _poll_job(cfg["server_url"], cfg["api_key"], job_id)
    if job["status"] == "done":
        r = job.get("result", {})
        print(f"[done]  {r.get('title', url)!r} — {r.get('status', '?')}")
        return True
    else:
        print(f"[error] {job.get('error', 'unknown error')}")
        return False


def cmd_watch(cfg: dict, folder: Path) -> None:
    """Watch a folder and send new files to HONDANA automatically."""
    done_dir = Path(cfg["watch_done_dir"])
    done_dir.mkdir(parents=True, exist_ok=True)
    interval = cfg["watch_interval"]

    print(f"[watch] {folder}  →  {cfg['server_url']} (table: {cfg['table']})")
    print(f"[watch] done_dir: {done_dir}  interval: {interval}s")
    print("[watch] Ctrl+C to stop\n", flush=True)

    while True:
        for f in sorted(folder.iterdir()):
            if not f.is_file():
                continue
            if f.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            try:
                if f.parent.resolve() == done_dir.resolve():
                    continue
            except Exception:
                pass

            ok = cmd_send_file(cfg, f)
            if ok:
                dest = done_dir / f.name
                if dest.exists():
                    ts = time.strftime("%Y%m%d%H%M%S")
                    dest = done_dir / f"{f.stem}_{ts}{f.suffix}"
                shutil.move(str(f), str(dest))

        time.sleep(interval)


def cmd_config(cfg: dict) -> None:
    """Print the effective configuration."""
    print("Effective configuration:")
    print(f"  config file:      {CONFIG_PATH} ({'found' if CONFIG_PATH.exists() else 'not found'})")
    for key, val in cfg.items():
        display = "***" if key == "api_key" and val else (val or "(not set)")
        print(f"  {key:<22} {display}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="hondana-push — send documents to HONDANA from any PC",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python hondana_push.py article.md
  python hondana_push.py https://example.com/article
  python hondana_push.py --table work report.pdf
  python hondana_push.py watch ~/Desktop/inbox
  python hondana_push.py config
        """,
    )
    parser.add_argument("--server", metavar="URL",    help="HONDANA server URL")
    parser.add_argument("--key",    metavar="KEY",    help="API key")
    parser.add_argument("--table",  metavar="TABLE",  help="Target table name")
    parser.add_argument("--action", metavar="ACTION", help="duplicate_action: overwrite/skip/new")
    parser.add_argument("args", nargs="*", help="command [args] or file/URL")

    parsed = parser.parse_args()
    cfg = _load_config(parsed)
    positionals: list[str] = parsed.args

    if not positionals:
        parser.print_help()
        return

    command = positionals[0]

    if command == "config":
        cmd_config(cfg)
        return

    if command == "watch":
        if len(positionals) < 2:
            print("[error] watch requires a folder argument")
            sys.exit(1)
        folder = Path(positionals[1]).expanduser().resolve()
        if not folder.is_dir():
            print(f"[error] not a directory: {folder}")
            sys.exit(1)
        try:
            cmd_watch(cfg, folder)
        except KeyboardInterrupt:
            print("\n[watch] stopped.")
        return

    target = command
    if target.startswith("http://") or target.startswith("https://"):
        ok = cmd_send_url(cfg, target)
    else:
        path = Path(target).expanduser().resolve()
        if not path.exists():
            print(f"[error] file not found: {path}")
            sys.exit(1)
        ok = cmd_send_file(cfg, path)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
