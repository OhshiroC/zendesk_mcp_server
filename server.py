#!/usr/bin/env python3
"""
Zendesk MCP Server（ローカル実行版）
通信方式: stdio (Claude Desktop / Claude Code 向け)

使い方:
  python server.py

環境変数:
  ZENDESK_SUBDOMAIN
  ZENDESK_EMAIL
  ZENDESK_API_TOKEN  Zendesk 管理画面で発行したAPIトークン
"""

import json
import os
import re
import sys
import ssl
import base64
import urllib.request
import urllib.parse
from datetime import datetime, timezone

# SSL コンテキスト
# SSLCertVerificationError 対策の暫定対応のため、セキュリティ注意
_SSL_CTX = ssl._create_unverified_context()

# ── 環境変数 ──────────────────────────────────────────────
ZENDESK_SUBDOMAIN = os.environ.get("ZENDESK_SUBDOMAIN", "")
ZENDESK_EMAIL     = os.environ.get("ZENDESK_EMAIL", "")
ZENDESK_API_TOKEN = os.environ.get("ZENDESK_API_TOKEN", "")
ZENDESK_BASE      = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2"


# ── PII マスキング ────────────────────────────────────────
# Zendesk から得た応答に含まれる個人情報(メール・電話番号)を固定プレースホルダに
# 置換してクライアントに返す。CLAUDE.md の Functional Core 方針に従い、マスク処理は
# 副作用のない純粋関数として実装し、ON/OFF の判定は呼び出し側(チョークポイント)で行う。

def _parse_mask_pii_env(value: str | None) -> bool:
    # 未設定(None) や不明値は安全側に倒して ON とみなす。明示的な OFF のみ False。
    if value is None:
        return True
    return value.strip().lower() not in ("0", "false")


# デフォルト ON。ZENDESK_MASK_PII=0 / false でマスクを無効化できる。
MASK_PII = _parse_mask_pii_env(os.environ.get("ZENDESK_MASK_PII"))

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-']+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# 電話番号は形式別パターンで最低桁数を保証する。セパレータには \s を使わない
# (\s は \n にマッチし、改行を跨いだ誤マッチ — 例: 行末の日付 07-23 と次行先頭の 0120 —
#  を起こすため)。リテラルスペースのみ許可する。
_INTL_PHONE_RE = re.compile(r"\+81[\- ]?\d{1,4}[\- ]?\d{1,4}[\- ]?\d{4}(?!\d)")
_NATIONAL_PHONE_RES = [
    re.compile(r"(?<![#\d])0[5-9]0[\- ]?\d{4}[\- ]?\d{4}(?!\d)"),               # 携帯 11桁
    re.compile(r"(?<![#\d])0120[\- ]?\d{3}[\- ]?\d{3}(?!\d)"),                  # フリーダイヤル 0120 10桁
    re.compile(r"(?<![#\d])0800[\- ]?\d{3}[\- ]?\d{4}(?!\d)"),                  # フリーダイヤル 0800 11桁
    re.compile(r"(?<![#\d])\(0[1-9]\d{0,3}\)[\- ]?\d{2,4}[\- ]?\d{4}(?!\d)"),   # 括弧付き市外局番
    re.compile(r"(?<![#\d])0[1-9]\d{2}[\- ]?\d{2}[\- ]?\d{4}(?!\d)"),           # 固定 4桁市外局番 10桁
    re.compile(r"(?<![#\d])0[1-9]\d[\- ]?\d{3}[\- ]?\d{4}(?!\d)"),              # 固定 3桁市外局番 10桁
    re.compile(r"(?<![#\d])0[1-9][\- ]?\d{4}[\- ]?\d{4}(?!\d)"),                # 固定 2桁市外局番 10桁
]


# 既知の制限(意図的な検出漏れ): 全角数字の電話番号(０９０…)、0081 形式の国際表記、
# IDN(非ASCII)メールは検出しない。全文の全角→半角正規化は PII でない全角数字まで
# 書き換えてしまい、設計原則「Precision over Recall(誤検出の悪影響 > 検出漏れ)」に反するため。
def _mask_intl_phone(m: "re.Match") -> str:
    # +81 以降の桁数が 9-10 桁のときだけ電話番号とみなす(短すぎる +81XXXX は誤検出)
    digits = sum(c.isdigit() for c in m.group()[3:])
    return "[PHONE]" if 9 <= digits <= 10 else m.group()


def mask_pii_text(text: str) -> str:
    # この関数は MASK_PII を参照せず、常にマスクを実行する純粋関数。
    # メールを先に置換することで、メール内の数字列が電話番号として誤検出されるのを防ぐ。
    text = _EMAIL_RE.sub("[EMAIL]", text)
    text = _INTL_PHONE_RE.sub(_mask_intl_phone, text)
    for pat in _NATIONAL_PHONE_RES:
        text = pat.sub("[PHONE]", text)
    return text


# ── Zendesk API ヘルパー ──────────────────────────────────

def _auth() -> str:
    cred = f"{ZENDESK_EMAIL}/token:{ZENDESK_API_TOKEN}"
    return "Basic " + base64.b64encode(cred.encode()).decode()


def zd_get(path: str, params: dict | None = None) -> dict:
    url = f"{ZENDESK_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": _auth()})
    with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as resp:
        return json.loads(resp.read())


def zd_post(path: str, payload: dict) -> dict:
    url  = f"{ZENDESK_BASE}{path}"
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Authorization": _auth(), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as resp:
        return json.loads(resp.read())


def zd_put(path: str, payload: dict) -> dict:
    url  = f"{ZENDESK_BASE}{path}"
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url, data=data, method="PUT",
        headers={"Authorization": _auth(), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as resp:
        return json.loads(resp.read())


def zd_get_all(path: str, result_key: str, params: dict | None = None,
               max_total: int = 200) -> tuple[list, int | None]:
    PAGE_SIZE = 100
    p = dict(params or {})
    p["per_page"] = PAGE_SIZE
    p.setdefault("page", 1)

    items: list = []
    while True:
        data       = zd_get(path, p)
        page_items = data.get(result_key, [])
        items     += page_items

        if len(items) >= max_total:
            items = items[:max_total]
            total     = data.get("count") or data.get("total")
            remaining = (total - max_total) if total and total > max_total else None
            return items, remaining

        next_page = data.get("next_page")
        if not next_page or len(page_items) < PAGE_SIZE:
            break

        p["page"] = p["page"] + 1

    return items, None


# ── ツール定義 ────────────────────────────────────────────

TOOLS = [
    # ── チケット系 ──────────────────────────────────────
    {
        "name": "search_tickets",
        "description": "チケットをキーワード・ステータス・タグ・担当者・日付等で検索する。keywordはタイトル・コメント本文の全文検索",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword":        {"type": "string",  "description": "タイトルまたはコメント本文に含まれる文字列で全文検索（例: 'ログインできない'）"},
                "query":          {"type": "string",  "description": "Zendesk検索修飾子（例: 'status:open tags:vip'）。keywordと併用可"},
                "assignee":       {"type": "string",  "description": "担当者のメールアドレスまたはユーザー名で絞り込む（例: 'john@example.com'）"},
                "created_after":  {"type": "string",  "description": "この日付以降に作成（YYYY-MM-DD）"},
                "created_before": {"type": "string",  "description": "この日付以前に作成（YYYY-MM-DD）"},
                "updated_after":  {"type": "string",  "description": "この日付以降に更新（YYYY-MM-DD）"},
                "updated_before": {"type": "string",  "description": "この日付以前に更新（YYYY-MM-DD）"},
                "per_page":       {"type": "integer", "description": "1ページあたりの取得件数（最大100）", "default": 100},
                "max_total":      {"type": "integer", "description": "取得上限件数（デフォルト200）。1000超を指定するとExport APIで大量取得可能（ソートはcreated_at昇順固定）", "default": 200},
            },
        },
    },
    {
        "name": "get_ticket",
        "description": "チケットIDを指定して詳細情報・カスタムフィールド・コメント一覧を取得する",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer", "description": "ZendeskチケットID"},
            },
            "required": ["ticket_id"],
        },
    },
    {
        "name": "list_tickets",
        "description": "最近のチケット一覧を取得する（傾向分析向け）。タグで絞り込むことも可能",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status":         {"type": "string",  "description": "open / pending / solved / closed", "default": "open"},
                "per_page":       {"type": "integer", "description": "取得件数（最大100）", "default": 30},
                "tags":           {"type": "array", "items": {"type": "string"}, "description": "絞り込むタグのリスト（例: ['bug', 'vip']）。複数指定するとAND条件になる"},
                "created_after":  {"type": "string",  "description": "この日付以降に作成（YYYY-MM-DD）"},
                "created_before": {"type": "string",  "description": "この日付以前に作成（YYYY-MM-DD）"},
                "updated_after":  {"type": "string",  "description": "この日付以降に更新（YYYY-MM-DD）"},
                "updated_before": {"type": "string",  "description": "この日付以前に更新（YYYY-MM-DD）"},
                "max_total":      {"type": "integer", "description": "取得上限件数（デフォルト200）。上限に達した場合は残り件数を通知する", "default": 200},
            },
        },
    },
    {
        "name": "get_ticket_stats",
        "description": "現在のチケット件数をステータス別に集計して返す",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "suggest_reply",
        "description": "チケットの内容を取得し、返信案を生成するためのコンテキストを組み立てる",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer", "description": "返信案を作りたいチケットID"},
            },
            "required": ["ticket_id"],
        },
    },

    # ── ナレッジベース系 ─────────────────────────────────
    {
        "name": "search_kb_articles",
        "description": "ナレッジベース記事をキーワードで全文検索する",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query":    {"type": "string",  "description": "検索キーワード"},
                "per_page": {"type": "integer", "description": "取得件数（最大30）", "default": 10},
                "locale":   {"type": "string",  "description": "言語コード（例: ja, en-us）。省略時は全言語", "default": ""},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_kb_articles",
        "description": "ナレッジベース記事の一覧を取得する（カテゴリ・セクション絞り込み可）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "per_page":   {"type": "integer", "description": "取得件数（最大100）", "default": 20},
                "section_id": {"type": "integer", "description": "セクションIDで絞り込む（省略可）"},
                "locale":     {"type": "string",  "description": "言語コード（例: ja, en-us）。省略時は全言語", "default": ""},
            },
        },
    },
    {
        "name": "get_kb_article",
        "description": "ナレッジベース記事IDを指定して本文全体を取得する",
        "inputSchema": {
            "type": "object",
            "properties": {
                "article_id": {"type": "integer", "description": "KB記事ID"},
                "locale":     {"type": "string",  "description": "言語コード（例: ja, en-us）。省略時はデフォルト言語", "default": ""},
            },
            "required": ["article_id"],
        },
    },
    {
        "name": "list_kb_categories",
        "description": "ナレッジベースのカテゴリ一覧を取得する",
        "inputSchema": {
            "type": "object",
            "properties": {
                "locale": {"type": "string", "description": "言語コード（省略可）", "default": ""},
            },
        },
    },
    {
        "name": "list_kb_sections",
        "description": "ナレッジベースのセクション一覧を取得する（カテゴリIDで絞り込み可）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category_id": {"type": "integer", "description": "カテゴリIDで絞り込む（省略可）"},
                "locale":      {"type": "string",  "description": "言語コード（省略可）", "default": ""},
            },
        },
    },

    # ── CSAT系 ──────────────────────────────────────────
    {
        "name": "get_csat_rating",
        "description": "チケットIDを指定してそのチケットのCSAT評価・コメント・bad理由を取得する",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer", "description": "ZendeskチケットID"},
                "locale":    {"type": "string",  "description": "言語コード（デフォルト: ja）", "default": "ja"},
            },
            "required": ["ticket_id"],
        },
    },

    # ── ユーザー系 ──────────────────────────────────────
    {
        "name": "get_user",
        "description": "ZendeskユーザーIDまたはメールアドレスでユーザー情報・カスタムフィールド（OSやアプリバージョン等）を取得する",
        "inputSchema": {
            "type": "object",
            "properties": {
                "requester_id": {"type": "integer", "description": "ZendeskユーザーID（get_ticketで取得したrequester_idなど）"},
                "email":        {"type": "string",  "description": "メールアドレスで検索する場合に指定"},
            },
        },
    },
    {
        "name": "list_user_fields",
        "description": "Zendeskに定義されているユーザーカスタムフィールドの一覧を取得する（フィールドキー・型・選択肢を確認できる）",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },

    # ── ヘルプ記事書き込み ───────────────────────────────
    {
        "name": "create_kb_article",
        "description": "ヘルプセンターに記事を下書きとして新規作成する。section_idはlist_kb_sectionsで取得できる",
        "inputSchema": {
            "type": "object",
            "properties": {
                "section_id": {"type": "integer", "description": "投稿先のセクションID（list_kb_sectionsで確認）"},
                "title":      {"type": "string",  "description": "記事タイトル"},
                "body":       {"type": "string",  "description": "記事本文（HTML形式）"},
                "locale":     {"type": "string",  "description": "言語コード（デフォルト: ja）", "default": "ja"},
            },
            "required": ["section_id", "title", "body"],
        },
    },
    {
        "name": "update_kb_article",
        "description": "既存のヘルプ記事を更新する。タイトル・本文・下書き状態を変更できる。article_idはget_kb_articleやlist_kb_articlesで取得できる",
        "inputSchema": {
            "type": "object",
            "properties": {
                "article_id": {"type": "integer", "description": "更新対象の記事ID"},
                "title":      {"type": "string",  "description": "新しいタイトル（省略時は変更なし）"},
                "body":       {"type": "string",  "description": "新しい本文（HTML形式）（省略時は変更なし）"},
                "draft":      {"type": "boolean", "description": "Trueで下書きに戻す、Falseで公開（省略時は変更なし）"},
                "locale":     {"type": "string",  "description": "言語コード（デフォルト: ja）", "default": "ja"},
            },
            "required": ["article_id"],
        },
    },
]


# ── ツール実装 ────────────────────────────────────────────

def zd_search_export(query: str, max_total: int = 10000) -> tuple[list, int | None]:
    PAGE_SIZE = 1000
    params = {
        "query":        query,
        "filter[type]": "ticket",
        "page[size]":   PAGE_SIZE,
    }
    items: list = []

    while True:
        url = f"{ZENDESK_BASE}/search/export.json?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": _auth()})
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as resp:
            data = json.loads(resp.read())

        page_items = data.get("results", [])
        items     += page_items

        if len(items) >= max_total:
            items = items[:max_total]
            meta  = data.get("meta", {})
            remaining = -1 if meta.get("has_more") else None
            return items, remaining

        meta = data.get("meta", {})
        if not meta.get("has_more"):
            break

        after = meta.get("after_cursor")
        if not after:
            break
        params["page[after]"] = after

    return items, None


def _remaining_msg(remaining: int | None, name: str = "件") -> str:
    if remaining:
        return f"\n\n⚠️ 上限に達しました。まだ約{remaining}{name}あります。続けて取得しますか？（max_total を増やして再実行してください）"
    return ""


def tool_search_tickets(args: dict) -> str:
    query     = args.get("query", "")
    keyword   = args.get("keyword", "")
    assignee  = args.get("assignee", "")
    max_total = args.get("max_total", 200)

    if keyword:
        query = f'"{keyword}" {query}'.strip()
    if assignee:
        query = f"{query} assignee:{assignee}"
    for key, modifier in [
        ("created_after",  "created>"),
        ("created_before", "created<"),
        ("updated_after",  "updated>"),
        ("updated_before", "updated<"),
    ]:
        if args.get(key):
            query = f"{query} {modifier}{args[key]}"

    full_query = f"type:ticket {query}"
    if max_total > 1000:
        tickets, remaining = zd_search_export(full_query, max_total=max_total)
        lines = [f"検索結果: {len(tickets)} 件（Export API使用 / created_at昇順）\n"]
    else:
        tickets, remaining = zd_get_all(
            "/search.json", "results",
            {"query": full_query, "sort_by": "updated_at", "sort_order": "desc"},
            max_total=max_total,
        )
        lines = [f"検索結果: {len(tickets)} 件\n"]
    for t in tickets:
        assignee_id  = t.get("assignee_id", "")
        requester_id = t.get("requester_id", "")
        lines.append(f"[#{t['id']}] {t['subject']} | {t['status']} | requester_id: {requester_id} | 担当者ID: {assignee_id} | 作成: {t['created_at'][:10]} | タグ: {','.join(t.get('tags',[]))}")
    return "\n".join(lines) + _remaining_msg(remaining)


# ── チケットフィールド名の解決 ────────────────────────────

_TICKET_FIELD_TITLES: dict[int, str] | None = None


def _ticket_field_titles() -> dict[int, str]:
    """カスタムフィールド id -> 表示名。初回のみ API を叩いてプロセス内にキャッシュ。"""
    global _TICKET_FIELD_TITLES
    if _TICKET_FIELD_TITLES is None:
        try:
            fields = zd_get("/ticket_fields.json").get("ticket_fields", [])
            _TICKET_FIELD_TITLES = {f["id"]: f.get("title", "") for f in fields}
        except Exception:
            _TICKET_FIELD_TITLES = {}
    return _TICKET_FIELD_TITLES


# 人名を保持するカスタムフィールド。regex では人名を検出できないため、
# get_user と同様にフィールド単位でプレースホルダに差し替える。
_NAME_FIELD_TITLES = ("名前", "氏名", "お名前")


def _format_custom_fields(t: dict) -> list[str]:
    """値が入っているカスタムフィールドだけを「表示名: 値」の行にする。"""
    titles = _ticket_field_titles()
    rows = []
    for cf in t.get("custom_fields") or []:
        v = cf.get("value")
        if v is None or v == "" or v is False or v == []:
            continue
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        title = titles.get(cf.get("id")) or f"field_{cf.get('id')}"
        if MASK_PII and title in _NAME_FIELD_TITLES:
            v = "[NAME]"
        rows.append(f"- {title}: {v}")
    return rows


def tool_get_ticket(args: dict) -> str:
    tid  = args["ticket_id"]
    t    = zd_get(f"/tickets/{tid}.json")["ticket"]
    cmts = zd_get(f"/tickets/{tid}/comments.json")["comments"]
    lines = [
        f"# チケット #{tid}: {t['subject']}",
        f"ステータス: {t['status']} | 優先度: {t.get('priority','なし')}",
        f"作成: {t['created_at'][:10]} | 更新: {t['updated_at'][:10]}",
        f"requester_id: {t.get('requester_id','なし')} | assignee_id: {t.get('assignee_id','なし')}",
        f"タグ: {', '.join(t.get('tags',[])) or 'なし'}",
    ]
    cf_rows = _format_custom_fields(t)
    if cf_rows:
        lines += ["", "## カスタムフィールド"] + cf_rows
    lines += ["", "## コメント"]
    for c in cmts:
        author = "顧客" if c["author_id"] == t["requester_id"] else "エージェント"
        lines.append(f"\n[{author} / {c['created_at'][:10]}]\n{c['body'][:800]}")
    return "\n".join(lines)


def tool_list_tickets(args: dict) -> str:
    status    = args.get("status", "open")
    tags      = args.get("tags", [])
    max_total = args.get("max_total", 200)

    date_query = ""
    for key, modifier in [
        ("created_after",  "created>"),
        ("created_before", "created<"),
        ("updated_after",  "updated>"),
        ("updated_before", "updated<"),
    ]:
        if args.get(key):
            date_query += f" {modifier}{args[key]}"

    if tags or date_query:
        tag_query = " ".join(f"tags:{t}" for t in tags)
        query     = f"type:ticket status:{status} {tag_query}{date_query}".strip()
        tickets, remaining = zd_get_all(
            "/search.json", "results",
            {"query": query, "sort_by": "updated_at", "sort_order": "desc"},
            max_total=max_total,
        )
        label_parts = []
        if tags:
            label_parts.append(f"タグ: {', '.join(tags)}")
        if date_query:
            label_parts.append(date_query.strip())
        lines = [f"チケット一覧（{status} / {' / '.join(label_parts)}）: {len(tickets)} 件\n"]
    else:
        tickets, remaining = zd_get_all(
            "/tickets.json", "tickets",
            {"status": status, "sort_by": "updated_at", "sort_order": "desc"},
            max_total=max_total,
        )
        lines = [f"チケット一覧（{status}）: {len(tickets)} 件\n"]

    for t in tickets:
        lines.append(f"[#{t['id']}] {t['subject']} | 更新: {t['updated_at'][:10]} | タグ: {','.join(t.get('tags',[]))}")
    return "\n".join(lines) + _remaining_msg(remaining)


def tool_get_ticket_stats(args: dict) -> str:
    results = {}
    for status in ("open", "pending", "solved", "closed"):
        data = zd_get("/search.json", {"query": f"type:ticket status:{status}", "per_page": 1})
        results[status] = data.get("count", 0)
    total = sum(results.values())
    lines = ["📊 チケット統計（現在）", f"合計: {total} 件", ""]
    for k, v in results.items():
        pct = round(v / total * 100) if total else 0
        lines.append(f"  {k:8s}: {v:4d} 件 ({pct}%)")
    return "\n".join(lines)


def tool_suggest_reply(args: dict) -> str:
    tid  = args["ticket_id"]
    t    = zd_get(f"/tickets/{tid}.json")["ticket"]
    cmts = zd_get(f"/tickets/{tid}/comments.json")["comments"]
    conv_history = []
    for c in cmts[-6:]:
        role = "顧客" if c["author_id"] == t["requester_id"] else "エージェント"
        conv_history.append(f"[{role}] {c['body'][:300]}")
    return (
        f"## 返信案生成コンテキスト\n\n"
        f"**件名**: {t['subject']}\n"
        f"**ステータス**: {t['status']} | **優先度**: {t.get('priority','なし')}\n"
        f"**タグ**: {', '.join(t.get('tags',[]))}\n\n"
        f"### 会話履歴（直近）\n" + "\n\n".join(conv_history) + "\n\n"
        f"---\n"
        f"上記を踏まえて、丁寧かつ簡潔な返信案を日本語で作成してください。\n"
        f"・顧客の問題を正確に理解して解決策を提示する\n"
        f"・専門用語は避け、わかりやすい言葉を使う\n"
        f"・必要に応じてナレッジベース記事への誘導を含める"
    )


# ── KB ツール実装 ─────────────────────────────────────────

def tool_search_kb_articles(args: dict) -> str:
    query    = args["query"]
    per_page = min(args.get("per_page", 10), 30)
    params   = {"query": query, "per_page": per_page}
    if args.get("locale"):
        params["locale"] = args["locale"]
    data     = zd_get("/help_center/articles/search.json", params)
    articles = data.get("results", [])
    lines    = [f"KB検索結果: {len(articles)} 件（クエリ: {query}）\n"]
    for a in articles:
        lines.append(f"[ID:{a['id']}] {a['title']} | セクション:{a.get('section_id','?')} | 更新:{a['updated_at'][:10]}")
    return "\n".join(lines)


def tool_list_kb_articles(args: dict) -> str:
    per_page   = min(args.get("per_page", 20), 100)
    section_id = args.get("section_id")
    locale     = args.get("locale", "")
    params     = {"per_page": per_page, "sort_by": "updated_at", "sort_order": "desc"}
    if locale:
        params["locale"] = locale

    if section_id:
        path = f"/help_center/sections/{section_id}/articles.json"
    else:
        path = "/help_center/articles.json"

    data     = zd_get(path, params)
    articles = data.get("articles", [])
    lines    = [f"KB記事一覧: {len(articles)} 件\n"]
    for a in articles:
        draft = " [下書き]" if a.get("draft") else ""
        lines.append(f"[ID:{a['id']}] {a['title']}{draft} | セクション:{a.get('section_id','?')} | 更新:{a['updated_at'][:10]}")
    return "\n".join(lines)


def tool_get_kb_article(args: dict) -> str:
    article_id = args["article_id"]
    locale     = args.get("locale", "")
    path       = f"/help_center/articles/{article_id}.json"
    if locale:
        path = f"/help_center/locales/{locale}/articles/{article_id}.json"
    data    = zd_get(path)
    article = data.get("article", {})

    body = re.sub(r"<[^>]+>", "", article.get("body", ""))
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    return (
        f"# {article.get('title','（タイトルなし）')}\n\n"
        f"ID: {article.get('id')} | セクション: {article.get('section_id','?')} | "
        f"更新: {article.get('updated_at','?')[:10]}\n\n"
        f"{body}"
    )


def tool_list_kb_categories(args: dict) -> str:
    locale = args.get("locale", "")
    params = {}
    if locale:
        params["locale"] = locale
    data       = zd_get("/help_center/categories.json", params or None)
    categories = data.get("categories", [])
    lines      = [f"カテゴリ一覧: {len(categories)} 件\n"]
    for c in categories:
        lines.append(f"[ID:{c['id']}] {c['name']} | 説明: {(c.get('description') or '')[:60]}")
    return "\n".join(lines)


def tool_list_kb_sections(args: dict) -> str:
    category_id = args.get("category_id")
    locale      = args.get("locale", "")
    params      = {}
    if locale:
        params["locale"] = locale
    path     = f"/help_center/categories/{category_id}/sections.json" if category_id else "/help_center/sections.json"
    data     = zd_get(path, params or None)
    sections = data.get("sections", [])
    lines    = [f"セクション一覧: {len(sections)} 件\n"]
    for s in sections:
        lines.append(f"[ID:{s['id']}] {s['name']} | カテゴリ:{s.get('category_id','?')} | 更新:{s['updated_at'][:10]}")
    return "\n".join(lines)


# ── CSAT ─────────────────────────────────────────────────

def _parse_survey_response(sr: dict, locale: str = "ja") -> dict:
    result = {
        "id":              sr.get("id"),
        "ticket_id":       None,
        "rating":          None,
        "rating_category": None,
        "comment":         None,
        "bad_reason":      None,
        "created_at":      None,
    }
    for subj in sr.get("subjects", []):
        if subj.get("type") == "ticket":
            result["ticket_id"] = subj.get("id")

    for ans in sr.get("answers", []):
        atype = ans.get("type")
        q     = ans.get("question", {})
        alias = q.get("alias", "")

        if atype == "rating_scale" and q.get("sub_type") == "customer_satisfaction":
            result["rating"]          = ans.get("rating")
            result["rating_category"] = ans.get("rating_category")
            result["created_at"]      = ans.get("created_at", "")[:10]

        elif atype == "closed_ended" and alias == "reason":
            for sel in ans.get("selections", []):
                opt_id = sel.get("option_id")
                for opt in q.get("options", []):
                    if opt.get("id") == opt_id:
                        label = opt.get("label", {})
                        result["bad_reason"] = label.get("value") or label.get("key", "")

        elif atype == "open_ended" and alias == "comment":
            result["comment"] = ans.get("value")

    return result


def _zd_get_survey_responses_page(params: dict) -> dict:
    url = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2/guide/survey_responses"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": _auth()})
    with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as resp:
        return json.loads(resp.read())


def _get_survey_response_detail(sr_id: str, locale: str = "ja") -> dict:
    url = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2/guide/{locale}/survey_responses/{sr_id}"
    req = urllib.request.Request(url, headers={"Authorization": _auth()})
    with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as resp:
        return json.loads(resp.read()).get("survey_response", {})


def tool_get_csat_rating(args: dict) -> str:
    tid    = args["ticket_id"]
    locale = args.get("locale", "ja")

    data      = _zd_get_survey_responses_page({
        "filter[subject_zrns]": f"zen:ticket:{tid}",
        "page[size]": 10,
    })
    responses = data.get("survey_responses", [])

    if not responses:
        return f"チケット #{tid} にはCSAT評価がありません。"

    detail = _get_survey_response_detail(responses[0]["id"], locale)
    parsed = _parse_survey_response(detail, locale)

    t     = zd_get(f"/tickets/{tid}.json")["ticket"]
    lines = [
        f"# チケット #{tid} のCSAT評価",
        f"件名: {t['subject']}",
        f"評価: {parsed.get('rating_category','?')}（スコア: {parsed.get('rating','?')}）",
    ]
    if parsed.get("bad_reason"):
        lines.append(f"bad理由: {parsed['bad_reason']}")
    lines += [
        f"コメント: {parsed.get('comment') or '（コメントなし）'}",
        f"評価日時: {parsed.get('created_at','?')}",
    ]
    return "\n".join(lines)


# ── ユーザー系 ────────────────────────────────────────────

def tool_get_user(args: dict) -> str:
    user_id = args.get("requester_id")
    email   = args.get("email", "")

    if user_id:
        data = zd_get(f"/users/{user_id}.json")
        user = data.get("user", {})
    elif email:
        data  = zd_get("/users/search.json", {"query": email})
        users = data.get("users", [])
        if not users:
            return f"メールアドレス '{email}' に一致するユーザーが見つかりませんでした。"
        user = users[0]
    else:
        return "user_id または email を指定してください。"

    custom_fields = user.get("user_fields", {})
    cf_lines = []
    for k, v in custom_fields.items():
        if v not in (None, "", []):
            cf_lines.append(f"  {k}: {v}")

    # 名前・メールは構造フィールドの PII。regex では人名を検出できないため、
    # ここでフィールド単位でプレースホルダに差し替える(ON/OFF は MASK_PII で制御)。
    name_display  = "[NAME]" if MASK_PII else user.get('name')
    email_display = "[EMAIL]" if MASK_PII else user.get('email')

    lines = [
        "# ユーザー情報",
        f"ID      : {user.get('id')}",
        f"名前    : {name_display}",
        f"メール  : {email_display}",
        f"ロール  : {user.get('role')}",
        f"組織ID  : {user.get('organization_id', 'なし')}",
        f"タグ    : {', '.join(user.get('tags', [])) or 'なし'}",
        f"作成日  : {user.get('created_at', '')[:10]}",
        f"最終ログイン: {user.get('last_login_at', 'なし')[:10] if user.get('last_login_at') else 'なし'}",
        f"アクティブ: {user.get('active')}",
        "",
        "## カスタムフィールド",
    ]
    lines += cf_lines if cf_lines else ["  （カスタムフィールドなし）"]
    return "\n".join(lines)


def tool_list_user_fields(args: dict) -> str:
    data   = zd_get("/user_fields.json")
    fields = data.get("user_fields", [])
    lines  = [f"ユーザーカスタムフィールド一覧: {len(fields)} 件\n"]
    for f in fields:
        field_type = f.get("type", "?")
        key        = f.get("key", "?")
        title      = f.get("title", "?")
        options    = f.get("custom_field_options", [])
        opt_str    = ""
        if options:
            opt_labels = [o.get("name", "") for o in options[:5]]
            opt_str    = f" | 選択肢: {', '.join(opt_labels)}"
            if len(options) > 5:
                opt_str += f" 他{len(options)-5}件"
        lines.append(f"[{key}] {title}（{field_type}）{opt_str}")
    return "\n".join(lines)


# ── KB 書き込み ────────────────────────────────────────────

def tool_create_kb_article(args: dict) -> str:
    section_id = args["section_id"]
    title      = args["title"]
    body       = args["body"]
    locale     = args.get("locale", "ja")
    payload    = {"article": {"title": title, "body": body, "locale": locale, "draft": True}}
    data       = zd_post(f"/help_center/sections/{section_id}/articles.json", payload)
    article    = data.get("article", {})
    lines = [
        "# 記事を下書きとして作成しました",
        f"ID      : {article.get('id')}",
        f"タイトル: {article.get('title')}",
        f"セクション: {article.get('section_id')}",
        f"URL     : {article.get('html_url', 'なし')}",
        f"作成日  : {article.get('created_at', '')[:10]}",
        "ステータス: 下書き（draft）",
    ]
    return "\n".join(lines)


def tool_update_kb_article(args: dict) -> str:
    article_id = args["article_id"]
    locale     = args.get("locale", "ja")
    payload    = {}
    if "title" in args:
        payload["title"] = args["title"]
    if "body" in args:
        payload["body"] = args["body"]
    if "draft" in args:
        payload["draft"] = args["draft"]
    if not payload:
        return "更新内容が指定されていません。title / body / draft のいずれかを指定してください。"
    data    = zd_put(f"/help_center/articles/{article_id}/translations/{locale}.json", {"translation": payload})
    article = data.get("translation", {})
    draft_status = "下書き（draft）" if article.get("draft") else "公開済み"
    lines = [
        "# 記事を更新しました",
        f"ID      : {article.get('source_id', article_id)}",
        f"タイトル: {article.get('title', '（取得不可）')}",
        f"URL     : {article.get('html_url', 'なし')}",
        f"更新日  : {article.get('updated_at', '')[:10]}",
        f"ステータス: {draft_status}",
    ]
    return "\n".join(lines)


# KB ツールは記事本文(サポート窓口の連絡先等)を返すため、regex マスクの対象外にする。
# Spec の Non-Goal「KB 記事本文のマスク」を尊重する。
_MASK_EXEMPT_TOOLS = {
    "get_kb_article", "search_kb_articles", "list_kb_articles",
    "list_kb_categories", "list_kb_sections",
}


TOOL_HANDLERS = {
    "search_tickets":     tool_search_tickets,
    "get_ticket":         tool_get_ticket,
    "list_tickets":       tool_list_tickets,
    "get_ticket_stats":   tool_get_ticket_stats,
    "suggest_reply":      tool_suggest_reply,
    "search_kb_articles": tool_search_kb_articles,
    "list_kb_articles":   tool_list_kb_articles,
    "get_kb_article":     tool_get_kb_article,
    "list_kb_categories": tool_list_kb_categories,
    "list_kb_sections":   tool_list_kb_sections,
    "get_csat_rating":    tool_get_csat_rating,
    "get_user":           tool_get_user,
    "list_user_fields":   tool_list_user_fields,
    "create_kb_article":  tool_create_kb_article,
    "update_kb_article":  tool_update_kb_article,
}


# ── MCP stdio ループ ──────────────────────────────────────

def handle(body: dict) -> dict | None:
    method = body.get("method", "")
    req_id = body.get("id")

    def ok(result):
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code, msg):
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": msg}}

    if method == "initialize":
        if not all([ZENDESK_SUBDOMAIN, ZENDESK_EMAIL, ZENDESK_API_TOKEN]):
            sys.stderr.write("[ERROR] 環境変数 ZENDESK_SUBDOMAIN / ZENDESK_EMAIL / ZENDESK_API_TOKEN が未設定です\n")
        return ok({
            "protocolVersion": "2024-11-05",
            "capabilities":    {"tools": {}},
            "serverInfo":      {"name": "zendesk-mcp", "version": "1.1.0"},
        })

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return ok({"tools": TOOLS})

    if method == "tools/call":
        name = body["params"]["name"]
        args = body["params"].get("arguments", {})
        if name not in TOOL_HANDLERS:
            return err(-32601, f"Unknown tool: {name}")
        try:
            result = TOOL_HANDLERS[name](args)
            if MASK_PII and name not in _MASK_EXEMPT_TOOLS:
                result = mask_pii_text(result)
            return ok({"content": [{"type": "text", "text": result}]})
        except Exception as e:
            msg = str(e)
            if MASK_PII:
                msg = mask_pii_text(msg)
            return err(-32603, msg)

    return err(-32601, f"Method not found: {method}")


def main():
    sys.stderr.write("[zendesk-mcp] 起動しました（stdio モード）\n")
    sys.stderr.write(f"[zendesk-mcp] PII masking: {'ON' if MASK_PII else 'OFF'}\n")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            body = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle(body)
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()