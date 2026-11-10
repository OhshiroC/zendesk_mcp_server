"""必須設定 (サブドメイン・OAuth クライアント ID) のチェックのテスト。

What: 設定が空・不正なままツールを呼ぶと、開けない認可 URL を出さずに直し方を返すこと、
      サブドメインの入力ゆれ (URL・ホスト名ごと) を吸収することを検証する。
"""

import pytest

import server


@pytest.mark.parametrize("raw, expected", [
    ("yourcompany", "yourcompany"),
    ("  YourCompany ", "yourcompany"),
    ("yourcompany.zendesk.com", "yourcompany"),
    ("https://yourcompany.zendesk.com/", "yourcompany"),
    ("https://yourcompany.zendesk.com/agent/tickets/1", "yourcompany"),
    ("", ""),
    (None, ""),
])
def test_normalize_subdomain(raw, expected):
    assert server._normalize_subdomain(raw) == expected


@pytest.mark.parametrize("sub, cid, ok", [
    ("yourcompany", "client", True),
    ("", "client", False),
    ("yourcompany", "", False),
    ("", "", False),
    ("your company", "client", False),
])
def test_config_error(monkeypatch, sub, cid, ok):
    monkeypatch.setattr(server, "ZENDESK_SUBDOMAIN", sub)
    monkeypatch.setattr(server, "ZENDESK_OAUTH_CLIENT_ID", cid)
    assert (server._config_error() == "") is ok


def test_missing_fields_are_named(monkeypatch):
    monkeypatch.setattr(server, "ZENDESK_SUBDOMAIN", "")
    monkeypatch.setattr(server, "ZENDESK_OAUTH_CLIENT_ID", "")
    msg = server._config_error()
    assert "サブドメイン" in msg and "OAuth クライアント ID" in msg and "拡張機能" in msg


def _call(name="get_ticket_stats"):
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": {}}})


def test_tool_call_returns_guidance_without_touching_zendesk(monkeypatch):
    monkeypatch.setattr(server, "ZENDESK_SUBDOMAIN", "")
    monkeypatch.setattr(server, "ZENDESK_OAUTH_CLIENT_ID", "")

    def boom(*a, **k):
        raise AssertionError("Zendesk に問い合わせてはいけない")
    monkeypatch.setattr(server, "zd_get", boom)
    monkeypatch.setattr(server, "get_access_token", boom, raising=False)

    resp = _call()
    result = resp["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "設定が足りません" in text
    assert ".zendesk.com/oauth" not in text


def test_tool_call_proceeds_when_configured(monkeypatch):
    monkeypatch.setattr(server, "ZENDESK_SUBDOMAIN", "yourcompany")
    monkeypatch.setattr(server, "ZENDESK_OAUTH_CLIENT_ID", "client")
    monkeypatch.setattr(server, "TOOL_HANDLERS", {"get_ticket_stats": lambda a: "ok"})
    resp = _call()
    assert "isError" not in resp["result"]


def test_tools_list_still_works_without_config(monkeypatch):
    monkeypatch.setattr(server, "ZENDESK_SUBDOMAIN", "")
    monkeypatch.setattr(server, "ZENDESK_OAUTH_CLIENT_ID", "")
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert resp["result"]["tools"]
