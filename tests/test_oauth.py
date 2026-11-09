"""OAuth 認証のテスト。

What: トークンキャッシュの読み書き、期限切れ時の refresh、401 時の再試行、
      ブラウザ認可(PKCE + 127.0.0.1 コールバック)の一連の流れを検証する。
      Zendesk 本体には接続せず、/oauth/tokens への通信は _token_request を差し替える。
"""

import base64
import hashlib
import io
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

import pytest

import server


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def oauth_env(tmp_path, monkeypatch):
    port = _free_port()
    monkeypatch.setattr(server, "ZENDESK_OAUTH_CLIENT_ID", "test_client")
    monkeypatch.setattr(server, "ZENDESK_OAUTH_TOKEN_CACHE", str(tmp_path / "token.json"))
    monkeypatch.setattr(server, "ZENDESK_OAUTH_REDIRECT_PORT", port)
    monkeypatch.setattr(server, "_REDIRECT_URI", f"http://127.0.0.1:{port}/callback")
    monkeypatch.setattr(server, "_AUTH_WAIT_SECONDS", 5)
    monkeypatch.setattr(server, "_pending_auth", None)
    yield


def _token(access="at", refresh="rt", expires_in=1800):
    return {
        "client_id":     "test_client",
        "access_token":  access,
        "refresh_token": refresh,
        "expires_at":    time.time() + expires_in,
    }


# ── トークンキャッシュ ─────────────────────────────────────

def test_save_token_is_owner_only(tmp_path):
    server._save_token(_token())
    mode = os.stat(server._token_cache_path()).st_mode & 0o777
    assert mode == 0o600
    assert server._load_token()["access_token"] == "at"


def test_token_for_other_client_is_ignored():
    server._save_token({**_token(), "client_id": "other_client"})
    assert server._load_token() is None


def test_valid_cached_token_is_used_without_network():
    server._save_token(_token(access="cached"))
    with mock.patch.object(server, "_token_request") as tr:
        assert server._get_access_token() == "cached"
    tr.assert_not_called()


# ── refresh ───────────────────────────────────────────────

def test_expired_token_is_refreshed_and_rotated():
    server._save_token(_token(access="old", refresh="rt1", expires_in=-10))
    new = _token(access="new", refresh="rt2")
    with mock.patch.object(server, "_token_request", return_value=new) as tr:
        assert server._get_access_token() == "new"
    tr.assert_called_once_with({"grant_type": "refresh_token", "refresh_token": "rt1"})
    # refresh token は使い捨てなので、新しいものが保存されていること
    assert server._load_token()["refresh_token"] == "rt2"


def test_zero_expires_at_is_treated_as_expired():
    # expires_at=0 を「期限なし」と誤認しないこと(実機スモークテストで見つかった不具合)
    server._save_token({**_token(access="old"), "expires_at": 0})
    with mock.patch.object(server, "_token_request", return_value=_token(access="new")):
        assert server._get_access_token() == "new"


def test_failed_refresh_discards_cache_and_starts_browser_auth():
    server._save_token(_token(expires_in=-10))
    err = urllib.error.HTTPError("u", 400, "invalid_grant", {}, io.BytesIO())
    with mock.patch.object(server, "_token_request", side_effect=err), \
         mock.patch.object(server, "_AUTH_WAIT_SECONDS", 0), \
         mock.patch.object(server.webbrowser, "open") as wb:
        with pytest.raises(server.AuthRequired) as exc:
            server._get_access_token()
    assert server._load_token() is None
    wb.assert_called_once()
    assert "/oauth/authorizations/new?" in str(exc.value)
    server._pending_auth.done.set()


# ── ブラウザ認可 ──────────────────────────────────────────

def _simulate_user_approves(url: str):
    """webbrowser.open の代わりに、Zendesk が認可後にリダイレクトする動作を再現する。"""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    _simulate_user_approves.query = q
    callback = q["redirect_uri"][0] + "?" + urllib.parse.urlencode({"code": "the_code", "state": q["state"][0]})
    with urllib.request.urlopen(callback, timeout=5) as resp:
        assert resp.status == 200


def test_browser_flow_exchanges_code_with_pkce():
    issued = _token(access="fresh")
    with mock.patch.object(server, "_token_request", return_value=issued) as tr, \
         mock.patch.object(server.webbrowser, "open", side_effect=_simulate_user_approves):
        assert server._get_access_token() == "fresh"

    q = _simulate_user_approves.query
    assert q["client_id"] == ["test_client"]
    assert q["code_challenge_method"] == ["S256"]
    params = tr.call_args.args[0]
    assert params["grant_type"] == "authorization_code"
    assert params["code"] == "the_code"
    # code_verifier から code_challenge が導出できること(PKCE S256)
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(params["code_verifier"].encode()).digest()
    ).decode().rstrip("=")
    assert q["code_challenge"] == [expected]
    assert server._load_token()["access_token"] == "fresh"


def test_callback_with_wrong_state_is_rejected():
    with mock.patch.object(server.webbrowser, "open"):
        pending = server._start_or_get_pending_auth()
    bad = server._REDIRECT_URI + "?" + urllib.parse.urlencode({"code": "x", "state": "forged"})
    with mock.patch.object(server, "_token_request") as tr:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(bad, timeout=5)
        assert exc.value.code == 400
        tr.assert_not_called()
    assert not pending.done.is_set()
    pending.done.set()


def test_auth_required_is_returned_as_tool_error():
    with mock.patch.object(server, "_get_access_token",
                           side_effect=server.AuthRequired("認可してください")):
        resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "list_user_fields", "arguments": {}}})
    assert resp["result"]["isError"] is True
    assert "認可してください" in resp["result"]["content"][0]["text"]


# ── API 呼び出し ──────────────────────────────────────────

class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_request_sends_bearer_and_retries_once_on_401():
    server._save_token(_token(access="stale"))
    seen = []

    def fake_urlopen(req, timeout, context):
        seen.append(req.get_header("Authorization"))
        if len(seen) == 1:
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO())
        return _Resp(json.dumps({"ok": True}).encode())

    with mock.patch.object(server.urllib.request, "urlopen", side_effect=fake_urlopen), \
         mock.patch.object(server, "_token_request", return_value=_token(access="renewed")):
        assert server.zd_get("/users/me.json") == {"ok": True}
    assert seen == ["Bearer stale", "Bearer renewed"]
