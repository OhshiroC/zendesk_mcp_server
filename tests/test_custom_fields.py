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


# ── 自由記述以外は redaction を通さない (main の文単位 redaction との統合) ──

_TYPED_FIELDS = {
    "ticket_fields": [
        {"id": 201, "title": "シリアルナンバー", "type": "text"},
        {"id": 202, "title": "ログイン・アカウント作成の内容", "type": "tagger"},
        {"id": 203, "title": "Total time spent (sec)", "type": "integer"},
        {"id": 204, "title": "発生状況", "type": "textarea"},
        {"id": 205, "title": "配送先住所", "type": "text"},
    ]
}


def _format_typed(custom_fields, mask_pii=True):
    with mock.patch.object(server, "MASK_PII", mask_pii), \
         mock.patch.object(server, "zd_get", return_value=_TYPED_FIELDS):
        return server._format_custom_fields({"custom_fields": custom_fields})


@pytest.mark.parametrize("fid, value", [
    (201, "1W012345678901"),         # 識別子形式の値
    (201, "ab-1234567890123-456"),   # 会話ID形式
    (202, "account_削除"),            # 選択肢 (tagger)
    (203, 421),                       # 数値
])
def test_structured_or_identifier_values_are_not_redacted(fid, value):
    out = _format_typed([{"id": fid, "value": value}])
    assert out[0].endswith(f": {value}")


def test_address_titled_field_masked():
    assert _format_typed([{"id": 205, "value": "東京都渋谷区神南1-2-3"}]) == ["- 配送先住所: [ADDRESS]"]


def test_free_text_goes_through_redaction():
    with mock.patch.object(server, "mask_user_field", return_value="REDACTED") as m:
        out = _format_typed([{"id": 204, "value": "田中です。電源が入らない"}])
    m.assert_called_once()
    assert out == ["- 発生状況: REDACTED"]
