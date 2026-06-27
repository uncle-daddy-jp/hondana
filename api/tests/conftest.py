import sys
from pathlib import Path

# api/ ディレクトリをパスに追加（tests/ から jobs, jobs_db 等を import できるようにする）
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """
    テストごとに独立した SQLite ファイルを tmp_path に作り、jobs_db のモジュール状態をリセットする。
    jdb_init は data_dir / "jobs.db" を開くため、:memory: ではなく tmp_path を渡す。
    """
    import jobs_db as jdb_module
    from jobs_db import jdb_init

    monkeypatch.setattr(jdb_module, "_conn", None)
    monkeypatch.setattr(jdb_module, "_max_retries", 3)
    jdb_init(tmp_path, max_retries=2)
    yield
    if jdb_module._conn is not None:
        jdb_module._conn.close()
        monkeypatch.setattr(jdb_module, "_conn", None)
