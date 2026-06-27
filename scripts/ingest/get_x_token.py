"""
X OAuth 2.0 アクセストークン取得スクリプト
使い方: python get_x_token.py
- ブラウザが開くので X でログイン→許可
- トークンが表示されるので .env の X_ACCESS_TOKEN に貼る
"""

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import requests

# .env から読み込む
def load_env(path=".env"):
    env = {}
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env

script_dir = os.path.dirname(os.path.abspath(__file__))
env = load_env(os.path.join(script_dir, "..", "..", ".env"))

CLIENT_ID     = os.environ.get("X_CLIENT_ID")     or env.get("X_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("X_CLIENT_SECRET") or env.get("X_CLIENT_SECRET", "")
REDIRECT_URI  = "http://127.0.0.1:8888/callback"
SCOPES        = "bookmark.read tweet.read tweet.write users.read offline.access"

# トークン保存先（Docker マウント先と同じディレクトリ）
TOKEN_FILE = Path(os.path.join(script_dir, "..", "..", "data", "ingest-state", "x_tokens.json"))

if not CLIENT_ID or not CLIENT_SECRET:
    print("エラー: .env に X_CLIENT_ID と X_CLIENT_SECRET を設定してください")
    exit(1)

# PKCE
code_verifier  = secrets.token_urlsafe(64)
code_challenge = base64.urlsafe_b64encode(
    hashlib.sha256(code_verifier.encode()).digest()
).rstrip(b"=").decode()
state = secrets.token_urlsafe(16)

auth_url = (
    "https://x.com/i/oauth2/authorize"
    f"?response_type=code"
    f"&client_id={CLIENT_ID}"
    f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
    f"&scope={urllib.parse.quote(SCOPES)}"
    f"&state={state}"
    f"&code_challenge={code_challenge}"
    f"&code_challenge_method=S256"
)

# コールバックを受け取るための変数
callback_result = {}
server_done = threading.Event()


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))

        if parsed.path == "/callback":
            callback_result.update(params)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<html><body><h2>認証完了。このタブを閉じてください。</h2></body></html>".encode())
            server_done.set()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass


server = http.server.HTTPServer(("127.0.0.1", 8888), CallbackHandler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()

print(f"\n以下のURLをブラウザで開いてください:\n")
print(auth_url)
print()
webbrowser.open(auth_url)

server_done.wait(timeout=120)
server.shutdown()

if "code" not in callback_result:
    print("エラー: コールバックが受信できませんでした")
    exit(1)

if callback_result.get("state") != state:
    print("エラー: state が一致しません（CSRF の可能性）")
    exit(1)

# トークン取得
resp = requests.post(
    "https://api.twitter.com/2/oauth2/token",
    auth=(CLIENT_ID, CLIENT_SECRET),
    data={
        "grant_type":    "authorization_code",
        "code":          callback_result["code"],
        "redirect_uri":  REDIRECT_URI,
        "code_verifier": code_verifier,
    },
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)

if resp.status_code != 200:
    print(f"エラー: トークン取得失敗 {resp.status_code}: {resp.text}")
    exit(1)

data = resp.json()
access_token  = data.get("access_token", "")
refresh_token = data.get("refresh_token", "")

print()
print("=" * 60)
print(f"X_ACCESS_TOKEN={access_token}")
if refresh_token:
    print(f"X_REFRESH_TOKEN={refresh_token}")
print("=" * 60)

# トークンをファイルに保存（Docker コンテナが /data/state/ 経由で読む）
TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
TOKEN_FILE.write_text(
    json.dumps({"access_token": access_token, "refresh_token": refresh_token}, indent=2, ensure_ascii=False),
    encoding="utf-8",
)
print(f"\nトークンを {TOKEN_FILE} に保存しました。")
if not refresh_token:
    print("⚠ リフレッシュトークンが取得できませんでした。X Developer Portal で offline.access が許可されているか確認してください。")
