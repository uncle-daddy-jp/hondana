"""Hondana バックアップスクリプト。

hondana api コンテナ内から実行する。以下の前提が必要:
  - rsync / openssh-client が api/Dockerfile でインストール済み
  - NAS_HOST, NAS_USER 環境変数が設定済み
  - SSH 鍵が NAS_SSH_KEY のパス (デフォルト /root/.ssh/nas_backup) にマウント済み

Usage:
  python scripts/backup.py
"""
import datetime
import gzip
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

_QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
_SQLITE_PATH = os.getenv("HONDANA_DB_PATH", "/data/hondana/jobs.db")

_NAS_HOST = os.getenv("NAS_HOST", "")
_NAS_USER = os.getenv("NAS_USER", "")
_NAS_BACKUP_ROOT = os.getenv("NAS_BACKUP_ROOT", "/volume1/backups/hondana")
_NAS_SSH_KEY = os.getenv("NAS_SSH_KEY", "/root/.ssh/nas_backup")
_BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "10"))
_ENV_FILE = os.getenv("BACKUP_ENV_FILE", "/secrets/project.env")

_QDRANT_TIMEOUT = 600


def _backup_qdrant(folder: str) -> None:
    qdrant_dir = os.path.join(folder, "qdrant")
    os.makedirs(qdrant_dir)

    r = httpx.get(f"{_QDRANT_URL}/collections", timeout=30)
    r.raise_for_status()
    collections = [c["name"] for c in r.json()["result"]["collections"]]

    if not collections:
        log.info("qdrant: コレクションなし")
        return

    failed = []
    for name in collections:
        try:
            # red 状態のコレクションはスナップショット作成不可のためスキップ
            status = httpx.get(f"{_QDRANT_URL}/collections/{name}", timeout=10)
            status.raise_for_status()
            if status.json()["result"]["status"] == "red":
                log.warning("qdrant: red コレクションをスキップ: %s", name)
                failed.append(name)
                continue

            snap = httpx.post(
                f"{_QDRANT_URL}/collections/{name}/snapshots", timeout=120
            )
            snap.raise_for_status()
            snap_name = snap.json()["result"]["name"]
            log.info("qdrant snapshot 作成: %s / %s", name, snap_name)

            gz_path = os.path.join(qdrant_dir, f"{name}.snapshot.gz")
            with httpx.stream(
                "GET",
                f"{_QDRANT_URL}/collections/{name}/snapshots/{snap_name}",
                timeout=_QDRANT_TIMEOUT,
            ) as resp:
                resp.raise_for_status()
                with gzip.open(gz_path, "wb", compresslevel=6) as gz_f:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        gz_f.write(chunk)

            size_mb = os.path.getsize(gz_path) / 1024 / 1024
            log.info("qdrant downloaded: %s → %.1f MB (compressed)", name, size_mb)

            httpx.delete(
                f"{_QDRANT_URL}/collections/{name}/snapshots/{snap_name}", timeout=30
            )
        except Exception as e:
            log.error("qdrant: %s のバックアップ失敗: %s", name, e)
            failed.append(name)

    if failed:
        raise RuntimeError(f"qdrant: {len(failed)} コレクション失敗: {failed}")


def _backup_sqlite(folder: str) -> None:
    if not os.path.exists(_SQLITE_PATH):
        log.warning("SQLite が見つかりません: %s", _SQLITE_PATH)
        return

    dst_path = os.path.join(folder, "jobs.db")
    src = sqlite3.connect(_SQLITE_PATH)
    dst = sqlite3.connect(dst_path)
    src.backup(dst)
    src.close()
    dst.close()
    log.info("sqlite backup ok: %.1f MB", os.path.getsize(dst_path) / 1024 / 1024)


def _backup_env(folder: str) -> None:
    if not os.path.exists(_ENV_FILE):
        log.warning("env ファイルが見つかりません: %s", _ENV_FILE)
        return
    dst = os.path.join(folder, "project.env")
    shutil.copy2(_ENV_FILE, dst)
    log.info("env backup ok: %s", _ENV_FILE)


_KEY_WORK = "/root/.ssh/nas_backup_rw"


def _prepare_key() -> None:
    """SSH 鍵を書き込み可能な固定パスにコピーして chmod 600 する。

    Windows バインドマウントは 0777 になるため SSH が拒否する。
    初回のみコピーすれば以降はそのまま使いまわせる。
    """
    if not os.path.exists(_KEY_WORK) or os.path.getmtime(_NAS_SSH_KEY) > os.path.getmtime(_KEY_WORK):
        os.makedirs(os.path.dirname(_KEY_WORK), exist_ok=True)
        shutil.copy2(_NAS_SSH_KEY, _KEY_WORK)
        os.chmod(_KEY_WORK, 0o600)
        log.info("SSH 鍵をコピー: %s", _KEY_WORK)


def _ssh_opts() -> list[str]:
    return [
        "-i", _KEY_WORK,
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",
    ]


def _rsync_to_nas(local_folder: str, label: str) -> None:
    if not _NAS_HOST:
        log.warning("NAS_HOST が未設定のため rsync をスキップします")
        return

    remote_dir = f"{_NAS_BACKUP_ROOT}/{label}"
    remote = f"{_NAS_USER}@{_NAS_HOST}:{remote_dir}/"

    # --mkpath は rsync 3.2.3+ のみ。NAS 側が古い場合もあるため SSH で先にディレクトリを作る
    subprocess.run(
        ["ssh"] + _ssh_opts() + [f"{_NAS_USER}@{_NAS_HOST}", f"mkdir -p {remote_dir}"],
        check=True,
    )

    cmd = [
        "rsync", "-av",
        "-e", "ssh " + " ".join(_ssh_opts()),
        local_folder + "/",
        remote,
    ]
    log.info("rsync 開始: %s", remote)
    subprocess.run(cmd, check=True)
    log.info("rsync 完了")


def _rotate_nas() -> None:
    if not _NAS_HOST:
        return

    remote_cmd = (
        f"ls -1d {_NAS_BACKUP_ROOT}/backup_* 2>/dev/null"
        f" | sort | head -n -{_BACKUP_KEEP}"
        f" | xargs -r rm -rf"
    )
    subprocess.run(
        ["ssh"] + _ssh_opts() + [f"{_NAS_USER}@{_NAS_HOST}", remote_cmd],
        check=True,
    )
    log.info("NAS ローテーション完了 (保持=%d世代)", _BACKUP_KEEP)


def run() -> None:
    _prepare_key()
    label = f"backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    log.info("=== hondana backup 開始: %s ===", label)

    with tempfile.TemporaryDirectory() as tmp:
        folder = os.path.join(tmp, label)
        os.makedirs(folder)

        _backup_qdrant(folder)
        _backup_sqlite(folder)
        _backup_env(folder)
        _rsync_to_nas(folder, label)

    _rotate_nas()
    log.info("=== hondana backup 完了: %s ===", label)


if __name__ == "__main__":
    run()
