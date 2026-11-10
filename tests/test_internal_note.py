"""社内メモ投稿 (add_internal_note) と書き込み系ガードのテスト。

What: 設定 OFF 時はツールが隠れて実行も拒否されること、投稿は常に public=False で
      送られ、公開として記録されたら失敗扱いになること、投稿者 ID を指定した場合は
      エージェント/管理者のときだけ author_id 付きで投稿すること、記事の公開
      (draft=False) が設定 OFF では拒否されることを検証する。
      Zendesk には接続せず zd_get / zd_put を差し替える。
"""

from unittest import mock

import pytest

import server


USERS = {
    500: {"id": 500, "name": "Claude (AI)", "role": "agent", "active": True},
    501: {"id": 501, "name": "顧客", "role": "end-user", "active": True},
    502: {"id": 502, "name": "停止中", "role": "agent", "active": False},
}


def _put_ok(public=False, author_id=1):
    return {"audit": {"events": [{"type": "Comment", "id": 999, "public": public, "author_id": author_id}]}}


def _fake_get(path, params=None):
    uid = int(path.split("/")[2].split(".")[0])
    return {"user": USERS[uid]}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(server, "ZENDESK_NOTE_AUTHOR_ID", "")
    server._NOTE_AUTHOR_CACHE.clear()
    yield
    server._NOTE_AUTHOR_CACHE.clear()


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(server, "ENABLE_SENSITIVE_WRITES", True)


@pytest.fixture
def zd(monkeypatch):
    get = mock.Mock(side_effect=_fake_get)
    put = mock.Mock(return_value=_put_ok())
    monkeypatch.setattr(server, "zd_get", get)
    monkeypatch.setattr(server, "zd_put", put)
    return get, put


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
    assert "無効化" in server.tool_add_internal_note({"ticket_id": 42, "body": "メモ"})
    zd[1].assert_not_called()


def test_enabled_lists_tool(enabled):
    names = [t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
    assert "add_internal_note" in names


def test_tool_schema_has_no_public_or_author_switch():
    tool = next(t for t in server.TOOLS if t["name"] == "add_internal_note")
    assert set(tool["inputSchema"]["properties"]) == {"ticket_id", "body", "kind"}


# ── 種類 (memo / draft) ───────────────────────────────

def test_memo_is_default_and_adds_no_tag(enabled, zd):
    out = server.tool_add_internal_note({"ticket_id": 42, "body": "調査メモ"})
    assert zd[1].call_args[0][1] == {"ticket": {"comment": {"body": "調査メモ", "public": False}}}
    assert "種類: 社内メモ" in out


def test_draft_adds_marker_and_tag(enabled, zd):
    out = server.tool_add_internal_note({"ticket_id": 42, "body": "お問い合わせありがとうございます。", "kind": "draft"})
    (c1, c2) = zd[1].call_args_list
    assert c1.args == ("/tickets/42.json", {"ticket": {"comment": {"body": "【返信下書き】\nお問い合わせありがとうございます。", "public": False}}})
    # PUT /tags.json は既存タグを残して追加する (POST だと置き換えになる)
    assert c2.args == ("/tickets/42/tags.json", {"tags": ["claude_draft"]})
    assert "返信下書き" in out and "⚠️" not in out


def test_draft_tag_failure_is_reported_not_raised(enabled, zd):
    zd[1].side_effect = [_put_ok(), RuntimeError("403")]
    out = server.tool_add_internal_note({"ticket_id": 42, "body": "b", "kind": "draft"})
    assert "投稿しました" in out and "タグの付与に失敗" in out


def test_draft_marker_not_duplicated(enabled, zd):
    server.tool_add_internal_note({"ticket_id": 42, "body": "【返信下書き】\n本文", "kind": "draft"})
    assert zd[1].call_args_list[0].args[1]["ticket"]["comment"]["body"] == "【返信下書き】\n本文"


def test_unknown_kind_refused(enabled, zd):
    assert "memo か draft" in server.tool_add_internal_note({"ticket_id": 42, "body": "b", "kind": "public"})
    zd[1].assert_not_called()


# ── 投稿 ──────────────────────────────────────────────

def test_post_is_always_private_and_immediate(enabled, zd):
    out = server.tool_add_internal_note({"ticket_id": 42, "body": "調査メモ"})
    assert "投稿しました" in out and "非公開を確認済み" in out and "ログイン中のユーザー" in out
    zd[1].assert_called_once_with("/tickets/42.json", {"ticket": {"comment": {"body": "調査メモ", "public": False}}})


def test_public_flag_in_args_is_ignored(enabled, zd):
    server.tool_add_internal_note({"ticket_id": 42, "body": "b", "public": True, "author_id": 501})
    comment = zd[1].call_args[0][1]["ticket"]["comment"]
    assert comment == {"body": "b", "public": False}


def test_empty_body_refused(enabled, zd):
    assert "空" in server.tool_add_internal_note({"ticket_id": 42, "body": "  "})
    zd[1].assert_not_called()


def test_recorded_as_public_raises(enabled, zd):
    zd[1].return_value = _put_ok(public=True)
    with pytest.raises(RuntimeError, match="公開コメント"):
        server.tool_add_internal_note({"ticket_id": 42, "body": "b"})


def test_missing_comment_event_raises(enabled, zd):
    zd[1].return_value = {"audit": {"events": []}}
    with pytest.raises(RuntimeError):
        server.tool_add_internal_note({"ticket_id": 42, "body": "b"})


# ── 投稿者の変更 ──────────────────────────────────────

def test_author_id_sent_when_configured_agent(enabled, zd, monkeypatch):
    monkeypatch.setattr(server, "ZENDESK_NOTE_AUTHOR_ID", "500")
    zd[1].return_value = _put_ok(author_id=500)
    out = server.tool_add_internal_note({"ticket_id": 42, "body": "b"})
    assert zd[1].call_args[0][1]["ticket"]["comment"] == {"body": "b", "public": False, "author_id": 500}
    assert "Claude (AI)（ID: 500）" in out and "⚠️" not in out


@pytest.mark.parametrize("uid", ["501", "502"])
def test_non_agent_or_inactive_author_refused(enabled, zd, monkeypatch, uid):
    monkeypatch.setattr(server, "ZENDESK_NOTE_AUTHOR_ID", uid)
    with pytest.raises(RuntimeError, match="エージェント"):
        server.tool_add_internal_note({"ticket_id": 42, "body": "b"})
    zd[1].assert_not_called()


def test_non_numeric_author_refused(enabled, zd, monkeypatch):
    monkeypatch.setattr(server, "ZENDESK_NOTE_AUTHOR_ID", "claude")
    with pytest.raises(RuntimeError, match="数値"):
        server.tool_add_internal_note({"ticket_id": 42, "body": "b"})
    zd[1].assert_not_called()


def test_author_verified_once(enabled, zd, monkeypatch):
    monkeypatch.setattr(server, "ZENDESK_NOTE_AUTHOR_ID", "500")
    zd[1].return_value = _put_ok(author_id=500)
    server.tool_add_internal_note({"ticket_id": 42, "body": "b"})
    server.tool_add_internal_note({"ticket_id": 43, "body": "c"})
    assert zd[0].call_count == 1


def test_warns_when_zendesk_ignores_author(enabled, zd, monkeypatch):
    monkeypatch.setattr(server, "ZENDESK_NOTE_AUTHOR_ID", "500")
    zd[1].return_value = _put_ok(author_id=1)
    out = server.tool_add_internal_note({"ticket_id": 42, "body": "b"})
    assert "⚠️" in out and "指定どおりになっていません" in out


# ── 記事公開ガード ────────────────────────────────────

def test_kb_publish_refused_when_disabled(monkeypatch, zd):
    monkeypatch.setattr(server, "ENABLE_SENSITIVE_WRITES", False)
    assert "無効化" in server.tool_update_kb_article({"article_id": 1, "draft": False})
    zd[1].assert_not_called()


def test_kb_draft_update_still_allowed_when_disabled(monkeypatch, zd):
    monkeypatch.setattr(server, "ENABLE_SENSITIVE_WRITES", False)
    zd[1].return_value = {"translation": {"draft": True, "title": "t"}}
    server.tool_update_kb_article({"article_id": 1, "draft": True, "body": "b"})
    zd[1].assert_called_once()


def test_kb_publish_allowed_when_enabled(enabled, zd):
    zd[1].return_value = {"translation": {"draft": False, "title": "t"}}
    assert "公開済み" in server.tool_update_kb_article({"article_id": 1, "draft": False})
