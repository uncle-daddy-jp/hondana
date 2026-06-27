"""
tests/test_jobs_db.py — SQLite 永続化層 (jdb_*) のユニットテスト。

各テストは fresh_db フィクスチャで独立した tmp_path DB を使うため、
前のテストの状態に依存しない。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jobs_db as jdb_module
from jobs_db import (
    jdb_claim_next,
    jdb_delete_failed,
    jdb_delete_old_failed,
    jdb_get,
    jdb_has_pending,
    jdb_has_pending_url,
    jdb_init,
    jdb_insert,
    jdb_list,
    jdb_max_retries,
    jdb_reset_failed,
    jdb_reset_one_failed,
    jdb_reset_running,
    jdb_stats,
    jdb_update_status,
)

# ── jdb_insert / jdb_get ─────────────────────────────────────────────────────


def test_insert_then_get_returns_job(fresh_db):
    jdb_insert("job-1", "ingest_url", {"url": "https://example.com"})
    job = jdb_get("job-1")
    assert job is not None
    assert job["id"] == "job-1"
    assert job["type"] == "ingest_url"
    assert job["payload"]["url"] == "https://example.com"


def test_insert_sets_status_pending(fresh_db):
    jdb_insert("job-2", "ingest_url", {"url": "https://example.com"})
    job = jdb_get("job-2")
    assert job["status"] == "pending"
    assert job["retry_count"] == 0


def test_get_missing_job_returns_none(fresh_db):
    assert jdb_get("nonexistent") is None


# ── jdb_claim_next ───────────────────────────────────────────────────────────


def test_claim_next_moves_job_to_running(fresh_db):
    jdb_insert("job-claim", "ingest_url", {"url": "https://c.com"})
    claimed = jdb_claim_next()
    assert claimed is not None
    assert claimed["id"] == "job-claim"
    assert claimed["status"] == "running"

    # DB 側にも反映されていること
    job = jdb_get("job-claim")
    assert job["status"] == "running"
    assert job["started_at"] is not None


def test_claim_next_twice_returns_none_for_second_call(fresh_db):
    jdb_insert("only-one", "ingest_url", {"url": "https://only.com"})
    first = jdb_claim_next()
    second = jdb_claim_next()
    assert first is not None
    assert first["id"] == "only-one"
    assert second is None


def test_claim_next_with_empty_queue_returns_none(fresh_db):
    assert jdb_claim_next() is None


# ── jdb_update_status ────────────────────────────────────────────────────────


def test_update_status_to_done(fresh_db):
    jdb_insert("job-done", "ingest_url", {"url": "https://d.com"})
    jdb_update_status(
        "job-done",
        status="done",
        finished_at="2026-04-21T00:00:00+00:00",
        result={"ok": True},
    )
    job = jdb_get("job-done")
    assert job["status"] == "done"
    assert job["finished_at"] == "2026-04-21T00:00:00+00:00"
    assert job["result"] == {"ok": True}


def test_update_status_to_failed(fresh_db):
    jdb_insert("job-fail", "ingest_url", {"url": "https://f.com"})
    jdb_update_status(
        "job-fail",
        status="failed",
        error="boom",
        retry_count=3,
    )
    job = jdb_get("job-fail")
    assert job["status"] == "failed"
    assert job["error"] == "boom"
    assert job["retry_count"] == 3


# ── jdb_has_pending ──────────────────────────────────────────────────────────


def test_has_pending_true_when_pending_exists(fresh_db):
    jdb_insert("p1", "ingest_url", {"url": "https://p.com"})
    assert jdb_has_pending() is True


def test_has_pending_false_when_no_pending(fresh_db):
    assert jdb_has_pending() is False
    # 完了済みジョブは pending としてカウントされない
    jdb_insert("p2", "ingest_url", {"url": "https://p2.com"})
    jdb_update_status("p2", status="done")
    assert jdb_has_pending() is False


# ── jdb_has_pending_url ──────────────────────────────────────────────────────


def test_has_pending_url_true_for_matching_url(fresh_db):
    jdb_insert("u1", "ingest_url", {"url": "https://hit.com"})
    assert jdb_has_pending_url("https://hit.com") is True


def test_has_pending_url_false_for_unknown_url(fresh_db):
    jdb_insert("u2", "ingest_url", {"url": "https://other.com"})
    assert jdb_has_pending_url("https://missing.com") is False


def test_has_pending_url_false_when_job_done(fresh_db):
    jdb_insert("u3", "ingest_url", {"url": "https://done.com"})
    jdb_update_status("u3", status="done")
    assert jdb_has_pending_url("https://done.com") is False


# ── jdb_reset_running ────────────────────────────────────────────────────────


def test_reset_running_moves_running_to_pending(fresh_db):
    jdb_insert("r1", "ingest_url", {"url": "https://r.com"})
    jdb_claim_next()  # pending → running
    assert jdb_get("r1")["status"] == "running"

    jdb_reset_running()
    job = jdb_get("r1")
    assert job["status"] == "pending"
    assert job["started_at"] is None


# ── jdb_reset_failed ─────────────────────────────────────────────────────────


def test_reset_failed_returns_list_and_moves_to_pending(fresh_db):
    jdb_insert("f1", "ingest_url", {"url": "https://f1.com"})
    jdb_insert("f2", "ingest_url", {"url": "https://f2.com"})
    jdb_update_status("f1", status="failed", error="e1", retry_count=3)
    jdb_update_status("f2", status="failed", error="e2", retry_count=3)

    reset = jdb_reset_failed()
    reset_ids = {r["id"] for r in reset}
    assert reset_ids == {"f1", "f2"}

    for job_id in ("f1", "f2"):
        job = jdb_get(job_id)
        assert job["status"] == "pending"
        assert job["retry_count"] == 0
        assert job["error"] is None


def test_reset_failed_returns_empty_when_no_failed(fresh_db):
    assert jdb_reset_failed() == []


# ── jdb_reset_one_failed ─────────────────────────────────────────────────────


def test_reset_one_failed_restores_specific_job(fresh_db):
    jdb_insert("one-fail", "ingest_url", {"url": "https://one.com"})
    jdb_update_status("one-fail", status="failed", error="x", retry_count=3)

    ok = jdb_reset_one_failed("one-fail")
    assert ok is True

    job = jdb_get("one-fail")
    assert job["status"] == "pending"
    assert job["retry_count"] == 0


def test_reset_one_failed_returns_false_for_missing(fresh_db):
    assert jdb_reset_one_failed("does-not-exist") is False


def test_reset_one_failed_returns_false_for_non_failed(fresh_db):
    jdb_insert("pending-job", "ingest_url", {"url": "https://p.com"})
    # pending なジョブには作用しない
    assert jdb_reset_one_failed("pending-job") is False


# ── jdb_delete_failed ────────────────────────────────────────────────────────


def test_delete_failed_removes_failed_jobs(fresh_db):
    jdb_insert("d1", "ingest_url", {"url": "https://d1.com"})
    jdb_insert("d2", "ingest_url", {"url": "https://d2.com"})
    jdb_insert("d3", "ingest_url", {"url": "https://d3.com"})
    jdb_update_status("d1", status="failed")
    jdb_update_status("d2", status="failed")
    # d3 は pending のまま

    count = jdb_delete_failed()
    assert count == 2
    assert jdb_get("d1") is None
    assert jdb_get("d2") is None
    assert jdb_get("d3") is not None


def test_delete_failed_returns_zero_when_none(fresh_db):
    assert jdb_delete_failed() == 0


# ── jdb_list ─────────────────────────────────────────────────────────────────


def test_list_respects_limit(fresh_db):
    for i in range(5):
        jdb_insert(f"l{i}", "ingest_url", {"url": f"https://l{i}.com"})
    result = jdb_list(limit=3)
    assert len(result) == 3


def test_list_filters_by_status(fresh_db):
    jdb_insert("ls1", "ingest_url", {"url": "https://ls1.com"})
    jdb_insert("ls2", "ingest_url", {"url": "https://ls2.com"})
    jdb_update_status("ls2", status="done")

    pending_jobs = jdb_list(limit=10, status="pending")
    done_jobs = jdb_list(limit=10, status="done")

    assert [j["id"] for j in pending_jobs] == ["ls1"]
    assert [j["id"] for j in done_jobs] == ["ls2"]


# ── jdb_stats ────────────────────────────────────────────────────────────────


def test_stats_counts_per_status(fresh_db):
    jdb_insert("s1", "ingest_url", {"url": "https://s1.com"})
    jdb_insert("s2", "ingest_url", {"url": "https://s2.com"})
    jdb_insert("s3", "ingest_url", {"url": "https://s3.com"})
    jdb_insert("s4", "ingest_url", {"url": "https://s4.com"})
    jdb_update_status("s2", status="done")
    jdb_update_status("s3", status="failed")
    jdb_update_status("s4", status="running")

    stats = jdb_stats()
    assert stats["pending"] == 1
    assert stats["done"] == 1
    assert stats["failed"] == 1
    assert stats["running"] == 1
    assert stats["total"] == 4


# ── jdb_max_retries ──────────────────────────────────────────────────────────


def test_max_retries_returns_configured_value(fresh_db):
    # fresh_db は max_retries=2 で初期化している
    assert jdb_max_retries() == 2


def test_max_retries_reflects_new_init(tmp_path, monkeypatch):
    monkeypatch.setattr(jdb_module, "_conn", None)
    monkeypatch.setattr(jdb_module, "_max_retries", 3)
    jdb_init(tmp_path, max_retries=7)
    assert jdb_max_retries() == 7
    if jdb_module._conn is not None:
        jdb_module._conn.close()


# ── jdb_delete_old_failed ────────────────────────────────────────────────────


def test_delete_old_failed_removes_only_old_jobs(fresh_db):
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=10)).isoformat()
    recent_ts = (now - timedelta(days=1)).isoformat()

    jdb_insert("old", "ingest_url", {"url": "https://old.com"})
    jdb_insert("recent", "ingest_url", {"url": "https://recent.com"})
    jdb_update_status("old", status="failed", finished_at=old_ts)
    jdb_update_status("recent", status="failed", finished_at=recent_ts)

    # 7日以上前の failed を削除する
    deleted = jdb_delete_old_failed(days=7)
    assert deleted == 1
    assert jdb_get("old") is None
    assert jdb_get("recent") is not None


def test_delete_old_failed_leaves_non_failed(fresh_db):
    """finished_at が古くても status が failed でなければ削除されない。"""
    old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    jdb_insert("done-old", "ingest_url", {"url": "https://done-old.com"})
    jdb_update_status("done-old", status="done", finished_at=old_ts)

    deleted = jdb_delete_old_failed(days=7)
    assert deleted == 0
    assert jdb_get("done-old") is not None
