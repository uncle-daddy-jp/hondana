# hondana-push

HONDANA が稼働しているサーバーとは別の PC から、ファイルや URL を知識ベースに送るための軽量クライアント。

- **単一 Python ファイル** — コピーするだけで使える
- **外部ライブラリ不要** — Python 3.10+ の標準ライブラリのみ
- **2 つのモード** — 手動送信（CLI）と自動監視（デーモン）

---

## 前提条件

- Python 3.10 以上
- HONDANA サーバーがネットワーク上で稼働していること（`http://サーバーIP:8200`）

---

## セットアップ

### 1. スクリプトをコピー

```bash
# Mac / Linux
cp hondana_push.py ~/bin/hondana_push.py
chmod +x ~/bin/hondana_push.py

# Windows（PowerShell）
Copy-Item hondana_push.py $HOME\bin\hondana_push.py
```

どこに置いてもよい。`python /path/to/hondana_push.py` で実行する。

### 2. 設定ファイルを作成

`~/.hondana-push.yml` を作成する：

```yaml
server_url: http://192.168.1.100:8200   # HONDANA サーバーの IP とポート
api_key: hondana-xxxx...                 # APIキー（未設定なら削除）
table: hondana_chunks                    # 送信先テーブル名
duplicate_action: overwrite            # overwrite / skip / new
watch_interval: 5                      # デーモンのポーリング間隔（秒）
watch_done_dir: ~/.hondana-push/done     # 送信済みファイルの移動先
```

**`api_key` の取得方法：**  
HONDANA WebUI（http://サーバーIP:8601）の **⚙️ Settings → APIキー管理** でキーを生成してコピーする。  
HONDANA 側で API キーが未設定の場合は、`api_key:` の行ごと削除するかブランクのままでよい。

### 3. 設定確認

```bash
python hondana_push.py config
```

```
Effective configuration:
  config file:      /home/user/.hondana-push.yml (found)
  server_url        http://192.168.1.100:8200
  api_key           ***
  table             hondana_chunks
  duplicate_action  overwrite
  watch_interval    5
  watch_done_dir    /home/user/.hondana-push/done
```

---

## 使い方

### ファイルを送る

```bash
python hondana_push.py article.md
python hondana_push.py report.pdf
python hondana_push.py presentation.pptx
```

送信中はジョブのステータスが表示され、完了すると結果が出る：

```
[send] article.md ...
  [running] job 3f2a1b8c...
[done]  'RAG アーキテクチャ入門' — inserted
```

### URL を送る

Web ページの URL を渡すと、本文を取得して知識ベースに登録する：

```bash
python hondana_push.py https://example.com/article
python hondana_push.py https://arxiv.org/abs/2310.12345
```

### テーブルを指定して送る

```bash
python hondana_push.py --table work report.pdf
python hondana_push.py --table research https://arxiv.org/abs/2310.12345
```

### 重複時の動作を指定する

```bash
# 既存の記事をスキップ（更新しない）
python hondana_push.py --action skip article.md

# 既存の記事を上書き（デフォルト）
python hondana_push.py --action overwrite article.md

# 別記事として新規追加（履歴として残す）
python hondana_push.py --action new article.md
```

### フォルダを監視して自動送信（デーモン）

指定フォルダを定期的にスキャンし、新しいファイルを自動で HONDANA に送る：

```bash
python hondana_push.py watch ~/Desktop/inbox
python hondana_push.py watch ~/Desktop/inbox --table work
```

動作：
1. `watch_interval` 秒（デフォルト: 5秒）ごとにフォルダをスキャン
2. 対応フォーマットの新着ファイルを検出
3. HONDANA に送信してジョブ完了を確認
4. 成功したファイルを `watch_done_dir`（デフォルト: `~/.hondana-push/done/`）に移動
5. エラーが起きても停止せず次のファイルへ進む

```
[watch] /home/user/Desktop/inbox  →  http://192.168.1.100:8200 (table: hondana_chunks)
[watch] done_dir: /home/user/.hondana-push/done  interval: 5s
[watch] Ctrl+C to stop

[send] new_article.md ...
  [running] job a1b2c3d4...
[done]  'AI エージェントの設計' — inserted
```

Ctrl+C で停止する。

---

## オプション一覧

全コマンドで使える。設定ファイルの値より優先される。

| オプション | 説明 | 例 |
|---|---|---|
| `--server URL` | HONDANA サーバーの URL | `--server http://192.168.1.100:8200` |
| `--key KEY` | API キー | `--key hondana-xxxx...` |
| `--table TABLE` | 送信先テーブル名 | `--table work` |
| `--action ACTION` | 重複時の動作 | `--action skip` |

---

## 設定ファイル詳細（~/.hondana-push.yml）

| キー | デフォルト | 説明 |
|---|---|---|
| `server_url` | `http://localhost:8200` | HONDANA サーバーの URL |
| `api_key` | （なし） | API キー。未設定でオープンアクセス |
| `table` | `hondana_chunks` | 送信先テーブル名 |
| `duplicate_action` | `overwrite` | 重複時の動作: `overwrite` / `skip` / `new` |
| `watch_interval` | `5` | デーモンのポーリング間隔（秒） |
| `watch_done_dir` | `~/.hondana-push/done` | 送信済みファイルの移動先 |

### 環境変数でも設定できる

設定ファイルより優先される（CLI オプションが最優先）。

| 環境変数 | 対応するキー |
|---|---|
| `HONDANA_SERVER_URL` | `server_url` |
| `HONDANA_API_KEY` | `api_key` |
| `HONDANA_TABLE` | `table` |

```bash
export HONDANA_SERVER_URL=http://192.168.1.100:8200
export HONDANA_API_KEY=hondana-xxxx...
python hondana_push.py article.md
```

---

## 対応フォーマット

| 形式 | 拡張子 |
|---|---|
| Markdown | `.md` |
| HTML | `.html` `.htm` |
| PDF | `.pdf` |
| Word | `.docx` |
| PowerPoint | `.pptx` |
| Excel / テキスト | `.xlsx` `.txt` |

---

## 重複処理の動作

| `duplicate_action` | 動作 |
|---|---|
| `overwrite`（デフォルト） | 同じ URL またはタイトルが存在する場合、内容が変わっていれば上書きする。変わっていなければスキップ。 |
| `skip` | 既存のものを保持してスキップする |
| `new` | 重複を無視して別記事として新規登録する |

---

## デーモンをシステム起動時に自動実行する

### Linux（systemd ユーザーサービス）

```ini
# ~/.config/systemd/user/hondana-push.service
[Unit]
Description=HONDANA Push Watcher

[Service]
ExecStart=python3 /home/user/bin/hondana_push.py watch /home/user/Desktop/inbox
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now hondana-push

# ログ確認
journalctl --user -u hondana-push -f
```

### macOS（launchd）

```xml
<!-- ~/Library/LaunchAgents/HONDANA.push.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>HONDANA.push</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/username/bin/hondana_push.py</string>
    <string>watch</string>
    <string>/Users/username/Desktop/inbox</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/hondana-push.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/hondana-push.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/HONDANA.push.plist

# ログ確認
tail -f /tmp/hondana-push.log
```

### Windows（タスクスケジューラ）

PowerShell で：

```powershell
$action = New-ScheduledTaskAction `
  -Execute "python" `
  -Argument "C:\Users\username\bin\hondana_push.py watch C:\Users\username\Desktop\inbox"

$trigger = New-ScheduledTaskTrigger -AtLogOn

Register-ScheduledTask `
  -TaskName "hondana-push" `
  -Action $action `
  -Trigger $trigger `
  -RunLevel Highest
```

---

## トラブルシューティング

### 接続できない

```
[error] <urlopen error [Errno 111] Connection refused>
```

→ `server_url` が正しいか確認する。HONDANA サーバーが起動しているか確認する。

```bash
python hondana_push.py config   # server_url を確認
curl http://192.168.1.100:8200/api/stats   # サーバーに直接疎通確認
```

### 認証エラー

```
[error] HTTP 401: {"detail": "Invalid or missing API key"}
```

→ `api_key` が正しいか確認する。HONDANA WebUI で新しいキーを生成して設定し直す。

### ファイルが送られない（デーモン）

- `watch_done_dir` にすでに移動済みでないか確認する
- 拡張子が対応フォーマットに含まれているか確認する（`.md` `.pdf` `.docx` `.pptx` `.html` `.txt` `.xlsx`）
- `python hondana_push.py config` でフォルダパスが正しいか確認する

### ジョブがエラーになる

```
[error] Failed to fetch URL: ...
```

→ URL が公開されていてアクセス可能か確認する。ログインが必要なページは取得できない。

```
[error] Unsupported file format: .xxx
```

→ 対応していない拡張子のファイル。対応フォーマット一覧を確認する。
