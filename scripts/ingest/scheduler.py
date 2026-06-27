"""
インジェストスケジューラー
- POST /trigger/x       → ingest_x.run() を即時実行（X_ACCESS_TOKEN 設定時のみ）
- X は毎時 00 分に自動実行
"""

import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    import ingest_x
    X_ENABLED = bool(os.environ.get("X_ACCESS_TOKEN"))
except ImportError:
    ingest_x = None
    X_ENABLED = False


def run_locked(lock: threading.Lock, fn, name: str):
    if lock.acquire(blocking=False):
        try:
            print(f"[スケジューラー] {name} 開始", flush=True)
            fn()
            print(f"[スケジューラー] {name} 完了", flush=True)
        except Exception as e:
            print(f"[スケジューラー] {name} エラー: {e}", flush=True)
        finally:
            lock.release()
    else:
        print(f"[スケジューラー] {name} は実行中のためスキップ", flush=True)


x_lock = threading.Lock()


class TriggerHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/trigger/x":
            if not X_ENABLED or ingest_x is None:
                self._respond(503, "X ingest not configured")
            else:
                threading.Thread(
                    target=run_locked,
                    args=(x_lock, ingest_x.run, "x"),
                    daemon=True,
                ).start()
                self._respond(200, "x ingest triggered")

        elif self.path == "/health":
            self._respond(200, "ok")

        else:
            self._respond(404, "not found")

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, "ok")
        else:
            self._respond(404, "not found")

    def _respond(self, status: int, body: str):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, fmt, *args):
        pass  # リクエストログは抑制


def schedule_loop():
    """毎時 00 分に X ブックマークを自動取り込み"""
    while True:
        now = datetime.now()
        if now.minute == 0 and X_ENABLED and ingest_x is not None:
            print(f"[スケジューラー] 定期実行: X ({now.strftime('%H:%M')})", flush=True)
            threading.Thread(
                target=run_locked,
                args=(x_lock, ingest_x.run, "x"),
                daemon=True,
            ).start()
        time.sleep(60)


if __name__ == "__main__":
    port = int(os.environ.get("SCHEDULER_PORT", "8888"))
    print(f"[スケジューラー] 起動 port={port}  X_ENABLED={X_ENABLED}", flush=True)

    threading.Thread(target=schedule_loop, daemon=True).start()
    HTTPServer(("0.0.0.0", port), TriggerHandler).serve_forever()
