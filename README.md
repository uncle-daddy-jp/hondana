# HONDANA — 個人向け RAG 知識ベース

記事・ドキュメント・URL を放り込むと、本文を抽出してベクトル検索可能な知識ベースに蓄積し、
自然言語での検索・回答生成、そして **MCP 経由で AI エージェントの知識ライブラリ** として使える
セルフホスト型の RAG システムです。（HONDANA＝「本棚」）

- 取り込み: URL / PDF / Word / Excel / PowerPoint / Markdown / テキスト / Reddit
- 検索: ベクトル検索・ハイブリッド検索（ベクトル＋全文）・タグ/日付/URL フィルタ・新着優先
- 連携: REST API ＋ MCP サーバ（Claude Code 等のエージェントから直接利用）
- 運用: テーブル（名前空間）・重複排除・保持ポリシー・バックアップ

---

## 主な機能

- **多形式取り込み＋抽出フォールバック** — URL は requests+trafilatura →（JS ページは）Playwright →
  Jina Reader の順でフォールバック。PDF/Office も本文＋見出しを抽出。Reddit は本文＋コメントを取得。
- **3 層チャンク** — L1=記事サマリー / L2=セクション（見出し単位）/ L3=段落（オーバーラップ付き）。
  検索ヒットした L3 には親 L2 を自動付与して文脈ごと返す。
- **ハイブリッド検索（RRF）** — ベクトル検索と全文（キーワード）検索を Reciprocal Rank Fusion で融合。
  固有名詞・完全一致語に強くなる。`hybrid: true` で有効化。
- **メタデータフィルタ** — タグ（AND/OR/除外）・相対日付（直近 N 日）・`source_url` 前方一致で絞り込み。
- **新着優先（recency decay）** — `recorded_at` による時間減衰で新しい記事を優先（半減期を指定可）。
- **出典付き結果** — 各チャンクに `chunk_id` / `section_id` / `section_heading` / `position` /
  `source_type` / `source_url` / `recorded_at` を含み、エージェントが正確に引用できる。
- **MCP サーバ** — `search_knowledge` ほか 10 ツールをエージェントへ公開。検索フィルタや保存時の重複制御も可能。
- **テーブル（名前空間）** — 用途別にコレクションを分離（横断検索も可）。
- **永続ジョブキュー** — 取り込みは非同期。SQLite(WAL) に永続化し、再起動後も継続。レート制限も自動退避。
- **重複排除・保持・バックアップ** — URL/タイトル/ハッシュでの重複排除、条件付き一括削除、
  Qdrant スナップショット＋ジョブ DB のバックアップスクリプト。

---

## アーキテクチャ

```
            ┌──────────── ingest ────────────┐        ┌──────── search ────────┐
  URL/file → fetch+extract → chunk(L1/L2/L3) → embed → Qdrant ← vector + FTS ← query
  (trafilatura/Playwright/    (見出し/段落)    (bge-m3 等)  │        │  ↑ RRF fuse + recency
   Jina/PDF/Office/Reddit)                                 │        └→ 親L2付与 → 回答生成(LLM)
                         非同期ジョブ(SQLite WAL) ─────────┘
```

- **ベクトル/メタデータ**: Qdrant（1 テーブル = 1 コレクション、payload に索引）
- **ジョブキュー**: SQLite（WAL、再起動復帰・リトライ・レート制限退避）
- **埋め込み / LLM**: `config.yml` で provider/model/URL を差し替え可
  - 埋め込み: ローカル `sentence-transformers`（既定 `intfloat/multilingual-e5-small`）または
    リモート OpenAI 互換エンドポイント（例: vLLM の `bge-m3`、`embedding_url` 指定時）
  - LLM: Claude / Groq / OpenAI 互換ローカル（vLLM・llama.cpp 等）
- **サービス（docker compose）**: `qdrant` / `api` / `webui`(Streamlit) / `scheduler`

---

## クイックスタート

```bash
git clone <repo> hondana && cd hondana
cp .env.example .env                 # APIキー等を設定（Claude/Groq を使う場合）
cp config.example.yml config.yml     # モデル・URL・検索設定を調整
docker compose up -d
# API:   http://localhost:8200   (OpenAPI: /docs)
# WebUI: http://localhost:8601
```

- 埋め込みをローカルで動かす場合、初回は PyTorch・モデル(~数百MB)のダウンロードで時間がかかる。
- Playwright のブラウザは API イメージに含まれるが、ローカル実行する場合は
  `playwright install chromium --with-deps` が必要。

---

## 設定（config.yml）

`config.example.yml` をコピーして編集。変更は `POST /api/config` でホットリロードされる（再起動不要）。
API キーなどのシークレットは config ではなく `.env` に置く（[環境変数](#環境変数env)）。

### LLM・埋め込みをクラウド↔ローカルで切り替える

LLM も埋め込みも **`config.yml` の provider/model/URL を書き換えるだけ**。コード変更・再ビルド不要
（変更は `POST /api/config` でホットリロード）。`provider` は `claude` / `groq` / `openai_compat` の3種で、
`openai_compat` は OpenAI 互換 API を話すローカル/社内エンドポイント全般（vLLM・Ollama・llama.cpp 等）。

| 用途 | キー | クラウド | ローカル（自前エンドポイント） |
|---|---|---|---|
| 回答生成 | `llm_answer_provider` / `_model` / `_url` | `claude`（要 `.env` の `ANTHROPIC_API_KEY`） | `openai_compat` ＋ `_url`（キー不要） |
| 要約・クエリ展開 | `llm_summary_provider` / `_model` / `_url` | **省略可＝回答生成を継承**。`groq` 等に分けることも可 | **省略可＝回答生成を継承**。別エンドポイントなら設定 |
| 埋め込み | `embedding_model` / `embedding_url` | — | `embedding_url` 未設定＝ローカル `sentence-transformers`。設定するとリモート OpenAI 互換（例 vLLM `bge-m3`） |

#### ローカル LLM で動かす（最短手順）

回答生成を自前エンドポイントに向けるだけ。要約・クエリ展開は自動で同じ設定を継承するので
`llm_summary_*` は書かなくてよい。**ローカル構成は API キー不要**（`.env` の `ANTHROPIC_API_KEY`/`GROQ_API_KEY` は空で可）。

```yaml
# config.yml — この3行を自分のエンドポイントに
llm_answer_provider: openai_compat
llm_answer_model: your-local-model        # サーバが返すモデル名（例 gemma4-26b / qwen3）
llm_answer_url: http://your-llm-host:8000
# llm_answer_thinking_budget: 512         # llama.cpp で思考を使うなら（vLLM 系は無視）
```

埋め込みもローカル GPU に載せるなら `embedding_url`（＋ 例 `embedding_model: bge-m3`）を追加する。

- 既定（テンプレートのまま）は **回答=Claude / 埋め込み=ローカル e5-small**。最小構成は `.env` に
  `ANTHROPIC_API_KEY` を入れるだけで動く（要約・クエリ展開も Claude を継承）。
- `llm_summary_*` を設定したときだけ、要約・クエリ展開を別エンジンにできる（例: 取り込み要約を無料 Groq に逃がす）。

### 主な設定キー

| キー | 既定 | 説明 |
|---|---|---|
| `embedding_model` | `intfloat/multilingual-e5-small` | 埋め込みモデル |
| `embedding_url` | （未設定） | 設定するとリモート OpenAI 互換埋め込み（例 vLLM bge-m3） |
| `llm_answer_provider` / `_model` / `_url` | `claude` / Haiku | 回答生成 LLM（要約・クエリ展開も未設定ならこれを継承） |
| `llm_summary_provider` / `_model` / `_url` | （未設定＝`llm_answer_*` 継承） | 要約・クエリ展開を別エンジンにするときだけ設定 |
| `enable_llm_summary` | `false` | LLM サマリー/タグ生成（品質↑・取り込み遅延あり） |
| `search_distance_threshold` | `0.3` | 類似度しきい値 |
| `search_hybrid_enabled` | `false` | ハイブリッド検索（ベクトル＋全文 RRF）既定 |
| `search_rrf_k` | `60` | RRF 定数 |
| `search_recency_decay_enabled` | `false` | 新着優先（時間減衰）既定 |
| `search_recency_half_life_days` / `_floor` | `180` / `0.5` | 減衰の半減期 / 下限 |
| `worker_count` / `max_retries` | `4` / `3` | 取り込みワーカー数 / リトライ |
| `failed_job_retention_days` | `7` | 失敗ジョブの自動削除日数 |

> 検索強化フラグ（hybrid / recency）は **既定 OFF**。リクエスト単位でも上書きできる。
> **埋め込みモデルを変更したら DB 全体の再取り込みが必要**（次元/ベクトルが変わり互換性が失われる。[制限事項](#制限事項)参照）。

---

## 環境変数（.env）

シークレットとホスト側パスは `.env`（`.env.example` をコピー）に置く。`.env` は git 管理外。

| 変数 | 必須 | 説明 |
|---|---|---|
| `ANTHROPIC_API_KEY` | provider=claude 時 | Claude（回答/要約）用 API キー |
| `GROQ_API_KEY` | provider=groq 時 | Groq（要約）用 API キー |
| `HONDANA_INBOX_DIR` | ✓ | 取り込み元フォルダ（ホスト側パス） |
| `HONDANA_DONE_DIR` | ✓ | 取り込み後の移動先（ホスト側パス） |
| `HONDANA_API_KEY` | — | WebUI/MCP 用キー。空＝開放 |
| `QDRANT_URL` | — | 既定 `http://qdrant:6333`（compose 内） |
| `USERPROFILE` | Linux/Mac | HF キャッシュのマウント元。Linux/Mac は自分のホームを設定 |
| `HONDANA_PUBLIC_URL` | — | ブックマークレット用の外部公開 URL（既定 `http://localhost:8200`） |
| `REDDIT_CLIENT_ID` / `_SECRET` / `_USER_AGENT` | 任意 | Reddit 取り込み（PRAW） |
| `X_CLIENT_ID` / `_SECRET` / `X_ACCESS_TOKEN` | 任意 | X/Twitter 取り込み（scheduler サービス） |
| `NAS_HOST` / `NAS_USER` / `NAS_SSH_KEY_HOST` / `NAS_BACKUP_ROOT` / `BACKUP_KEEP` | 任意 | NAS バックアップ（`scripts/backup.py`）。`NAS_HOST` 空でローカルのみ |

> Reddit / X / NAS バックアップは **任意機能**。使わなければ空のままで起動・基本機能に影響しない。

---

## 使い方

### WebUI
`http://localhost:8601` — チャット検索、取り込み、テーブル・記事の管理、設定。

### REST API（主要）
```
POST /api/ingest/url      {url, table, duplicate_action}     # URL を非同期取り込み
POST /api/ingest/file     (multipart)                        # ファイル取り込み
POST /api/ingest/text     {title, text, table}               # テキスト取り込み
POST /api/search          {query, top_k, hybrid, recency_decay, filters, table}
POST /api/ask             {question, ...}                    # 検索＋回答生成
POST /api/save-answer     {question, answer, duplicate_action, agent_id, origin}
GET  /api/articles        ?table=&limit=                     # 一覧
POST /api/articles/bulk-delete {recorded_days_gte, table}    # 条件一括削除（保持運用）
GET  /api/jobs/{id}                                          # ジョブ状態
```

検索フィルタ例:
```bash
curl -s localhost:8200/api/search -H 'Content-Type: application/json' -d '{
  "query": "エージェント設計", "hybrid": true, "recency_decay": true,
  "filters": {"tags_any": ["AI"], "last_n_days": 30, "url_prefix": "https://www."}
}'
```

### MCP（エージェント連携）
`~/.claude/mcp.json` などに登録:
```json
{ "mcpServers": { "hondana": {
  "type": "http", "url": "http://localhost:8200/mcp",
  "headers": { "Authorization": "Bearer <api-key>" }
}}}
```
ツール: `search_knowledge`（メイン・filters/hybrid/recency 対応）/ `get_article` / `get_recent` /
`search_by_tag` / `list_tables` / `list_tags` / `ingest_new` / `save_answer`（duplicate_action/agent_id/origin 対応）/
`append_to_article` / `delete_knowledge`。

---

## 運用

- **テーブル**: `POST /api/tables {name}` で作成（名前は `a-zA-Z0-9_-`）。検索は `table` 省略で全テーブル横断。
- **重複排除**: 取り込み・保存は URL/タイトル/ハッシュで重複を検出（`duplicate_action: skip/overwrite/new`）。
- **保持ポリシー**: `POST /api/articles/bulk-delete {recorded_days_gte: N, table}` で N 日より古い記事を削除。
- **バックアップ**: `python scripts/backup.py`（コンテナ内）で Qdrant スナップショット＋ジョブ DB を保存し、
  設定時は NAS へ rsync（直近 10 世代を保持）。定期実行は OS のスケジューラ等から
  `docker exec hondana-api-1 python scripts/backup.py` を日次で叩く。
- **認証**: `config.yml` の `api_keys` を設定すると `X-API-Key` または `Authorization: Bearer` が必須になる
  （未設定時は開放）。

---

## セキュリティ

**インターネットへ出す前に必ず認証を有効化してください。** 既定値は個人のローカル/LAN 利用向けに開放されています。

- **既定で無認証**: `config.yml` の `api_keys` が空（既定）だと、全 API/MCP を誰でも叩けます。外部公開・リバースプロキシ越しに出す場合は、WebUI の Settings か `api_keys` でキーを設定し、クライアントは `X-API-Key` か `Authorization: Bearer <key>` を送ること。
- **無認証で晒すと削除まで可能**: 読み取り・取り込みだけでなく、`delete_knowledge` / `POST /api/articles/bulk-delete`（一括削除）も開放対象になります。
- **認証免除パス**: ブックマークレット用 `GET /clip` とパーマリンク `GET /k/{id}` はブラウザ都合で常に認証免除。前段（リバースプロキシ等）で保護するか、機微情報を載せないこと。
- **CORS**: 既定で `allow_origins=["*"]`。公開時は `api/main.py` で許可オリジンを絞るのが安全。
- **シークレット**: API キー等は `.env`（git 管理外）にのみ置く。`config.yml` には置かない（Settings 画面・ホットリロードで露出し得るため）。

---

## 制限事項

- 埋め込みモデルを変更すると互換性が失われ、**全再取り込み**が必要（既存ペイロードの再ベクトル化は `api/scripts/migrate_embeddings.py` で補助できる）。
- 記事一覧/ソートは Qdrant スキャンに依存するため、数万記事規模では遅くなり得る（現状は個人利用前提）。
- 取り込みは非同期。投入直後の `GET /api/articles/{id}` は索引反映待ちで一時的に 404 になり得る。
- ハイブリッド/新着優先は既定 OFF。回答生成は LLM 依存のため、保存前に内容を確認すること。
- Jina Reader は外部無料サービス（SLA なし・フォールバック用途）。

---

## 開発

```bash
# アプリ本体は docker 推奨。開発ツールだけ入れる（このプロジェクトは pip パッケージではない）:
pip install ruff black -r api/tests/requirements-test.txt   # ruff/black + pytest/pytest-asyncio
pytest                         # api/tests（pyproject で pythonpath=api を設定済）
ruff check api/ && black api/  # Lint / フォーマット
```

## ライセンス
MIT License — [LICENSE](LICENSE) を参照。
