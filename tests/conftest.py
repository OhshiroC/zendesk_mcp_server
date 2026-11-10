"""テスト共通の設定。"""

import pytest

import server


@pytest.fixture(autouse=True)
def _configured_server(monkeypatch):
    # 必須設定が空だと tools/call は設定エラーを返す (test_config_check.py 参照)。
    # それ以外のテストは「設定済み」の状態で動かす。
    monkeypatch.setattr(server, "ZENDESK_SUBDOMAIN", "example")
    monkeypatch.setattr(server, "ZENDESK_OAUTH_CLIENT_ID", "test_client")
