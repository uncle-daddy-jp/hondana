"""
X (Twitter) ブックマークを HONDANA に取り込む

認証方式（環境変数で選択）:
  X_ACCESS_TOKEN  — OAuth 2.0 ユーザーアクセストークン（推奨）
                    X Developer Portal で取得したユーザートークン
  X_BEARER_TOKEN  — アプリ専用 Bearer Token（ブックマーク取得には不可、一部エンドポイントのみ）

ブックマーク API は OAuth 2.0 User Context（bookmark.read スコープ）が必須。
X_ACCESS_TOKEN を使用すること。

登録先:
  table = x
  title = "[@username] ツイート冒頭80字"
  source_url = https://x.com/{username}/status/{tweet_id}
  duplicate_action = skip（同一ツイートは重複投入しない）
"""

import json
import os
from pathlib import Path
from datetime import datetime

import requests

HONDANA_URL = os.environ.get("HONDANA_URL", "http://api:8000")
X_CLIENT_ID = os.environ.get("X_CLIENT_ID", "")
X_CLIENT_SECRET = os.environ.get("X_CLIENT_SECRET", "")
TABLE = "x"
STATE_FILE = Path(os.environ.get("STATE_FILE_X", "/data/state/x_ingest_state.json"))
TOKEN_FILE = STATE_FILE.parent / "x_tokens.json"


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"bookmark_ids": []}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def load_tokens() -> tuple[str, str]:
    """(access_token, refresh_token) をトークンファイル→環境変数の順で読む。"""
    if TOKEN_FILE.exists():
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        access = data.get("access_token", "")
        refresh = data.get("refresh_token", "")
        if access:
            return access, refresh
    return os.environ.get("X_ACCESS_TOKEN", ""), os.environ.get("X_REFRESH_TOKEN", "")


def save_tokens(access_token: str, refresh_token: str):
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(
        json.dumps({"access_token": access_token, "refresh_token": refresh_token}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def refresh_access_token(refresh_token: str) -> tuple[str, str]:
    """リフレッシュトークンで新しいアクセストークンを取得し保存する。(access, refresh) を返す。"""
    resp = requests.post(
        "https://api.twitter.com/2/oauth2/token",
        auth=(X_CLIENT_ID, X_CLIENT_SECRET),
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    new_access = data["access_token"]
    # X はリフレッシュトークンをローテーションする場合があるので更新する
    new_refresh = data.get("refresh_token", refresh_token)
    save_tokens(new_access, new_refresh)
    return new_access, new_refresh


def get_my_user_id(token: str) -> str:
    resp = requests.get(
        "https://api.twitter.com/2/users/me",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["data"]["id"]


def fetch_bookmarks(user_id: str, token: str, pagination_token: str | None = None) -> dict:
    params = {
        "max_results": 100,
        "tweet.fields": "created_at,author_id,text,note_tweet,entities",
        "expansions": "author_id",
        "user.fields": "username",
    }
    if pagination_token:
        params["pagination_token"] = pagination_token

    resp = requests.get(
        f"https://api.twitter.com/2/users/{user_id}/bookmarks",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def ingest_bookmark(tweet: dict, users: dict) -> bool:
    tweet_id = tweet["id"]
    author_id = tweet.get("author_id", "")
    username = users.get(author_id, "unknown")

    # note_tweet（長文ポスト）があればそちら優先
    note = tweet.get("note_tweet", {})
    text = note.get("text", "") or tweet.get("text", "")

    title = f"[@{username}] {text[:80]}"
    source_url = f"https://x.com/{username}/status/{tweet_id}"

    payload = {
        "title": title,
        "text": f"[@{username}]\n\n{text}",
        "source_url": source_url,
        "table": TABLE,
        "duplicate_action": "skip",
    }
    try:
        resp = requests.post(f"{HONDANA_URL}/api/ingest/text", json=payload, timeout=30)
        return resp.status_code in (200, 202)
    except Exception as e:
        print(f"  [エラー] {tweet_id}: {e}")
        return False


def run():
    access_token, refresh_token = load_tokens()
    if not access_token:
        print("[X] X_ACCESS_TOKEN が設定されていません。スキップ。")
        return

    state = load_state()
    processed_ids = set(state.get("bookmark_ids", []))

    try:
        user_id = get_my_user_id(access_token)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 401 and refresh_token and X_CLIENT_ID:
            print("[X] トークン期限切れ。リフレッシュ中...")
            try:
                access_token, refresh_token = refresh_access_token(refresh_token)
                user_id = get_my_user_id(access_token)
                print("[X] トークン更新成功。")
            except Exception as re:
                print(f"[X] トークンリフレッシュ失敗: {re}")
                return
        else:
            print(f"[X] ユーザーID取得失敗: {e}")
            return
    except Exception as e:
        print(f"[X] ユーザーID取得失敗: {e}")
        return

    print(f"[{datetime.now().strftime('%H:%M:%S')}] X ブックマーク取得中 (user_id={user_id})")
    ok = skip = err = 0
    next_token = None

    while True:
        try:
            data = fetch_bookmarks(user_id, access_token, next_token)
        except Exception as e:
            print(f"  [エラー] ブックマーク取得失敗: {e}")
            break

        tweets = data.get("data", [])
        if not tweets:
            break

        users_list = data.get("includes", {}).get("users", [])
        users = {u["id"]: u["username"] for u in users_list}

        for tweet in tweets:
            tweet_id = tweet["id"]
            if tweet_id in processed_ids:
                skip += 1
                continue
            if ingest_bookmark(tweet, users):
                ok += 1
                processed_ids.add(tweet_id)
            else:
                err += 1

        meta = data.get("meta", {})
        next_token = meta.get("next_token")
        if not next_token:
            break

    state["bookmark_ids"] = list(processed_ids)
    save_state(state)
    print(f"→ 完了: {ok}件登録 / {err}件失敗 / {skip}件スキップ")


if __name__ == "__main__":
    run()
