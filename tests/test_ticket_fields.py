"""チケットのカスタムフィールドの定義一覧・選択肢の表示名・一覧表示・検索条件のテスト。

What: list_ticket_fields、ドロップダウン値の表示名への変換、search_tickets / list_tickets の
      custom_fields (各行への表示) と custom_field_filters (custom_field_<id>:<値> への変換) を検証する。
"""

from unittest import mock

import pytest

import server


_FIELDS = {
    "ticket_fields": [
        {"id": 1, "title": "件名", "type": "subject", "removable": False, "active": True},
        {"id": 101, "title": "製品名", "type": "tagger", "removable": True, "active": True,
         "custom_field_options": [
             {"name": "Model X", "value": "model_x"},
             {"name": "Model X mini", "value": "model_x_mini"},
             {"name": "周辺機器::Hub Y", "value": "hub_y"},
         ]},
        {"id": 102, "title": "OS", "type": "multiselect", "removable": True, "active": True,
         "custom_field_options": [
             {"name": "iOS", "value": "os_ios"},
             {"name": "Android", "value": "os_android"},
         ]},
        {"id": 103, "title": "シリアル番号", "type": "text", "removable": True, "active": True},
        {"id": 104, "title": "旧フィールド", "type": "text", "removable": True, "active": False},
        {"id": 105, "title": "要折り返し", "type": "checkbox", "removable": True, "active": True},
        {"id": 106, "title": "製品名（旧）", "type": "text", "removable": True, "active": True},
    ]
}


@pytest.fixture(autouse=True)
def _reset_cache():
    server._TICKET_FIELD_TITLES = None
    yield
    server._TICKET_FIELD_TITLES = None


def _ticket(tid, **cf):
    return {
        "id": tid, "subject": "件名", "status": "open", "requester_id": 1, "assignee_id": 2,
        "created_at": "2026-11-01T00:00:00Z", "updated_at": "2026-11-02T00:00:00Z", "tags": [],
        "custom_fields": [{"id": int(k[1:]), "value": v} for k, v in cf.items()],
    }


def _fake(tickets=(), calls=None):
    def fake_zd_get(path, params=None):
        if calls is not None:
            calls.append((path, dict(params or {})))
        if path == "/ticket_fields.json":
            return _FIELDS
        if path == "/search.json":
            return {"results": list(tickets), "count": len(tickets)}
        if path == "/tickets.json":
            return {"tickets": list(tickets)}
        raise AssertionError(path)
    return fake_zd_get


@pytest.fixture
def zd(monkeypatch):
    monkeypatch.setattr(server, "MASK_PII", True)
    state = {"tickets": [], "calls": []}
    monkeypatch.setattr(server, "zd_get", lambda p, params=None: _fake(state["tickets"], state["calls"])(p, params))
    return state


# ── 選択肢の表示名 ─────────────────────────────────────────

def test_dropdown_value_shown_as_option_name(zd):
    rows = server._format_custom_fields(_ticket(1, f101="model_x", f102=["os_ios", "os_android"]))
    assert rows == ["- 製品名: Model X", "- OS: iOS, Android"]


def test_unknown_option_value_kept(zd):
    assert server._format_custom_fields(_ticket(1, f101="discontinued")) == ["- 製品名: discontinued"]


def test_field_definitions_are_paginated(monkeypatch):
    pages = {
        1: {"ticket_fields": [{"id": i, "title": f"f{i}"} for i in range(100)], "next_page": "x"},
        2: {"ticket_fields": [{"id": 1000, "title": "last"}], "next_page": None},
    }
    monkeypatch.setattr(server, "zd_get", lambda p, params=None: pages[params["page"]])
    assert server._ticket_field_titles()[1000] == "last"


def test_failed_definition_fetch_is_not_cached(monkeypatch):
    monkeypatch.setattr(server, "zd_get", mock.Mock(side_effect=RuntimeError("boom")))
    assert server._ticket_field_titles() == {}
    monkeypatch.setattr(server, "zd_get", lambda p, params=None: _FIELDS)
    assert server._ticket_field_titles()[101] == "製品名"


# ── フィールドの解決 ───────────────────────────────────────

@pytest.mark.parametrize("ref, fid", [
    (101, 101), ("101", 101), ("製品名", 101), ("os", 102), ("シリアル", 103),
])
def test_resolve_field(zd, ref, fid):
    assert server._resolve_ticket_field(ref) == fid


def test_resolve_ambiguous_partial_match_raises(zd):
    # 「製品」は 製品名 と 製品名（旧） の両方に当たる
    with pytest.raises(ValueError, match="複数"):
        server._resolve_ticket_field("製品")


def test_resolve_exact_match_wins_over_partial(zd):
    assert server._resolve_ticket_field("製品名") == 101


def test_resolve_unknown_raises(zd):
    with pytest.raises(ValueError, match="list_ticket_fields"):
        server._resolve_ticket_field("存在しない")
    with pytest.raises(ValueError, match="存在しません"):
        server._resolve_ticket_field(999)


# ── 検索条件 ───────────────────────────────────────────────

@pytest.mark.parametrize("filters, expected", [
    ({"製品名": "Model X"}, "custom_field_101:model_x"),        # 表示名
    ({"製品名": "model_x"}, "custom_field_101:model_x"),               # 値
    ({"製品名": "model x"}, "custom_field_101:model_x"),        # 大文字小文字
    ({"製品名": "Hub Y"}, "custom_field_101:hub_y"),               # 階層の末尾
    ({"OS": "iOS"}, "custom_field_102:os_ios"),
    ({"103": "1W2 ABC"}, 'custom_field_103:"1W2 ABC"'),              # 空白入りは引用
    ({"要折り返し": True}, "custom_field_105:true"),
    ({"製品名": "model_x", "OS": "Android"}, "custom_field_101:model_x custom_field_102:os_android"),
])
def test_custom_field_query(zd, filters, expected):
    assert server._custom_field_query(filters) == expected


def test_unknown_option_raises_with_examples(zd):
    with pytest.raises(ValueError, match="選択肢"):
        server._custom_field_query({"製品名": "Model Z"})


def test_search_tickets_adds_filter_to_query(zd):
    server.tool_search_tickets({"keyword": "接続", "custom_field_filters": {"製品名": "Model X"}})
    path, params = [c for c in zd["calls"] if c[0] == "/search.json"][0]
    assert params["query"] == 'type:ticket "接続" custom_field_101:model_x'


def test_list_tickets_switches_to_search_with_filter(zd):
    out = server.tool_list_tickets({"status": "open", "custom_field_filters": {"OS": "iOS"}})
    path, params = [c for c in zd["calls"] if c[0] in ("/search.json", "/tickets.json")][0]
    assert path == "/search.json"
    assert params["query"] == "type:ticket status:open custom_field_102:os_ios"
    assert "custom_field_102:os_ios" in out.splitlines()[0]


def test_list_tickets_without_filter_uses_tickets_endpoint(zd):
    server.tool_list_tickets({"status": "open"})
    assert any(c[0] == "/tickets.json" for c in zd["calls"])


# ── 一覧への表示 ───────────────────────────────────────────

def test_search_line_shows_selected_fields_in_order(zd):
    zd["tickets"] = [_ticket(7, f101="model_x", f102=["os_ios"], f103="1W2X")]
    out = server.tool_search_tickets({"custom_fields": ["OS", "製品名"]})
    line = [l for l in out.splitlines() if l.startswith("[#7]")][0]
    assert line.endswith(" | OS: iOS; 製品名: Model X")
    assert "シリアル" not in line


def test_list_line_shows_all_fields(zd):
    zd["tickets"] = [_ticket(7, f101="model_x_mini", f103="1W2X", f105=False)]
    out = server.tool_list_tickets({"include_all_custom_fields": True})
    line = [l for l in out.splitlines() if l.startswith("[#7]")][0]
    assert line.endswith(" | 製品名: Model X mini; シリアル番号: 1W2X")


def test_lines_unchanged_without_field_args(zd):
    zd["tickets"] = [_ticket(7, f101="model_x")]
    out = server.tool_list_tickets({})
    line = [l for l in out.splitlines() if l.startswith("[#7]")][0]
    assert line.endswith("タグ: ")


def test_inline_value_newlines_flattened(zd, monkeypatch):
    monkeypatch.setattr(server, "MASK_PII", False)
    zd["tickets"] = [_ticket(7, f103="a\nb")]
    out = server.tool_list_tickets({"custom_fields": [103]})
    assert "シリアル番号: a b" in out


def test_name_field_masked_in_list(zd):
    _FIELDS["ticket_fields"].append({"id": 107, "title": "お名前", "type": "text", "removable": True})
    try:
        zd["tickets"] = [_ticket(7, f107="山田太郎")]
        out = server.tool_list_tickets({"custom_fields": ["お名前"]})
    finally:
        _FIELDS["ticket_fields"].pop()
    assert "山田太郎" not in out and "お名前: [NAME]" in out


# ── list_ticket_fields ─────────────────────────────────────

def test_list_ticket_fields_default_hides_system_and_inactive(zd):
    out = server.tool_list_ticket_fields({})
    assert "[ID 101] 製品名（tagger）" in out
    assert "    - Model X = model_x" in out
    assert "件名" not in out and "旧フィールド" not in out


def test_list_ticket_fields_include_flags(zd):
    out = server.tool_list_ticket_fields({"include_inactive": True, "include_system": True})
    assert "[ID 1] 件名（subject） [標準]" in out
    assert "[ID 104] 旧フィールド（text） [無効]" in out


def test_list_ticket_fields_keyword_matches_option_name(zd):
    out = server.tool_list_ticket_fields({"keyword": "mini"})
    assert "[ID 101] 製品名" in out
    assert "Model X mini = model_x_mini" in out
    assert "    - Model X = model_x" not in out.splitlines()  # 当たった選択肢だけ
    assert "OS" not in out


def test_list_ticket_fields_truncates_options(zd):
    out = server.tool_list_ticket_fields({"max_options": 1})
    assert "…他 2 件" in out


def test_system_types_hidden_even_if_removable(zd):
    _FIELDS["ticket_fields"].append({"id": 2, "title": "優先度", "type": "priority", "removable": True})
    try:
        assert "優先度" not in server.tool_list_ticket_fields({})
        assert "[ID 2] 優先度（priority） [標準]" in server.tool_list_ticket_fields({"include_system": True})
    finally:
        _FIELDS["ticket_fields"].pop()


def test_list_ticket_fields_handles_fetch_failure(monkeypatch):
    monkeypatch.setattr(server, "zd_get", mock.Mock(side_effect=RuntimeError("boom")))
    assert "取得できませんでした" in server.tool_list_ticket_fields({})


def test_tool_is_registered():
    assert "list_ticket_fields" in server.TOOL_HANDLERS
    assert any(t["name"] == "list_ticket_fields" for t in server.TOOLS)


def test_filter_error_is_returned_as_tool_error(zd):
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "search_tickets",
                                     "arguments": {"custom_field_filters": {"存在しない": "x"}}}})
    assert "list_ticket_fields" in resp["error"]["message"]
