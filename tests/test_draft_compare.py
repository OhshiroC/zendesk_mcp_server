"""get_ticket のコメント表示と、返信下書きの比較 (compare_draft_replies) のテスト。

What: get_ticket がコメントごとに公開/社内メモ/返信下書きと投稿者 (顧客/エージェント/Claude)
      を表示し、100 件を超えるコメントもページを辿って取得し、文字数制限を指定できること。
      compare_draft_replies が下書きとその後のエージェントの公開返信を正しく組にし
      (顧客・Claude・エンドユーザーの公開コメントは返信とみなさない)、未返信・差し替えを
      区別し、類似度を計算することを検証する。Zendesk には接続しない。
"""

from unittest import mock

import pytest

import server

REQ, AGENT, CLAUDE, CC = 10, 20, 30, 40
ROLES = {REQ: "end-user", AGENT: "agent", CLAUDE: "agent", CC: "end-user"}


def _c(i, author, public, body):
    return {"id": i, "author_id": author, "public": public, "body": body,
            "created_at": f"2026-11-10T0{i % 10}:00:00Z"}


def _ticket(tid=1, tags=("claude_draft",)):
    return {"id": tid, "subject": "Wi-Fi に繋がらない", "status": "open", "priority": None,
            "created_at": "2026-11-10T00:00:00Z", "updated_at": "2026-11-10T00:00:00Z",
            "requester_id": REQ, "assignee_id": AGENT, "tags": list(tags), "custom_fields": []}


def _fake_zd(comments, tickets=None):
    tickets = tickets or {1: _ticket()}

    def get(path, params=None):
        if path == "/users/show_many.json":
            ids = [int(x) for x in params["ids"].split(",")]
            return {"users": [{"id": i, "role": ROLES[i]} for i in ids if i in ROLES]}
        if path.endswith("/comments.json"):
            tid = int(path.split("/")[2])
            items = comments[tid] if isinstance(comments, dict) else comments
            page = (params or {}).get("page", 1)
            chunk = items[(page - 1) * 100: page * 100]
            return {"comments": chunk, "next_page": "x" if page * 100 < len(items) else None}
        if path == "/search.json":
            return {"results": list(tickets.values()), "count": len(tickets), "next_page": None}
        if path.startswith("/tickets/"):
            return {"ticket": tickets[int(path.split("/")[2].split(".")[0])]}
        if path == "/ticket_fields.json":
            return {"ticket_fields": []}
        raise AssertionError(path)
    return get


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setattr(server, "ZENDESK_NOTE_AUTHOR_ID", str(CLAUDE))
    monkeypatch.setattr(server, "MASK_PII", False)
    server._TICKET_FIELD_TITLES = None
    yield
    server._TICKET_FIELD_TITLES = None


# ── get_ticket ────────────────────────────────────────

def test_get_ticket_labels_visibility_and_author(monkeypatch):
    cmts = [
        _c(1, REQ, True, "繋がりません"),
        _c(2, CLAUDE, False, "【返信下書き】\nルーターを再起動してください"),
        _c(3, AGENT, False, "調査中"),
        _c(4, AGENT, True, "ルーターを再起動してみてください"),
        _c(5, CC, True, "私も同じです"),
    ]
    monkeypatch.setattr(server, "zd_get", _fake_zd(cmts))
    out = server.tool_get_ticket({"ticket_id": 1})
    assert "コメント（5 件）" in out
    assert "[顧客 / 公開返信 /" in out
    assert "[Claude / 社内メモ・返信下書き /" in out
    assert "[エージェント / 社内メモ /" in out
    assert "[エージェント / 公開返信 /" in out
    assert "[エンドユーザー / 公開返信 /" in out


def test_get_ticket_fetches_more_than_100_comments(monkeypatch):
    cmts = [_c(i, REQ, True, f"c{i}") for i in range(1, 251)]
    monkeypatch.setattr(server, "zd_get", _fake_zd(cmts))
    out = server.tool_get_ticket({"ticket_id": 1})
    assert "コメント（250 件）" in out and "\nc250" in out


@pytest.mark.parametrize("limit, full", [(800, False), (0, True)])
def test_get_ticket_body_limit(monkeypatch, limit, full):
    monkeypatch.setattr(server, "zd_get", _fake_zd([_c(1, REQ, True, "あ" * 1000)]))
    out = server.tool_get_ticket({"ticket_id": 1, "max_body_chars": limit})
    assert ("あ" * 1000 in out) is full
    assert ("以下 200 文字省略" in out) is (not full)


def test_get_ticket_survives_role_lookup_failure(monkeypatch):
    base = _fake_zd([_c(1, AGENT, True, "x")])
    def get(path, params=None):
        if path == "/users/show_many.json":
            raise RuntimeError("boom")
        return base(path, params)
    monkeypatch.setattr(server, "zd_get", get)
    assert "[エージェント / 公開返信 /" in server.tool_get_ticket({"ticket_id": 1})


# ── compare_draft_replies ─────────────────────────────

def test_pairs_draft_with_next_agent_public_reply(monkeypatch):
    cmts = [
        _c(1, REQ, True, "繋がりません"),
        _c(2, CLAUDE, False, "【返信下書き】\nルーターを再起動してください。"),
        _c(3, REQ, True, "まだですか"),            # 顧客の公開コメントは返信ではない
        _c(4, CC, True, "私も"),                   # CC のエンドユーザーも返信ではない
        _c(5, AGENT, False, "社内メモ"),           # 非公開は返信ではない
        _c(6, AGENT, True, "ルーターを再起動してください。"),
    ]
    monkeypatch.setattr(server, "zd_get", _fake_zd(cmts))
    out = server.tool_compare_draft_replies({"ticket_id": 1})
    assert "比較できた組: 1 件" in out and "類似度: 100%" in out
    assert "--- 実際の返信 ---\nルーターを再起動してください。" in out
    assert "【返信下書き】" not in out.split("--- 下書き ---")[1]  # 目印は比較から除く


def test_pending_and_superseded(monkeypatch):
    cmts = [
        _c(1, CLAUDE, False, "【返信下書き】\n案1"),
        _c(2, CLAUDE, False, "【返信下書き】\n案2"),
        _c(3, AGENT, True, "全然違う文章です"),
        _c(4, CLAUDE, False, "【返信下書き】\n案3"),
    ]
    monkeypatch.setattr(server, "zd_get", _fake_zd(cmts))
    out = server.tool_compare_draft_replies({"ticket_id": 1})
    assert "比較できた組: 1 件 | 未返信: 1 件 | 差し替え: 1 件" in out


def test_plain_memo_is_not_a_draft(monkeypatch):
    cmts = [_c(1, CLAUDE, False, "調査メモ: ログを確認"), _c(2, AGENT, True, "回答")]
    monkeypatch.setattr(server, "zd_get", _fake_zd(cmts))
    out = server.tool_compare_draft_replies({"ticket_id": 1})
    assert "見つかりませんでした" in out


def test_public_comment_with_marker_is_not_a_draft():
    assert not server._is_draft({"public": True, "body": "【返信下書き】x"})


def test_bulk_search_uses_tag_and_dates(monkeypatch):
    calls = []
    base = _fake_zd({1: [], 2: []}, {1: _ticket(1), 2: _ticket(2)})
    def get(path, params=None):
        calls.append((path, params))
        return base(path, params)
    monkeypatch.setattr(server, "zd_get", get)
    out = server.tool_compare_draft_replies({"updated_after": "2026-11-01", "max_tickets": 5})
    q = next(p for path, p in calls if path == "/search.json")["query"]
    assert q == "type:ticket tags:claude_draft updated>2026-11-01"
    assert "対象チケット: 2 件" in out


@pytest.mark.parametrize("a, b, expected", [
    ("abc", "abc", 100), ("a b\nc", "abc", 100), ("abc", "xyz", 0),
])
def test_similarity(a, b, expected):
    assert server._similarity(a, b) == expected


def test_compare_output_is_masked(monkeypatch):
    monkeypatch.setattr(server, "MASK_PII", True)
    cmts = [_c(1, CLAUDE, False, "【返信下書き】\ntest@example.com までご連絡ください"),
            _c(2, AGENT, True, "test@example.com までご連絡ください")]
    monkeypatch.setattr(server, "zd_get", _fake_zd(cmts))
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "compare_draft_replies", "arguments": {"ticket_id": 1}}})
    text = resp["result"]["content"][0]["text"]
    assert "test@example.com" not in text and "[EMAIL]" in text


def test_agent_requester_private_note_not_labeled_customer(monkeypatch):
    # 依頼者がエージェント自身 (社内チケット) の場合、本人の社内メモは「顧客」ではない
    t = {**_ticket(), "requester_id": AGENT}
    monkeypatch.setattr(server, "zd_get", _fake_zd([_c(1, AGENT, False, "メモ"), _c(2, AGENT, True, "返信")], {1: t}))
    out = server.tool_get_ticket({"ticket_id": 1})
    assert "[エージェント / 社内メモ /" in out and "[顧客 / 公開返信 /" in out


def test_agent_requester_public_reply_is_paired(monkeypatch):
    t = {**_ticket(), "requester_id": AGENT}
    cmts = [_c(1, CLAUDE, False, "【返信下書き】\n案"), _c(2, AGENT, True, "案")]
    monkeypatch.setattr(server, "zd_get", _fake_zd(cmts, {1: t}))
    assert "比較できた組: 1 件" in server.tool_compare_draft_replies({"ticket_id": 1})


def test_requester_excluded_when_roles_unavailable(monkeypatch):
    base = _fake_zd([_c(1, CLAUDE, False, "【返信下書き】\n案"), _c(2, REQ, True, "まだ？")])
    def get(path, params=None):
        if path == "/users/show_many.json":
            raise RuntimeError("boom")
        return base(path, params)
    monkeypatch.setattr(server, "zd_get", get)
    assert "未返信: 1 件" in server.tool_compare_draft_replies({"ticket_id": 1})
