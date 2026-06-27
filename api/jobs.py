"""
jobs.py — Async job queue backed by SQLite with event-driven workers and retry.

Job lifecycle: pending → running → done | failed

SQLite is the single source of truth for all job state.
Workers sleep on asyncio.Event and wake only when a new job arrives.
No polling — jobs_enqueue() rings the doorbell (event.set()); workers drain
SQLite until empty, then go back to sleep.

Public API:
  jobs_init_event()                — initialise asyncio.Event (call in lifespan)
  jobs_enqueue(job_type, payload)  → job_id (str)
  jobs_get(job_id)                 → job dict | None
  jobs_list(limit)                 → list[dict]
  jobs_stats()                     → counts per status
  jobs_run_worker(handler)         → coroutine (run as asyncio.Task in lifespan)
  jobs_retry_failed()              → reset all failed jobs to pending, return job_ids
  jobs_retry_one(job_id)           → reset single failed job to pending, return bool
  jobs_clear_failed()              → delete all failed jobs, return count
  jobs_run_cleanup_worker(days)    → coroutine: 毎日1回 days 日以上前の failed ジョブを削除
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _cleanup_temp_file(job_type: str, payload: dict) -> None:
    """ingest_file ジョブの一時ファイルをジョブ終了時に削除する。"""
    if job_type == "ingest_file":
        tmp = payload.get("tmp_path")
        if tmp:
            try:
                Path(tmp).unlink(missing_ok=True)
            except Exception:
                pass


from constants import RATE_LIMIT_PAUSE_SECONDS, JOB_CLEANUP_INTERVAL
from ingest.llm_client import LLMRateLimitError

from jobs_db import (
    jdb_claim_next,
    jdb_delete_failed,
    jdb_delete_old_failed,
    jdb_get,
    jdb_insert,
    jdb_list,
    jdb_max_retries,
    jdb_reset_failed,
    jdb_reset_one_failed,
    jdb_stats,
    jdb_update_status,
)

# ── Module state ──────────────────────────────────────────────────────────────

_new_job_event: asyncio.Event | None = None


# ── Public API ────────────────────────────────────────────────────────────────


def jobs_init_event() -> None:
    """asyncio.Event を初期化する。lifespan の asyncio ループ起動後に呼ぶこと。"""
    global _new_job_event
    _new_job_event = asyncio.Event()


def jobs_notify() -> None:
    """ワーカーにジョブ存在を通知する。起動時に pending が残っている場合などに使う。"""
    if _new_job_event is not None:
        _new_job_event.set()


def jobs_enqueue(job_type: str, payload: dict) -> str:
    """Add a job to the queue. Returns job_id immediately."""
    job_id = str(uuid.uuid4())
    jdb_insert(job_id, job_type, payload)
    if _new_job_event is not None:
        _new_job_event.set()
    return job_id


def jobs_get(job_id: str) -> dict | None:
    return jdb_get(job_id)


def jobs_list(limit: int = 50, status: str | None = None) -> list[dict]:
    return jdb_list(limit, status=status)


def jobs_stats() -> dict:
    return jdb_stats()


async def jobs_run_worker(handler) -> None:
    """
    Event-driven worker: sleeps until jobs_enqueue() sets the event,
    then drains SQLite until no pending jobs remain.
    handler(job_type, payload) must be an async callable returning a serialisable dict.
    Multiple instances can run concurrently; jdb_claim_next() is atomic.
    """
    while True:
        await _new_job_event.wait()
        _new_job_event.clear()

        while True:
            item = jdb_claim_next()
            if item is None:
                break

            job_id = item["id"]
            job_type = item["type"]
            payload = item["payload"]
            retry_count = item["retry_count"]

            try:
                result = await handler(job_type, payload)
                finished = datetime.now(timezone.utc).isoformat()
                jdb_update_status(job_id, status="done", finished_at=finished, result=result)
                _cleanup_temp_file(job_type, payload)

            except LLMRateLimitError:
                logger.warning(
                    "RATE-LIMITED job=%s type=%s — pausing worker %ds", job_id, job_type, RATE_LIMIT_PAUSE_SECONDS
                )
                jdb_update_status(job_id, status="pending", started_at=None)
                await asyncio.sleep(RATE_LIMIT_PAUSE_SECONDS)
                _new_job_event.set()

            except asyncio.CancelledError:
                # シャットダウン時にタスクがキャンセルされた場合、ジョブを pending に戻して再起動後に再試行できるようにする
                jdb_update_status(job_id, status="pending", started_at=None)
                raise

            except Exception as exc:
                retry_count += 1
                logger.error("ERROR job=%s type=%s attempt=%d: %s", job_id, job_type, retry_count, exc)

                if retry_count <= jdb_max_retries():
                    jdb_update_status(job_id, status="pending", started_at=None, retry_count=retry_count)
                    _new_job_event.set()
                else:
                    finished = datetime.now(timezone.utc).isoformat()
                    jdb_update_status(
                        job_id,
                        status="failed",
                        finished_at=finished,
                        error=str(exc),
                        retry_count=retry_count,
                    )
                    _cleanup_temp_file(job_type, payload)
                    logger.error("FAILED job=%s type=%s after %d attempts", job_id, job_type, retry_count)


def jobs_retry_failed() -> list[str]:
    """全 failed ジョブを pending にリセットし、ワーカーに通知する。"""
    rows = jdb_reset_failed()
    job_ids = [r["id"] for r in rows]
    if job_ids and _new_job_event is not None:
        _new_job_event.set()
    return job_ids


def jobs_retry_one(job_id: str) -> bool:
    """指定した failed ジョブを pending にリセットしてワーカーに通知する。"""
    ok = jdb_reset_one_failed(job_id)
    if ok and _new_job_event is not None:
        _new_job_event.set()
    return ok


def jobs_clear_failed() -> int:
    """全 failed ジョブを SQLite から削除する。削除件数を返す。"""
    return jdb_delete_failed()


async def jobs_run_cleanup_worker(days: int) -> None:
    """
    定期クリーンアップワーカー。24時間ごとに、days 日以上前に失敗した
    ジョブを SQLite から削除する。lifespan で asyncio.Task として起動する。
    """
    while True:
        await asyncio.sleep(JOB_CLEANUP_INTERVAL)
        deleted = jdb_delete_old_failed(days)
        if deleted:
            logger.info("cleanup: %d failed jobs older than %d days deleted", deleted, days)
