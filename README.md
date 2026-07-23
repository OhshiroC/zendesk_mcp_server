# ローカルで動く Zendesk MCP Server

Claude Desktop / Claude Code などから Zendesk を直接操作するための MCP サーバーです。
Python 3.12 標準ライブラリで動作します。

---

## ツール一覧

### チケット系

| ツール名 | できること |
|---|---|
| `search_tickets` | キーワード・ステータス・タグ・担当者・日付でチケット検索（1000件超は Export API 使用） |
| `get_ticket` | チケット詳細 + コメント一覧を取得 |
| `list_tickets` | 最近のチケット一覧（タグ・日付絞り込み可、傾向分析向け） |
| `get_ticket_stats` | ステータス別の件数集計 |
| `suggest_reply` | 返信案生成のためのコンテキストを組み立て |

### ナレッジベース系

| ツール名 | できること |
|---|---|
| `search_kb_articles` | KB記事をキーワードで全文検索 |
| `list_kb_articles` | KB記事一覧（セクション絞り込み可） |
| `get_kb_article` | 記事IDを指定して本文全体を取得 |
| `list_kb_categories` | カテゴリ一覧を取得 |
| `list_kb_sections` | セクション一覧を取得（カテゴリ絞り込み可） |
| `create_kb_article` | 記事を下書きとして新規作成 |
| `update_kb_article` | 既存記事のタイトル・本文・下書き状態を更新 |

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

### 1. Zendesk APIトークンを発行

**発行場所:**
Zendesk管理画面 → Apps and integrations → APIs → Zendesk API → Add API token

### 2. 認証情報を環境変数に設定

3つの環境変数を shell の設定（`~/.zshrc` / `~/.bashrc` / direnv など）に追加します。
プラグインはこれらの値を保持せず、起動時に環境変数から読み込みます。

```sh
export ZENDESK_SUBDOMAIN="yoursubdomain"
export ZENDESK_EMAIL="you@example.com"
export ZENDESK_API_TOKEN="xxxxxxxxxxxxxxxxxxx"
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
        "ZENDESK_EMAIL": "you@example.com",
        "ZENDESK_API_TOKEN": "xxxxxxxxxxxxxxxxxxx"
      }
    }
  }
}
```

`/Users/ユーザー名/mcp-servers/zendesk/server.py` は実際のフルパスに変更してください。
設定後、Claude Desktop を再起動するとツールが有効になります。

#### 方法C: .mcpb バンドル（Claude Desktop 拡張機能）

`.mcpb` バンドルをビルドすると、Claude Desktop の拡張機能としてワンクリックでインストールでき、
Zendesk の認証情報を **設定画面（GUI）から入力**できます（環境変数を手で設定する必要がありません）。

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
   - **Zendesk Email**: ログインに使うメールアドレス
   - **Zendesk API Token**: Zendesk 管理画面で発行した API トークン
   - **Python Path**（任意）: 通常は空欄のままで `python3` が使われます。
3. 有効化するとツールが利用可能になります。

**注意: Python の実行パスについて**
- このサーバーは Python 3.12 以上（標準ライブラリのみ）で動作します。Claude Desktop は Python を同梱しないため、
  お使いの PC の Python を使用します。
- **macOS では、Finder / Dock から起動した Claude Desktop の PATH が最小限**（`/usr/bin` など）になり、
  Homebrew や pyenv で入れた `python3` が見つからず起動に失敗することがあります。
  その場合は設定画面の **Python Path** に **フルパス**を指定してください（例: `/opt/homebrew/bin/python3`）。
  フルパスは `which python3` で確認できます。

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