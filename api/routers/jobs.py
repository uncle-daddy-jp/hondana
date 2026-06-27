"""
routers/jobs.py — ジョブステータス・リトライ・削除エンドポイント。

  GET    /api/jobs/stats          → キュー深度サマリー
  GET    /api/jobs                → ジョブ一覧（最新順）
  POST   /api/jobs/retry-failed   → 全 failed ジョブを pending にリセットして再キュー
  DELETE /api/jobs/failed         → 全 failed ジョブを削除
  GET    /api/jobs/{job_id}       → 単一ジョブ詳細
  POST   /api/jobs/{job_id}/retry → 指定ジョブを pending にリセットして再キュー

注意: FastAPI はルートを登録順に評価するため、固定パス（/stats, /retry-failed, /failed）は
動的パス（/{job_id}）より先に登録すること。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from jobs import jobs_clear_failed, jobs_get, jobs_list, jobs_retry_failed, jobs_retry_one, jobs_stats

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("/stats")
def get_job_stats():
    """キュー深度サマリー — エンキュー前の負荷確認に使う。"""
    return jobs_stats()


@router.get("")
def list_jobs(limit: int = 50, status: str | None = None):
    return {"stats": jobs_stats(), "jobs": jobs_list(limit, status=status)}


@router.post("/retry-failed")
def retry_all_failed_jobs():
    """
    全 failed ジョブを pending にリセットしてキューに再投入する。
    retry_count を 0 にリセットするため、max_retries 回の再試行が再度利用できる。
    SQLite にのみ残る古いジョブも復元する。
    """
    retried = jobs_retry_failed()
    return {"retried": len(retried), "job_ids": retried}


@router.delete("/failed")
def clear_failed_jobs():
    """全 failed ジョブをメモリと SQLite から削除する。"""
    deleted = jobs_clear_failed()
    return {"deleted": deleted}


@router.get("/{job_id}")
def get_job(job_id: str):
    job = jobs_get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return job


@router.post("/{job_id}/retry")
def retry_one_job(job_id: str):
    """指定した failed ジョブを pending にリセットしてキューに再投入する。"""
    found = jobs_retry_one(job_id)
    if not found:
        job = jobs_get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        raise HTTPException(
            status_code=409,
            detail=f"Job '{job_id}' is not in failed state (current: {job['status']})",
        )
    return {"job_id": job_id, "status": "pending"}
