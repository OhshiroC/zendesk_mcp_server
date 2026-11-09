"""社内メモ投稿 (add_internal_note) と書き込み系ガードのテスト。

What: 設定 OFF 時はツールが隠れて実行も拒否されること、1 回目はプレビューのみで
      書き込まないこと、トークンがチケットID・本文・期限に結び付いていること、
      投稿は常に public=False で送られ、公開として記録されたら失敗扱いになること、
      記事の公開 (draft=False) が設定 OFF では拒否されることを検証する。
      Zendesk には接続せず zd_get / zd_put を差し替える。
"""

from unittest import mock

import pytest

import server


TICKET = {"id": 42, "subject": "テスト件名", "status": "open", "updated_at": "2026-11-09T00:00:00Z"}


def _put_ok(public=False):
    return {"audit": {"events": [{"type": "Comment", "id": 999, "public": public, "body": "x"}]}}


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(server, "ENABLE_SENSITIVE_WRITES", True)


@pytest.fixture
def zd(monkeypatch):
    get = mock.Mock(return_value={"ticket": dict(TICKET)})
    put = mock.Mock(return_value=_put_ok())
    monkeypatch.setattr(server, "zd_get", get)
    monkeypatch.setattr(server, "zd_put", put)
    return get, put


def _token_from(preview: str) -> str:
    return preview.split('confirm_token="', 1)[1].split('"', 1)[0]


# ── 設定 ──────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    (None, False), ("", False), ("0", False), ("false", False), ("unknown", False),
    ("1", True), ("true", True), ("True", True), ("yes", True), ("on", True),
])
def test_opt_in_env_defaults_to_off(value, expected):
    assert server._parse_opt_in_env(value) is expected


def test_disabled_hides_tool_and_refuses(monkeypatch, zd):
    monkeypatch.setattr(server, "ENABLE_SENSITIVE_WRITES", False)
    names = [t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
    assert "add_internal_note" not in names
    out = server.tool_add_internal_note({"ticket_id": 42, "body": "メモ"})
    assert "無効化" in out
    zd[0].assert_not_called()
    zd[1].assert_not_called()


def test_enabled_lists_tool(enabled):
    names = [t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
    assert "add_internal_note" in names


def test_tool_schema_has_no_public_switch():
    tool = next(t for t in server.TOOLS if t["name"] == "add_internal_note")
    assert set(tool["inputSchema"]["properties"]) == {"ticket_id", "body", "confirm_token"}


# ── 2 段階投稿 ────────────────────────────────────────

def test_preview_does_not_write(enabled, zd):
    out = server.tool_add_internal_note({"ticket_id": 42, "body": "調査メモ"})
    assert "まだ投稿していません" in out and "テスト件名" in out and "調査メモ" in out
    zd[1].assert_not_called()


def test_confirmed_post_is_always_private(enabled, zd):
    token = _token_from(server.tool_add_internal_note({"ticket_id": 42, "body": "調査メモ"}))
    out = server.tool_add_internal_note({"ticket_id": 42, "body": "調査メモ", "confirm_token": token})
    assert "投稿しました" in out and "非公開を確認済み" in out
    zd[1].assert_called_once_with("/tickets/42.json", {"ticket": {"comment": {"body": "調査メモ", "public": False}}})


def test_public_flag_in_args_is_ignored(enabled, zd):
    token = _token_from(server.tool_add_internal_note({"ticket_id": 42, "body": "b"}))
    server.tool_add_internal_note({"ticket_id": 42, "body": "b", "confirm_token": token, "public": True})
    assert zd[1].call_args[0][1]["ticket"]["comment"]["public"] is False


@pytest.mark.parametrize("change", [{"body": "すり替え"}, {"ticket_id": 43}])
def test_token_bound_to_ticket_and_body(enabled, zd, change):
    token = _token_from(server.tool_add_internal_note({"ticket_id": 42, "body": "元の本文"}))
    args = {"ticket_id": 42, "body": "元の本文", "confirm_token": token, **change}
    out = server.tool_add_internal_note(args)
    assert "中止" in out
    zd[1].assert_not_called()


def test_expired_token_rejected(enabled, zd, monkeypatch):
    token = _token_from(server.tool_add_internal_note({"ticket_id": 42, "body": "b"}))
    real_time = server.time.time
    monkeypatch.setattr(server.time, "time", lambda: real_time() + server._NOTE_TOKEN_TTL + 1)
    out = server.tool_add_internal_note({"ticket_id": 42, "body": "b", "confirm_token": token})
    assert "有効期限" in out
    zd[1].assert_not_called()


@pytest.mark.parametrize("token", ["garbage", "123.abc", ""])
def test_forged_token_rejected(enabled, zd, token):
    out = server.tool_add_internal_note({"ticket_id": 42, "body": "b", "confirm_token": token})
    if token:  # 空文字はプレビュー扱い
        assert "中止" in out
    zd[1].assert_not_called()


def test_closed_ticket_refused(enabled, zd):
    zd[0].return_value = {"ticket": {**TICKET, "status": "closed"}}
    assert "クローズ済み" in server.tool_add_internal_note({"ticket_id": 42, "body": "b"})


def test_empty_body_refused(enabled, zd):
    assert "空" in server.tool_add_internal_note({"ticket_id": 42, "body": "  "})
    zd[0].assert_not_called()


def test_recorded_as_public_raises(enabled, zd):
    zd[1].return_value = _put_ok(public=True)
    token = _token_from(server.tool_add_internal_note({"ticket_id": 42, "body": "b"}))
    with pytest.raises(RuntimeError, match="公開コメント"):
        server.tool_add_internal_note({"ticket_id": 42, "body": "b", "confirm_token": token})


def test_missing_comment_event_raises(enabled, zd):
    zd[1].return_value = {"audit": {"events": []}}
    token = _token_from(server.tool_add_internal_note({"ticket_id": 42, "body": "b"}))
    with pytest.raises(RuntimeError):
        server.tool_add_internal_note({"ticket_id": 42, "body": "b", "confirm_token": token})


# ── 記事公開ガード ────────────────────────────────────

def test_kb_publish_refused_when_disabled(monkeypatch, zd):
    monkeypatch.setattr(server, "ENABLE_SENSITIVE_WRITES", False)
    out = server.tool_update_kb_article({"article_id": 1, "draft": False})
    assert "無効化" in out
    zd[1].assert_not_called()


def test_kb_draft_update_still_allowed_when_disabled(monkeypatch, zd):
    monkeypatch.setattr(server, "ENABLE_SENSITIVE_WRITES", False)
    zd[1].return_value = {"translation": {"draft": True, "title": "t"}}
    server.tool_update_kb_article({"article_id": 1, "draft": True, "body": "b"})
    zd[1].assert_called_once()


def test_kb_publish_allowed_when_enabled(enabled, zd):
    zd[1].return_value = {"translation": {"draft": False, "title": "t"}}
    out = server.tool_update_kb_article({"article_id": 1, "draft": False})
    assert "公開済み" in out
