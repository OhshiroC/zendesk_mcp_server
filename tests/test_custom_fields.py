"""チケットのカスタムフィールド表示のテスト。

What: server.py の get_ticket が返すカスタムフィールド節(値ありフィールドの抽出、
      表示名の解決とキャッシュ、氏名フィールドのフィールドレベルマスク)が
      仕様どおりに振る舞うことを検証する。
"""

from unittest import mock

import pytest

import server


_FAKE_FIELDS = {
    "ticket_fields": [
        {"id": 101, "title": "製品名"},
        {"id": 102, "title": "シリアル番号"},
        {"id": 103, "title": "お名前"},
        {"id": 104, "title": "発生状況"},
    ]
}


@pytest.fixture(autouse=True)
def _reset_field_title_cache():
    # 表示名キャッシュはプロセス内グローバルなので、テスト間で持ち越さない。
    server._TICKET_FIELD_TITLES = None
    yield
    server._TICKET_FIELD_TITLES = None


def _format(custom_fields, mask_pii=True):
    with mock.patch.object(server, "MASK_PII", mask_pii), \
         mock.patch.object(server, "zd_get", return_value=_FAKE_FIELDS):
        return server._format_custom_fields({"custom_fields": custom_fields})


# ── 値ありフィールドだけを行にする ───────────────────────────

def test_value_field_is_rendered_with_title():
    assert _format([{"id": 101, "value": "Nature Remo 3"}]) == ["- 製品名: Nature Remo 3"]


@pytest.mark.parametrize("value", [None, "", False, []])
def test_empty_value_is_skipped(value):
    assert _format([{"id": 101, "value": value}]) == []


def test_zero_and_false_string_are_kept():
    # 0 や "false" は「値なし」ではないので残す。
    assert _format([{"id": 101, "value": 0}]) == ["- 製品名: 0"]
    assert _format([{"id": 102, "value": "false"}]) == ["- シリアル番号: false"]


def test_list_value_is_joined():
    assert _format([{"id": 104, "value": ["a", "b"]}]) == ["- 発生状況: a, b"]


def test_custom_fields_absent_or_null():
    assert server._format_custom_fields({}) == []
    assert server._format_custom_fields({"custom_fields": None}) == []


# ── 表示名の解決 ─────────────────────────────────────────────

def test_unknown_id_falls_back_to_field_id():
    assert _format([{"id": 999, "value": "x"}]) == ["- field_999: x"]


def test_field_titles_are_fetched_once_and_cached():
    with mock.patch.object(server, "zd_get", return_value=_FAKE_FIELDS) as m:
        server._ticket_field_titles()
        server._ticket_field_titles()
    assert m.call_count == 1


def test_field_titles_degrade_to_empty_on_api_error():
    # 表示名が引けなくても get_ticket 自体は落とさない(id フォールバックで表示する)。
    with mock.patch.object(server, "zd_get", side_effect=RuntimeError("boom")):
        assert server._ticket_field_titles() == {}


# ── 氏名フィールドのフィールドレベルマスク ───────────────────

def test_name_field_masked_when_on():
    out = _format([{"id": 103, "value": "山田太郎"}], mask_pii=True)
    assert out == ["- お名前: [NAME]"]


def test_name_field_passthrough_when_off():
    out = _format([{"id": 103, "value": "山田太郎"}], mask_pii=False)
    assert out == ["- お名前: 山田太郎"]


def test_non_name_field_not_masked():
    out = _format([{"id": 102, "value": "1W2XXXXXXXXX"}], mask_pii=True)
    assert out == ["- シリアル番号: 1W2XXXXXXXXX"]


# ── get_ticket への組み込み ──────────────────────────────────

_FAKE_TICKET = {
    "id": 1,
    "subject": "エアコンが操作できない",
    "status": "open",
    "priority": "normal",
    "created_at": "2026-08-01T00:00:00Z",
    "updated_at": "2026-08-02T00:00:00Z",
    "requester_id": 10,
    "assignee_id": 20,
    "tags": ["ac"],
}


def _get_ticket(custom_fields):
    ticket = dict(_FAKE_TICKET, custom_fields=custom_fields)

    def fake_zd_get(path, params=None):
        if path.startswith("/tickets/"):
            return {"ticket": ticket, "comments": []}
        return _FAKE_FIELDS

    with mock.patch.object(server, "MASK_PII", True), \
         mock.patch.object(server, "zd_get", side_effect=fake_zd_get):
        return server.tool_get_ticket({"ticket_id": 1})


def test_get_ticket_renders_custom_field_section():
    out = _get_ticket([{"id": 101, "value": "Nature Remo 3"}])
    assert "## カスタムフィールド" in out
    assert "- 製品名: Nature Remo 3" in out
    assert "## コメント" in out


def test_get_ticket_omits_section_when_no_values():
    out = _get_ticket([{"id": 101, "value": None}])
    assert "## カスタムフィールド" not in out
    assert "## コメント" in out
