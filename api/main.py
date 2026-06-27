"""main.py — FastAPI app construction, lifespan, router wiring, and shared AppState."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from auth import auth_require_key
from config_store import load_config
from db import db_list_tables
from ingest.embed_client import embed_client_build
from ingest.llm_client import llm_client_build
from ingest.pipeline import pipeline_ensure_fts_index
from jobs import jobs_init_event, jobs_notify, jobs_run_cleanup_worker, jobs_run_worker
from jobs_db import jdb_has_pending, jdb_init, jdb_reset_running
from routers.ask import router as ask_router
from routers.config import router as config_router
from routers.ingest import router as ingest_router
from routers.jobs import router as jobs_router
from routers.mcp import router as mcp_router
from routers.tables import router as tables_router
from routers.view import router as view_router

# ── App state ─────────────────────────────────────────────────────────────────


class AppState:
    cfg: dict
    llm: object
    embed: object
    data_dir: Path
    inbox_dir: Path
    done_dir: Path


state = AppState()


# ── Lifespan ──────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Imported here to avoid a circular import: job_handler.py imports `state` from main.
    from job_handler import job_handler

    cfg = load_config()
    state.cfg = cfg
    state.data_dir = Path(cfg.get("data_dir", "/data/hondana"))
    state.inbox_dir = Path(cfg.get("inbox_dir", "/inbox"))
    state.done_dir = Path(cfg.get("done_dir", "/done"))
    state.llm = llm_client_build(cfg)
    state.embed = embed_client_build(cfg)
    app.state.data_dir = state.data_dir  # expose to routers via request.app.state
    app.state.llm = state.llm
    app.state.embed = state.embed
    app.state.cfg = state.cfg
    app.state.state_ref = state  # live reference — auth reads cfg from here

    jdb_init(state.data_dir, int(cfg.get("max_retries", 3)))
    jdb_reset_running()  # 前回中断された running ジョブを pending に戻す

    # Ensure FTS index on all existing Qdrant collections
    try:
        for _cname in db_list_tables():
            pipeline_ensure_fts_index(_cname)
    except Exception:
        logger.warning("FTS index setup error during startup", exc_info=True)

    jobs_init_event()
    if jdb_has_pending():
        jobs_notify()
        logger.info("Pending jobs found in SQLite — workers will pick them up on start")

    worker_count = int(cfg.get("worker_count", 2))
    retention_days = int(cfg.get("failed_job_retention_days", 7))
    worker_tasks = [asyncio.create_task(jobs_run_worker(job_handler)) for _ in range(worker_count)]
    cleanup_task = asyncio.create_task(jobs_run_cleanup_worker(retention_days))
    logger.info("cleanup worker started: retain failed jobs for %d days", retention_days)

    yield

    for t in [*worker_tasks, cleanup_task]:
        t.cancel()
    for t in [*worker_tasks, cleanup_task]:
        try:
            await t
        except asyncio.CancelledError:
            pass


# ── App construction ──────────────────────────────────────────────────────────

app = FastAPI(title="HONDANA API", lifespan=lifespan, dependencies=[Depends(auth_require_key)])
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    """Log unhandled backend errors and return a uniform JSON 500. 4xx still use HTTPException."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


app.include_router(view_router)
app.include_router(ingest_router)
app.include_router(jobs_router)
app.include_router(tables_router)
app.include_router(ask_router)
app.include_router(config_router)
app.include_router(mcp_router)
