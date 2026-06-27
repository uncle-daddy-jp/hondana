"""
tests/test_jobs.py — asyncio イベント駆動ジョブキュー (jobs_*) のユニットテスト。

jobs_run_worker は async generator 的に動き続けるため、
asyncio.create_task で起動し、処理が終わったら cancel する。
"""

from __future__ import annotations

import asyncio

import pytest

from jobs import (
    jobs_clear_failed,
    jobs_enqueue,
    jobs_get,
    jobs_init_event,
    jobs_list,
    jobs_retry_failed,
    jobs_retry_one,
    jobs_run_worker,
    jobs_stats,
)

# ── 同期テスト: enqueue / get / list / stats ─────────────────────────────────


def test_enqueue_returns_id_and_get_finds_job(fresh_db):
    # enqueue はイベントを set しようとするので、同期テストでは Event を初期化しておく
    # （初期化しないと _new_job_event は None のままでスキップされる）
    job_id = jobs_enqueue("ingest_url", {"url": "https://example.com"})
    assert job_id

    job = jobs_get(job_id)
    assert job is not None
    assert job["status"] == "pending"
    assert job["type"] == "ingest_url"
    assert job["payload"]["url"] == "https://example.com"


def test_stats_reflects_enqueued_jobs(fresh_db):
    jobs_enqueue("ingest_url", {"url": "https://a.com"})
    jobs_enqueue("ingest_url", {"url": "https://b.com"})
    stats = jobs_stats()
    assert stats["pending"] == 2
    assert stats["total"] == 2


def test_list_returns_most_recent_first(fresh_db):
    id1 = jobs_enqueue("ingest_url", {"url": "https://first.com"})
    id2 = jobs_enqueue("ingest_url", {"url": "https://second.com"})
    listing = jobs_list(limit=10)
    ids = [j["id"] for j in listing]
    assert ids.index(id2) < ids.index(id1)  # id2 が後から挿入 → created_at 降順で先頭


# ── 非同期テスト: jobs_run_worker ─────────────────────────────────────────────


async def _run_worker_until_idle(handler, timeout: float = 2.0) -> None:
    """ワーカーを起動して pending が捌けるまで少し待ち、キャンセルして終了する。"""
    task = asyncio.create_task(jobs_run_worker(handler))
    try:
        # handler が処理する猶予を与える。jobs_run_worker は永遠に回るため
        # ここは短い sleep で十分（CI の遅さを考慮して 0.3 秒）。
        await asyncio.sleep(0.3)
    finally:
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


@pytest.mark.asyncio
async def test_worker_processes_job_to_done(fresh_db):
    jobs_init_event()
    job_id = jobs_enqueue("ingest_url", {"url": "https://ok.com"})

    async def handler(job_type, payload):
        return {"ok": True}

    await _run_worker_until_idle(handler)

    job = jobs_get(job_id)
    assert job["status"] == "done"
    assert job["result"] == {"ok": True}


@pytest.mark.asyncio
async def test_worker_marks_job_failed_after_max_retries(fresh_db):
    """fresh_db は max_retries=2 → 1回目+2リトライ=計3回失敗したら failed になる。"""
    jobs_init_event()
    job_id = jobs_enqueue("ingest_url", {"url": "https://fail.com"})

    call_count = {"n": 0}

    async def failing_handler(job_type, payload):
        call_count["n"] += 1
        raise RuntimeError("always fails")

    await _run_worker_until_idle(failing_handler)

    job = jobs_get(job_id)
    assert job["status"] == "failed"
    assert job["retry_count"] == 3  # 初回 + max_retries(2) 回のリトライ
    assert "always fails" in job["error"]
    assert call_count["n"] == 3


@pytest.mark.asyncio
async def test_worker_succeeds_on_retry(fresh_db):
    """1回目失敗・2回目成功のケースでは done になる。"""
    jobs_init_event()
    job_id = jobs_enqueue("ingest_url", {"url": "https://flaky.com"})

    attempt = {"n": 0}

    async def flaky_handler(job_type, payload):
        attempt["n"] += 1
        if attempt["n"] == 1:
            raise RuntimeError("first attempt fails")
        return {"ok": True}

    await _run_worker_until_idle(flaky_handler)

    job = jobs_get(job_id)
    assert job["status"] == "done"
    assert job["retry_count"] == 1


# ── jobs_retry_failed / jobs_retry_one / jobs_clear_failed ────────────────────


def test_retry_failed_resets_all_failed_to_pending(fresh_db):
    jobs_init_event()
    id1 = jobs_enqueue("ingest_url", {"url": "https://r1.com"})
    id2 = jobs_enqueue("ingest_url", {"url": "https://r2.com"})
    # 直接 DB を通じて failed に落とす
    from jobs_db import jdb_update_status

    jdb_update_status(id1, status="failed", error="e1", retry_count=3)
    jdb_update_status(id2, status="failed", error="e2", retry_count=3)

    reset_ids = jobs_retry_failed()
    assert set(reset_ids) == {id1, id2}

    for jid in (id1, id2):
        job = jobs_get(jid)
        assert job["status"] == "pending"
        assert job["retry_count"] == 0


def test_retry_one_resets_specified_job(fresh_db):
    jobs_init_event()
    job_id = jobs_enqueue("ingest_url", {"url": "https://one.com"})
    from jobs_db import jdb_update_status

    jdb_update_status(job_id, status="failed", error="x", retry_count=3)

    ok = jobs_retry_one(job_id)
    assert ok is True
    job = jobs_get(job_id)
    assert job["status"] == "pending"


def test_retry_one_returns_false_for_missing(fresh_db):
    jobs_init_event()
    assert jobs_retry_one("no-such-id") is False


def test_clear_failed_deletes_failed_jobs(fresh_db):
    jobs_init_event()
    id1 = jobs_enqueue("ingest_url", {"url": "https://c1.com"})
    id2 = jobs_enqueue("ingest_url", {"url": "https://c2.com"})
    id3 = jobs_enqueue("ingest_url", {"url": "https://c3.com"})
    from jobs_db import jdb_update_status

    jdb_update_status(id1, status="failed")
    jdb_update_status(id2, status="failed")
    # id3 は pending のまま

    count = jobs_clear_failed()
    assert count == 2
    assert jobs_get(id1) is None
    assert jobs_get(id2) is None
    assert jobs_get(id3) is not None
