"""PII マスキング機能のテスト。

What: server.py の PII マスキング(メール・電話の regex 置換、tool_get_user の
      フィールドレベルマスク、チョークポイント/エラーパスのガード、環境変数パース)が
      仕様どおりに振る舞うことを検証する。
"""

from unittest import mock

import pytest

import server


# ── AC-1: mask_pii_text の純粋性・冪等性 ──────────────────────

def test_mask_pii_text_is_deterministic():
    text = "連絡先: user@example.com / 090-1234-5678"
    assert server.mask_pii_text(text) == server.mask_pii_text(text)


def test_mask_pii_text_is_idempotent():
    text = "連絡先: user@example.com / 090-1234-5678"
    once = server.mask_pii_text(text)
    assert server.mask_pii_text(once) == once


# ── AC-3: メール検出 ─────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("連絡先: user@example.com", "連絡先: [EMAIL]"),
    ("test@sub.example.co.jp", "[EMAIL]"),
    ("user+tag@example.com", "[EMAIL]"),
    ("a@b.com と c@d.com", "[EMAIL] と [EMAIL]"),
])
def test_email_masked(text, expected):
    assert server.mask_pii_text(text) == expected


def test_no_email_unchanged():
    assert server.mask_pii_text("これはテストです") == "これはテストです"


# ── AC-4: 電話番号検出 ───────────────────────────────────────

@pytest.mark.parametrize("text", [
    "090-1234-5678",       # 携帯 ハイフン有
    "09012345678",         # 携帯 ハイフン無
    "03-1234-5678",        # 固定 2桁市外局番 ハイフン有
    "0312345678",          # 固定 2桁市外局番 ハイフン無
    "(03)1234-5678",       # 括弧形式
    "+81-90-1234-5678",    # 国際 ハイフン
    "+819012345678",       # 国際 連続
    "0120-123-456",        # フリーダイヤル 0120
    "0800-123-4567",       # フリーダイヤル 0800 (セパレータ付き)
    "045-123-4567",        # 固定 3桁市外局番
    "0467-12-3456",        # 固定 4桁市外局番
])
def test_phone_masked(text):
    assert server.mask_pii_text(f"電話: {text} です") == "電話: [PHONE] です"


def test_email_with_apostrophe_fully_masked():
    # アポストロフィ入りローカル部でも部分マッチの残骸 (o') を残さない
    assert server.mask_pii_text("o'malley@example.com") == "[EMAIL]"


# ── AC-4/AC-6: 電話番号として誤検出してはならないもの ────────

@pytest.mark.parametrize("text", [
    "requester_id: 12345678",   # 数値ID
    "チケット #12345",           # チケットID
    "注文番号: 03421",           # 短い数字列 (5桁)
    "コード: 012345",            # 短い数字列 (6桁)
    "ref: 0123456",              # 短い数字列 (7桁)
    "〒012-3456",                # 郵便番号
    "+81-1-2-34",                # +81 後4桁 (電話番号ではない)
    "+81123456",                 # +81 後6桁 (桁数不足)
    "ステータス: open",          # PII なし
])
def test_non_phone_unchanged(text):
    assert server.mask_pii_text(text) == text


def test_cross_line_date_plus_phone():
    # 改行を跨いだ誤マッチが起きないこと。日付は保持し、電話のみマスクされる。
    text = "更新: 2026-07-23\n0120-123-456 に電話"
    result = server.mask_pii_text(text)
    assert "2026-07-23" in result
    assert "[PHONE]" in result
    assert result == "更新: 2026-07-23\n[PHONE] に電話"


def test_cross_line_date_plus_date():
    text = "2026-07-23\n2026-07-22"
    assert server.mask_pii_text(text) == text


# ── 混合 ─────────────────────────────────────────────────────

def test_email_and_phone_masked():
    assert server.mask_pii_text("user@a.com / 090-1234-5678") == "[EMAIL] / [PHONE]"


# ── AC-8: 環境変数パース ─────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    (None, True),
    ("", True),
    ("1", True),
    ("true", True),
    ("TRUE", True),
    ("0", False),
    ("false", False),
    ("FALSE", False),
    ("false ", False),   # 前後の空白は無視する
    (" 0 ", False),
    ("fals", True),   # typo は安全側に倒して ON
])
def test_parse_mask_pii_env(value, expected):
    assert server._parse_mask_pii_env(value) is expected


# ── AC-2/AC-6/AC-7: tool_get_user のフィールドレベルマスク ────

_FAKE_USER = {
    "user": {
        "id": 12345,
        "name": "山田太郎",
        "email": "taro@example.com",
        "role": "end-user",
        "organization_id": 999,
        "tags": [],
        "created_at": "2020-01-01T00:00:00Z",
        "last_login_at": "2026-07-01T00:00:00Z",
        "active": True,
        "user_fields": {"os_version": "iOS 17"},
    }
}


def test_get_user_masks_name_and_email_when_on():
    with mock.patch.object(server, "MASK_PII", True), \
         mock.patch.object(server, "zd_get", return_value=_FAKE_USER):
        out = server.tool_get_user({"requester_id": 12345})
    assert "[NAME]" in out
    assert "[EMAIL]" in out
    assert "山田太郎" not in out
    assert "taro@example.com" not in out
    # AC-6: 数値 ID は保持される
    assert "12345" in out
    # AC-7: user_fields のカスタム値は残る
    assert "iOS 17" in out


def test_get_user_passthrough_when_off():
    with mock.patch.object(server, "MASK_PII", False), \
         mock.patch.object(server, "zd_get", return_value=_FAKE_USER):
        out = server.tool_get_user({"requester_id": 12345})
    assert "山田太郎" in out
    assert "taro@example.com" in out


# ── AC-6: mask_pii_text が数値 ID を変えない ─────────────────

def test_numeric_id_unchanged_by_mask_pii_text():
    assert server.mask_pii_text("requester_id: 12345") == "requester_id: 12345"


# ── AC-9: KB ツール除外セット ────────────────────────────────

def test_mask_exempt_tools_contains_kb_tools():
    assert server._MASK_EXEMPT_TOOLS == {
        "get_kb_article", "search_kb_articles", "list_kb_articles",
        "list_kb_categories", "list_kb_sections",
    }


def _call_tool(name, handler):
    """handle() 経由で tools/call を呼び、返り値のテキストを取り出すヘルパー。"""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": {}},
    }
    with mock.patch.dict(server.TOOL_HANDLERS, {name: handler}):
        return server.handle(body)


def test_kb_tool_output_not_masked_even_when_on():
    # KB ツールは除外セットに含まれるため、ON でも regex マスクされない。
    raw = "サポート窓口: 0120-123-456 / support@example.com"
    with mock.patch.object(server, "MASK_PII", True):
        resp = _call_tool("get_kb_article", lambda args: raw)
    assert resp["result"]["content"][0]["text"] == raw


def test_non_kb_tool_output_masked_when_on():
    raw = "サポート窓口: 0120-123-456 / support@example.com"
    with mock.patch.object(server, "MASK_PII", True):
        resp = _call_tool("get_ticket", lambda args: raw)
    text = resp["result"]["content"][0]["text"]
    assert "[PHONE]" in text and "[EMAIL]" in text
    assert "0120-123-456" not in text
    assert "support@example.com" not in text


# ── AC-5: チョークポイントガードのバイパス (OFF) ─────────────

def test_choke_point_bypassed_when_off():
    raw = "電話: 090-1234-5678 / mail: user@example.com"
    with mock.patch.object(server, "MASK_PII", False):
        resp = _call_tool("get_ticket", lambda args: raw)
    assert resp["result"]["content"][0]["text"] == raw


# ── AC-10: エラーパスのマスク ───────────────────────────────

def test_error_path_masks_pii_when_on():
    def boom(args):
        raise RuntimeError("Failed for user@example.com / 090-1234-5678")

    with mock.patch.object(server, "MASK_PII", True):
        resp = _call_tool("get_ticket", boom)
    msg = resp["error"]["message"]
    assert "[EMAIL]" in msg and "[PHONE]" in msg
    assert "user@example.com" not in msg
    assert "090-1234-5678" not in msg


def test_error_path_passthrough_when_off():
    def boom(args):
        raise RuntimeError("Failed for user@example.com")

    with mock.patch.object(server, "MASK_PII", False):
        resp = _call_tool("get_ticket", boom)
    assert "user@example.com" in resp["error"]["message"]
