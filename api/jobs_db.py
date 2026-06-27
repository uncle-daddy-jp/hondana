"""
jobs_db.py — SQLite persistence layer for the job queue.

SQLite is the single source of truth for all job state.
All job types (URL, text, file) are persisted.

Public API (jdb_ prefix):
  jdb_init(data_dir, max_retries)        — open/create DB, set WAL mode
  jdb_insert(job_id, job_type, payload) — persist a new pending job
  jdb_update_status(job_id, **kwargs)   — update status, timestamps, error, retry_count
  jdb_claim_next()                      — atomicに pending を1件取得して running に更新
  jdb_get(job_id)                       — 1件取得
  jdb_list(limit)                       — 最新N件取得
  jdb_stats()                           — ステータス別カウント
  jdb_has_pending()                     — pending ジョブが存在するか確認
  jdb_reset_running()                   — 起動時に running → pending にリセット
  jdb_max_retries()                     — return configured max retry count
  jdb_reset_failed()                    — failed → pending にリセット（一括）
  jdb_reset_one_failed(job_id)          — failed → pending にリセット（1件）
  jdb_delete_failed()                   — failed ジョブを全削除
  jdb_delete_old_failed(days)           — X日以上前に失敗したジョブを削除
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

# ── Module state ──────────────────────────────────────────────────────────────

_conn: sqlite3.Connection | None = None
_max_retries: int = 3
_lock = threading.Lock()  # jdb_insert is called from thread pool; other fns from event loop


_DDL = """
CREATE TABLE IF NOT EXISTS persistent_jobs (
    job_id      TEXT PRIMARY KEY,
    job_type    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    payload     TEXT NOT NULL,
    result      TEXT,
    error       TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0
)
"""


def _row_to_dict(row: sqlite3.Row) -> dict:
    """sqlite3.Row → jobs API 互換の dict に変換。"""
    payload = row["payload"]
    result = row["result"]
    return {
        "id": row["job_id"],
        "type": row["job_type"],
        "status": row["status"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "result": json.loads(result) if result else None,
        "error": row["error"],
        "retry_count": row["retry_count"],
        "payload": json.loads(payload) if payload else {},
    }


# ── Public API ────────────────────────────────────────────────────────────────


def jdb_init(data_dir: Path, max_retries: int = 3) -> None:
    """Open (or create) the SQLite jobs DB and apply schema. Must be called at startup."""
    global _conn, _max_retries
    _max_retries = max_retries
    db_file = data_dir / "jobs.db"
    _conn = sqlite3.connect(str(db_file), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")  # safe for concurrent workers
    _conn.execute(_DDL)
    _conn.commit()


def jdb_insert(job_id: str, job_type: str, payload: dict) -> None:
    """Persist a new pending job."""
    if _conn is None:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        _conn.execute(
            "INSERT OR IGNORE INTO persistent_jobs (job_id, job_type, status, created_at, payload)"
            " VALUES (?, ?, 'pending', ?, ?)",
            (job_id, job_type, now, json.dumps(payload, ensure_ascii=False)),
        )
        _conn.commit()


def jdb_update_status(job_id: str, **kwargs) -> None:
    """
    Update one or more fields for a job.
    Accepted kwargs: status, started_at, finished_at, result, error, retry_count.
    """
    if _conn is None:
        return
    allowed = {"status", "started_at", "finished_at", "result", "error", "retry_count"}
    sets = {k: v for k, v in kwargs.items() if k in allowed}
    if not sets:
        return
    if "result" in sets and isinstance(sets["result"], dict):
        sets["result"] = json.dumps(sets["result"], ensure_ascii=False)
    clause = ", ".join(f"{k} = ?" for k in sets)
    values = list(sets.values()) + [job_id]
    with _lock:
        _conn.execute(f"UPDATE persistent_jobs SET {clause} WHERE job_id = ?", values)
        _conn.commit()


def jdb_claim_next() -> dict | None:
    """
    pending ジョブを1件取得し、atomicに running に更新して返す。
    なければ None。SELECT→UPDATE を同一トランザクション内で行うため複数ワーカーで競合しない。
    返却形式は _row_to_dict と同一（id, type, status, ...）。
    """
    if _conn is None:
        return None
    with _lock:
        row = _conn.execute(
            "SELECT * FROM persistent_jobs WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        now = datetime.now(timezone.utc).isoformat()
        _conn.execute(
            "UPDATE persistent_jobs SET status = 'running', started_at = ? WHERE job_id = ?",
            (now, row["job_id"]),
        )
        _conn.commit()
    d = _row_to_dict(row)
    d["status"] = "running"
    d["started_at"] = now
    return d


def jdb_get(job_id: str) -> dict | None:
    """1件取得。存在しなければ None。"""
    if _conn is None:
        return None
    row = _conn.execute("SELECT * FROM persistent_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return _row_to_dict(row) if row else None


def jdb_list(limit: int = 50, status: str | None = None) -> list[dict]:
    """最新N件を created_at 降順で取得。status を指定するとそのステータスのみ返す。"""
    if _conn is None:
        return []
    if status:
        rows = _conn.execute(
            "SELECT * FROM persistent_jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = _conn.execute("SELECT * FROM persistent_jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def jdb_stats() -> dict:
    """ステータス別カウントを返す。"""
    counts: dict[str, int] = {"pending": 0, "running": 0, "done": 0, "failed": 0}
    if _conn is None:
        counts["total"] = 0
        return counts
    rows = _conn.execute("SELECT status, COUNT(*) AS cnt FROM persistent_jobs GROUP BY status").fetchall()
    for r in rows:
        counts[r["status"]] = r["cnt"]
    counts["total"] = sum(counts.values())
    return counts


def jdb_has_pending_url(url: str) -> bool:
    """同じ URL の pending/running ジョブがキューに存在するか確認する。"""
    if _conn is None:
        return False
    row = _conn.execute(
        "SELECT 1 FROM persistent_jobs"
        " WHERE status IN ('pending', 'running')"
        "   AND json_extract(payload, '$.url') = ?"
        " LIMIT 1",
        (url,),
    ).fetchone()
    return row is not None


def jdb_has_pending() -> bool:
    """pending ジョブが1件以上存在するか確認する。起動時のイベント初期セットに使う。"""
    if _conn is None:
        return False
    row = _conn.execute("SELECT 1 FROM persistent_jobs WHERE status = 'pending' LIMIT 1").fetchone()
    return row is not None


def jdb_reset_running() -> None:
    """
    起動時に呼ぶ。前回コンテナ停止時に中断された running ジョブを pending に戻す。
    """
    if _conn is None:
        return
    with _lock:
        _conn.execute("UPDATE persistent_jobs SET status = 'pending', started_at = NULL" " WHERE status = 'running'")
        _conn.commit()


def jdb_max_retries() -> int:
    """Return configured max retry count."""
    return _max_retries


def jdb_reset_failed() -> list[dict]:
    """
    failed ジョブをすべて pending にリセットし、対象ジョブの一覧を返す。
    retry_count を 0 にリセットするため、max_retries 回の再試行が再度利用できる。
    """
    if _conn is None:
        return []
    with _lock:
        rows = _conn.execute("SELECT * FROM persistent_jobs WHERE status = 'failed'").fetchall()
        if not rows:
            return []
        ids = [r["job_id"] for r in rows]
        placeholders = ", ".join("?" * len(ids))
        _conn.execute(
            f"UPDATE persistent_jobs"
            f" SET status = 'pending', started_at = NULL, finished_at = NULL,"
            f"     error = NULL, retry_count = 0"
            f" WHERE job_id IN ({placeholders})",
            ids,
        )
        _conn.commit()
    return [_row_to_dict(r) for r in rows]


def jdb_reset_one_failed(job_id: str) -> bool:
    """指定した failed ジョブを pending にリセットする。対象がなければ False。"""
    if _conn is None:
        return False
    with _lock:
        cur = _conn.execute(
            "UPDATE persistent_jobs"
            " SET status = 'pending', started_at = NULL, finished_at = NULL,"
            "     error = NULL, retry_count = 0"
            " WHERE job_id = ? AND status = 'failed'",
            (job_id,),
        )
        _conn.commit()
    return cur.rowcount > 0


def jdb_delete_failed() -> int:
    """failed ジョブをすべて削除する。削除件数を返す。"""
    if _conn is None:
        return 0
    with _lock:
        cur = _conn.execute("DELETE FROM persistent_jobs WHERE status = 'failed'")
        _conn.commit()
    return cur.rowcount


def jdb_delete_old_failed(days: int) -> int:
    """finished_at が days 日以上前の failed ジョブを削除する。削除件数を返す。"""
    if _conn is None:
        return 0
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _lock:
        cur = _conn.execute(
            "DELETE FROM persistent_jobs WHERE status = 'failed' AND finished_at < ?",
            (cutoff,),
        )
        _conn.commit()
    return cur.rowcount
