# ローカルで動く Zendesk MCP Server

Claude Desktop / Claude Code などから Zendesk を直接操作するための MCP サーバーです。
サーバー本体は Python 3.12 以上の標準ライブラリのみで動作します。氏名・住所マスクの
NER 層だけが外部ライブラリを使いますが、`.mcpb` でインストールする場合は
Claude Desktop の UV ランタイムが Python ごと自動で用意するため、事前準備は不要です
（未導入でも縮退モードで動作します）。

---

## ツール一覧

### チケット系

| ツール名 | できること |
|---|---|
| `search_tickets` | キーワード・ステータス・タグ・担当者・日付でチケット検索（1000件超は Export API 使用） |
| `get_ticket` | チケット詳細 + カスタムフィールド + コメント一覧を取得。コメントごとに公開返信/社内メモと投稿者（顧客/エージェント/Claude）を表示。1件あたりの文字数は `max_body_chars`（既定 800、0 で全文） |
| `list_tickets` | 最近のチケット一覧（タグ・日付絞り込み可、傾向分析向け） |
| `get_ticket_stats` | ステータス別の件数集計 |
| `suggest_reply` | 返信案生成のためのコンテキストを組み立て |
| `add_internal_note` | チケットに社内メモ（非公開コメント）を追加。`kind: "draft"` で返信下書きとして残せる。**設定でオンにしたときだけ利用可能**（下記「社内メモ投稿・記事公開」参照） |
| `compare_draft_replies` | 返信下書きと、その後にエージェントが実際に送った公開返信を組にして、類似度と両方の本文を返す |

### ナレッジベース系

| ツール名 | できること |
|---|---|
| `search_kb_articles` | KB記事をキーワードで全文検索 |
| `list_kb_articles` | KB記事一覧（セクション絞り込み可） |
| `get_kb_article` | 記事IDを指定して本文全体を取得 |
| `list_kb_categories` | カテゴリ一覧を取得 |
| `list_kb_sections` | セクション一覧を取得（カテゴリ絞り込み可） |
| `create_kb_article` | 記事を下書きとして新規作成 |
| `update_kb_article` | 既存記事のタイトル・本文・下書き状態を更新（公開は設定でオンにしたときだけ） |

### CSAT系

| ツール名 | できること |
|---|---|
| `get_csat_rating` | チケットIDを指定してCSAT評価・コメント・bad理由を取得 |

### ユーザー系

| ツール名 | できること |
|---|---|
| `get_user` | ユーザーID またはメールアドレスでユーザー情報・カスタムフィールドを取得 |
| `list_user_fields` | ユーザーカスタムフィールドの定義一覧を取得 |

---

## セットアップ

### 1. Zendesk に OAuth クライアントを作成（管理者が一度だけ）

認証は OAuth（Authorization Code + PKCE）です。利用者はそれぞれ自分の Zendesk アカウントで
ログインして認可するため、API トークンを配布する必要はありません。操作は利用者本人の権限で行われます。

**作成場所:**
Zendesk 管理画面 → アプリおよびインテグレーション → API → OAuth クライアント → OAuth クライアントを追加

| 項目 | 設定値 |
|---|---|
| クライアントの種類 | **パブリック**（シークレットを持たず PKCE で保護） |
| 識別子 | 任意（例: `zendesk_mcp`）。これが `ZENDESK_OAUTH_CLIENT_ID` になります |
| リダイレクト URL | `http://127.0.0.1:47823/callback` |
| 許可されるスコープ | `read` と `write`（KB 記事の作成・更新に write が必要） |

### 2. 設定を環境変数に設定

2つの環境変数を shell の設定（`~/.zshrc` / `~/.bashrc` / direnv など）に追加します。
どちらも秘密情報ではありません。

```sh
export ZENDESK_SUBDOMAIN="yoursubdomain"
export ZENDESK_OAUTH_CLIENT_ID="zendesk_mcp"
```

**初回の認可**

最初にツールを実行するとブラウザで Zendesk の認可画面が開きます。ログインして「許可」を押すと
トークンが保存され、そのままツールが実行されます（約45秒以内に完了しなかった場合は、許可した後に
もう一度実行してください。ブラウザが開かない場合は、ツールの応答に表示される URL を開きます）。

- トークンは `~/.config/zendesk-mcp/token-<subdomain>.json`（パーミッション 600）に保存されます。
- アクセストークンの期限が切れると refresh token で自動更新します。refresh token も失効した場合
  （既定で 30 日間未使用など）は、再びブラウザでの認可を求めます。
- 認可をやり直したい場合はこのファイルを削除します。

**任意の環境変数**

| 変数 | 既定値 | 用途 |
|---|---|---|
| `ZENDESK_OAUTH_SCOPES` | `read write` | 要求するスコープ。読み取り専用で使うなら `read` |
| `ZENDESK_OAUTH_REDIRECT_PORT` | `47823` | コールバックを待ち受けるポート。変える場合は OAuth クライアントのリダイレクト URL も合わせて変更 |
| `ZENDESK_OAUTH_TOKEN_CACHE` | `~/.config/zendesk-mcp/token-<subdomain>.json` | トークンの保存先 |

#### PII マスキング（任意）

クライアント（Claude）に返す応答から個人情報を自動でマスクします。**デフォルトで有効**です。

**メールアドレス・電話番号（span 置換）**

- 検出した箇所だけを `[EMAIL]` / `[PHONE]` に置換します。
- `get_user` の名前・メールは `[NAME]` / `[EMAIL]` に置換されます。
- ユーザーカスタムフィールドのうち住所系のキー（`address` / `zip` / `住所` 等）は
  `[ADDRESS]` に置換されます。
- `get_ticket` のカスタムフィールドのうち、氏名を保持するもの（「名前」「氏名」
  「お名前」）の値は `[NAME]`、住所系の表示名のものは `[ADDRESS]` に置換されます。
  自由記述の値は氏名・住所の文単位 redaction を通します。選択肢・数値・日付の項目と、
  シリアル番号のような英数字だけの値はそのまま返します（メール・電話番号は置換されます）。

**氏名・住所（文単位の redaction）**

氏名・住所は「検出漏れがそのまま規程違反になる」ため、span 置換ではなく
**危険と判定した文を丸ごと落とす**方式を採ります。落とした文は、何を検出したかが
分かるプレースホルダに置き換わります。

```
入力: お世話になっております。田中です。至急確認をお願いします。
      先日購入した製品が届きません。
      配送先は東京都渋谷区神南1-2-3です。

出力: お世話になっております。[NAME]至急確認をお願いします。
      先日購入した製品が届きません。
      [ADDRESS]
```

問い合わせの本題（「製品が届かない」「至急」）は残るため、傾向分析や返信案生成には
引き続き利用できます。

**プレースホルダの意味**

| プレースホルダ | 意味 |
|---|---|
| `[NAME]` | 人名を検出したため、その文を落とした |
| `[ADDRESS]` | 住所を検出したため、その文を落とした |
| `[ADDRESS][NAME]` | 同じ文に人名と住所の両方があった |
| `[REDACTED]` | 種別を判定できないまま落とした（NER 未導入時の縮退動作） |

いずれも**文全体が落ちている**点は共通で、プレースホルダは「なぜ落としたか」を示します。
`[NAME]` が付いていても、その文に含まれていた他の情報も一緒に失われています。
同じプレースホルダが連続する場合は 1 個に畳まれます。

**NER 層（任意依存）**

判定の第1層に日本語 NER（spaCy の `ja_core_news_md`）を使います。
**未導入でも動作します**が、その場合は安全側に倒して**自由記述本文（コメント本文・
件名・CSAT コメント）をすべて `[REDACTED]`** にする縮退動作になります。
構造化フィールド（ステータス・タグ・日付・ID）とナレッジベース系ツールは
縮退時も通常どおり利用できます。

**`.mcpb` からインストールする場合、利用者側の作業は不要です。**

このバンドルは `server.type: "uv"`（`manifest_version` 0.4）を使っています。
Claude Desktop がインストール時に uv を自動取得し、`pyproject.toml` / `uv.lock` に
従って `uv sync` を実行して専用の仮想環境を作ります。**Python 自体も uv が
ダウンロードする**ため、利用者が Python を用意する必要もありません。
インストール時に依存を取得する旨の確認ダイアログが表示されます。

バンドル本体は約 66kB です（依存はインストール時に取得されます）。

手動で `server.py` を実行する場合（Claude Code プラグイン等）は pip で導入します。

```sh
python3 -m pip install -r requirements-ner.txt
```

起動時に stderr へどちらのモードで動作しているかを出力します。

```
[zendesk-mcp] 氏名・住所 redaction: NER 有効 (model=ja_core_news_md)
```

**対応プラットフォーム**

| | 結果 |
|---|---|
| macOS arm64 / x86_64 | ✅ NER 有効 |
| Windows x86_64 | ✅ NER 有効 |
| Windows arm64 | ⚠️ 縮退モード |
| Linux | ✅ NER 有効 |

Python のバージョンは `pyproject.toml` の `requires-python = ">=3.12,<3.14"` に従い
uv が選びます。3.14 を除外しているのは spaCy 3.8.x に cp314 wheel が無く、モデルの
要件が `spacy>=3.8.0,<3.9.0` のため上位版にも移れないためです。

Windows arm64 は spacy / blis / SudachiPy が win_arm64 wheel を提供していないため、
`pyproject.toml` の環境マーカーで NER 依存を除外しています。これにより
インストール自体は成功し、起動後に縮退モード（自由記述を全て `[REDACTED]`）に
落ちるだけで済みます。

対応モデルは `ja_core_news_lg` / `md` / `sm` で、精度の高い順に探索して最初に
読めたものを使います。推奨は **`md`**（40MB）です。

| モデル | ents_f | recall | 単語ベクトル | サイズ |
|---|---|---|---|---|
| `ja_core_news_sm` | 0.6087 | 0.5547 | なし | 12MB |
| **`ja_core_news_md`** | **0.7043** | **0.6755** | chiVe 300次元 | **40MB** |
| `ja_core_news_lg` | 0.7223 | 0.6969 | chiVe 300次元 | 530MB |

`sm` は単語ベクトルを持たないため未知語（珍しい姓・難読地名）に弱く、この機能で
最も重要な部分が落ちます。`lg` は 530MB に対して `md` より +0.018 しか伸びません。

> **NOTE: `ja-ginza` は採用していません。** パッケージは MIT 宣言ですが、wheel 内の
> `meta.json` に学習元として UD_Japanese-BCCWJ（CC BY-NC-SA 4.0）と GSK2014-A
> （個別に定める商用ライセンス）が記載されています。NonCommercial 条項は再配布だけで
> なく商用利用自体を制限するため、業務利用およびバンドルへの同梱配布に適しません。
> `ja_core_news_md` / `lg` は `ja-ginza` と同一の chiVe ベクトルを使うため未知語への
> 一般化能力は同等で、ライセンスは CC BY-SA 4.0（NC 制限なし）です。

- ナレッジベース記事本文（`get_kb_article` 等）はマスク対象外です。

**既知の限界**

- 姓辞書に無い珍しい姓や難読地名は、NER が取りこぼすと検出できません
  （NER 未導入時は自由記述を全落としするため、この漏れは発生しません）。
- 地名を伴わない番地のみの表記（例: 単独の `1-2-3`）は、日付・バージョン番号との
  衝突を避けるため住所とみなしません。個人を特定しないためです。
- 全角数字の電話番号、`0081` 形式、IDN メールは検出しません（既存の制限）。

マスクを無効化するには、環境変数 `ZENDESK_MASK_PII` に `0` または `false` を設定します。

```sh
export ZENDESK_MASK_PII="0"   # 未設定・その他の値は ON（マスク有効）
```

> **⚠️ 警告**: `ZENDESK_MASK_PII=0` にすると、氏名・住所・メールアドレス・電話番号が
> そのまま LLM へ送信されます。社内規程や顧客との契約に違反する可能性があります。
> 無効化した状態では起動時 stderr に警告が出力され、各ツールの応答先頭にも
> 注意文が挿入されます。誤マスクの調査など、一時的な用途に限って使用してください。

#### 社内メモ投稿・記事公開（任意・デフォルト無効）

誤爆すると顧客に影響しうる書き込み（チケットへの社内メモ投稿、ヘルプ記事の公開）は、
環境変数 `ZENDESK_ENABLE_SENSITIVE_WRITES` を `1` / `true` にしたときだけ有効になります
（`.mcpb` では設定画面の「社内メモ投稿・記事公開を許可」）。無効の間は `add_internal_note` は
ツール一覧に表示されず、`update_kb_article` の `draft: false`（公開）も拒否されます。

`add_internal_note` の誤爆対策:

- **公開返信は送れません。** `public: false` をコードで固定しており、切り替える引数はありません。
- **投稿後に非公開を確認します。** Zendesk の監査ログで、作成されたコメントが非公開であることを確かめ、
  公開として記録されていた場合はエラーにします。
- Zendesk のロール（カスタムロール・Light Agent）で社内メモのみに制限しておくと、さらに安全です。

**返信下書きとして残す（`kind: "draft"`）**

`add_internal_note` は既定では通常の社内メモ（`kind: "memo"`）として投稿します。顧客への返信の
下書きを残すときだけ `kind: "draft"` を指定すると、次の 2 つが付きます。

- 本文の先頭に `【返信下書き】`（同じチケット内の通常メモと区別するコメント単位の目印）
- チケットに `claude_draft` タグ（下書きのあるチケットをまとめて探すため。既存タグは消しません）

`compare_draft_replies` は、この下書きと「その後に最初に送られたエージェントの公開返信」を組にして、
類似度（空白を無視した文字列の一致率、0〜100%）と両方の本文を返します。顧客・エンドユーザー（CC 等）・
投稿者に設定した Claude ユーザーの公開コメントは返信とみなしません。返信の前に新しい下書きが
投稿された場合、前の下書きは「差し替え」、まだ返信が無いものは「未返信」として件数だけ数えます。
`ticket_id` を指定するとそのチケットだけ、省略すると `claude_draft` タグのチケットを更新日の新しい順に
調べます（`updated_after` / `updated_before` で期間を絞れます）。Zendesk の検索インデックスへの反映には
数分かかるため、下書きを投稿した直後はタグ検索で見つからないことがあります（`ticket_id` 指定なら即時に調べられます）。

**投稿者の変更（任意）**

Claude が書いたメモだと分かるように、投稿者を別の Zendesk ユーザー（例: Claude 用に作った
「Claude (AI)」エージェント）にできます。環境変数 `ZENDESK_NOTE_AUTHOR_ID` にそのユーザーの ID を
指定します（`.mcpb` では設定画面の「社内メモの投稿者ID」）。未設定ならログイン中の本人が投稿者です。

- 指定できるのはエージェント/管理者だけです。エンドユーザーは社内メモを書けないため、
  投稿前にロールを確かめ、エージェント/管理者でなければ投稿しません。
- 投稿者を変えても、チケットの更新履歴には**ログイン中の本人が更新者として残ります**。
- 他人名義でコメントする権限が無い場合、Zendesk 側で投稿者がログイン中の本人になることがあります。
  その場合はツールの応答で警告します。

```sh
export ZENDESK_ENABLE_SENSITIVE_WRITES="1"   # 未設定・その他の値は無効
export ZENDESK_NOTE_AUTHOR_ID="1234567890"   # 任意。社内メモの投稿者にするユーザーID
```

### 3. インストール

#### 方法A: Claude Code プラグイン（推奨）

このリポジトリ自身がプラグインのマーケットプレイスになっています。
Claude Code で以下を実行します。

```
/plugin marketplace add OhshiroC/zendesk_mcp_server
/plugin install zendesk-mcp-server@zendesk-mcp
```

インストール後、セッションを開始すると MCP サーバーが起動し、ツールが利用可能になります
（すでにセッション中の場合は `/reload-plugins`）。
`python3` が PATH 上にあり、Python 3.12 以上であることが前提です。

#### 方法B: Claude Desktop に手動登録

`claude_desktop_config.json` の内容を Claude Desktop の設定ファイルにマージします。

**設定ファイルの場所:**
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "zendesk": {
      "command": "python",
      "args": ["/Users/ユーザー名/mcp-servers/zendesk/server.py"],
      "env": {
        "ZENDESK_SUBDOMAIN": "yoursubdomain",
        "ZENDESK_OAUTH_CLIENT_ID": "your_oauth_client_identifier"
      }
    }
  }
}
```

`/Users/ユーザー名/mcp-servers/zendesk/server.py` は実際のフルパスに変更してください。
設定後、Claude Desktop を再起動するとツールが有効になります。

#### 方法C: .mcpb バンドル（Claude Desktop 拡張機能）

`.mcpb` バンドルをビルドすると、Claude Desktop の拡張機能としてワンクリックでインストールでき、
Zendesk の接続設定を **設定画面（GUI）から入力**できます（環境変数を手で設定する必要がありません）。

**ビルドの前提条件**
- Node.js / `npx` が利用可能であること（初回ビルド時に `@anthropic-ai/mcpb` を npm から取得するためネットワーク接続が必要）
- `make` が利用可能であること

**ビルド手順**

```sh
make mcpb
```

`dist/zendesk-mcp-server.mcpb` が生成されます（`make validate` で manifest 検証のみ、`make clean` で生成物削除）。

**インストール手順**
1. 生成された `dist/zendesk-mcp-server.mcpb` を Claude Desktop のウィンドウにドラッグ＆ドロップする
   （または 設定 → Extensions から `.mcpb` ファイルを指定してインストールする）。
2. インストール時に表示される設定画面で、以下を入力する:
   - **Zendesk Subdomain**: `yourcompany`（`https://yourcompany.zendesk.com` の `yourcompany` 部分）
   - **Zendesk OAuth Client ID**: 手順1で作成した OAuth クライアントの識別子
   - **PII マスキング**（任意）: 個人情報（氏名・住所・メールアドレス・電話番号）のマスクを有効/無効にします。**デフォルトは有効**。オフにすると生の値がそのまま LLM へ送信されます（規程違反のリスクがあります）。
   - **社内メモ投稿・記事公開を許可**（任意）: チケットへの社内メモ投稿とヘルプ記事の公開を許可します。**デフォルトは無効**。
   - **社内メモの投稿者ID**（任意）: 社内メモの投稿者にする Zendesk ユーザーの ID。空欄ならログイン中の本人。
3. インストール時に「依存関係を取得する」旨の確認ダイアログが表示されるので許可する。
4. 有効化するとツールが利用可能になります。初回のツール実行時にブラウザで Zendesk の認可画面が開きます。

**Python の準備は不要です**

`manifest_version` 0.4 の UV ランタイム（`server.type: "uv"`）を使っているため、
Claude Desktop が uv を自動取得し、`pyproject.toml` / `uv.lock` に従って専用の仮想環境を
作ります。**Python 本体も uv がダウンロードする**ので、お使いの PC に Python が
入っている必要はありません。以前必要だった **Python Path** の設定項目は廃止しました
（macOS で Finder 起動時に PATH が最小限になり Homebrew の `python3` が見つからない、
という問題も起きなくなります）。

初回インストール時は Python と依存関係のダウンロードが走るため時間がかかります。

---

## 使用例

```
「直近のオープンチケットの傾向を分析して」
→ list_tickets → Claude が傾向を分析

「Wi-Fi接続エラーに関するチケットを探して」
→ search_tickets(keyword="Wi-Fi") を自動実行

「チケット #12345 の返信案を作って」
→ suggest_reply → Claude が返信文を生成

「チケット #12345 の顧客情報を見せて」
→ get_ticket → get_user を自動実行

「チケット #12345 のCSAT評価を確認して」
→ get_csat_rating を自動実行

「ログイン関連のKB記事を検索して」
→ search_kb_articles を自動実行

「KB記事の構成（カテゴリ・セクション）を教えて」
→ list_kb_categories → list_kb_sections

「セクション ID:xxxxx に記事を下書きで作成して」
→ create_kb_article を自動実行

「記事 ID:xxxxx のタイトルと本文を更新して」
→ update_kb_article を自動実行
```

---

## ファイル構成

```
zendesk-mcp-server/
├── server.py                    # MCPサーバー本体（これだけあれば動きます）
├── manifest.json                # MCPB バンドル用マニフェスト（方法C・GUI設定定義）
├── Makefile                     # `make mcpb` で .mcpb をビルド
├── .mcpbignore                  # .mcpb パッケージングの除外設定
├── .mcp.json                    # プラグイン用の MCP サーバー起動定義
├── .claude-plugin/
│   ├── plugin.json              # プラグインマニフェスト
│   └── marketplace.json         # このリポを1プラグインのマーケットとして定義
├── .env.example                 # 環境変数のテンプレート
├── claude_desktop_config.json   # Claude Desktop 設定サンプル（手動登録用）
└── README.md
```