"""stdio / JSON-RPC 層のテスト。

What: 通知 (id なし) に応答しないこと、ping に応答すること、Windows のように
      stdio が UTF-8 以外 (cp932 / cp1252) で開かれていても UTF-8 の JSON を
      読み書きできることを検証する。Zendesk 本体には接続しない。
"""

import json
import os
import subprocess
import sys

import pytest

import server

SERVER_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server.py")


@pytest.mark.parametrize("method", ["notifications/initialized", "notifications/cancelled", "notifications/unknown"])
def test_notifications_get_no_response(method):
    assert server.handle({"jsonrpc": "2.0", "method": method, "params": {}}) is None


def test_ping_returns_empty_result():
    assert server.handle({"jsonrpc": "2.0", "id": 7, "method": "ping"}) == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_unknown_method_with_id_is_error():
    resp = server.handle({"jsonrpc": "2.0", "id": 8, "method": "nope"})
    assert resp["id"] == 8 and resp["error"]["code"] == -32601


@pytest.mark.parametrize("encoding", ["cp932", "cp1252"])
def test_stdio_is_utf8_even_with_non_utf8_locale(encoding):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env.update(PYTHONIOENCODING=encoding, ZENDESK_SUBDOMAIN="x", ZENDESK_OAUTH_CLIENT_ID="x", ZENDESK_MASK_PII="true")
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "テスト📊"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    stdin = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in msgs).encode("utf-8")
    r = subprocess.run([sys.executable, SERVER_PY], input=stdin, capture_output=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    lines = [json.loads(l.decode("utf-8")) for l in r.stdout.split(b"\n") if l]
    assert [l["id"] for l in lines] == [1, 2]
    assert any("チケット" in t["description"] for t in lines[1]["result"]["tools"])
    assert b"\r\n" not in r.stdout
