"""氏名・住所の文単位 redaction のテスト。

What: server.py の氏名・住所 redaction が仕様どおりに振る舞うことを検証する。
      - 文単位で危険なセグメントだけを落とす
      - プレースホルダで検出種別（[NAME] / [ADDRESS]）が識別できる
      - 一般的な業務文（日付・バージョン番号・「お客様」等）を過剰マスクしない
      - NER 利用不可時は自由記述を全落としし、構造化フィールドは保持する（fail-closed 縮退）
      - 判定・置換が MASK_PII を参照しない純粋関数である
"""

import importlib.util
import sys
from unittest import mock

import pytest

import server


# NER が利用可能だが何も検出しなかった状況を模す（規則ベース層のみを働かせる）。
def _no_ner_hit(_segment: str) -> frozenset:
    return frozenset()


def _call_tool(name, handler, args=None):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": args or {}},
    }
    with mock.patch.dict(server.TOOL_HANDLERS, {name: handler}):
        return server.handle(body)


# ── split_segments: 分割の可逆性と改行の非跨ぎ ─────────────

@pytest.mark.parametrize("text", [
    "",
    "一文だけ。",
    "お世話になっております。田中です。至急確認をお願いします。",
    "一行目\n二行目\n",
    "空行あり\n\n次の段落。\n",
    "改行なし末尾",
    "windows 改行\r\n二行目\r\n",
])
def test_split_segments_is_lossless(text):
    assert "".join(server.split_segments(text)) == text


def test_split_segments_does_not_cross_newlines():
    # 行末の数字と次行先頭が結合して誤判定されないことが分割の前提条件。
    segments = server.split_segments("納期は07-23\n0120-123-456 へ連絡\n")
    assert all("\n" not in s or s.strip() == "" for s in segments)
    assert "07-23\n0120" not in "".join(s for s in segments if s.strip())


# ── 受け入れ基準: 文単位で危険な文だけを落とす ──────────────

def test_redacts_only_the_sentence_containing_the_name():
    text = "お世話になっております。田中です。至急確認をお願いします。"
    out = server.redact_free_text(text, _no_ner_hit)
    assert out == "お世話になっております。[NAME]至急確認をお願いします。"


def test_redacts_sentence_containing_full_address():
    text = "配送先は東京都渋谷区神南1-2-3です。"
    assert server.redact_free_text(text, _no_ner_hit) == "[ADDRESS]"


def test_keeps_sentence_with_no_pii():
    text = "先日購入した製品が届きません。"
    assert server.redact_free_text(text, _no_ner_hit) == text


def test_issue_body_survives_while_pii_sentences_are_dropped():
    text = (
        "お世話になっております。田中です。\n"
        "先日購入した製品が届きません。\n"
        "配送先は東京都渋谷区神南1-2-3です。\n"
        "至急確認をお願いします。\n"
    )
    out = server.redact_free_text(text, _no_ner_hit)
    assert "製品が届きません" in out
    assert "至急確認をお願いします" in out
    assert "田中" not in out
    assert "東京都" not in out
    assert "渋谷区" not in out


# ── 氏名の検出 ───────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "田中です。",
    "担当の佐藤と申します。",
    "山田様よりお問い合わせいただきました。",
    "鈴木さんに転送しました。",
    "長谷川氏が対応します。",
    "五十嵐と申します。",
    "林です。",
    "森様へ返送してください。",
    "佐々木太郎と申します。",
])
def test_person_name_sentences_are_risky(text):
    assert server.segment_has_person_name(text), text
    assert server.redact_free_text(text, _no_ner_hit) == "[NAME]"


@pytest.mark.parametrize("text", [
    "お客様からのご連絡です。",
    "ご担当者様へ転送しました。",
    "皆様よろしくお願いします。",
    "関係部署に確認します。",
    "森林の面積を調べました。",
    "市場調査の結果を共有します。",
    "配送業者に問い合わせました。",
])
def test_common_phrases_are_not_treated_as_names(text):
    assert not server.segment_has_person_name(text), text
    assert server.redact_free_text(text, _no_ner_hit) == text


# ── 住所の検出 ───────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "〒150-0001 へ送付しました。",
    "〒1500001 が記載されています。",
    "東京都に住んでいます。",
    "大阪府までの送料を教えてください。",
    "渋谷区神南一丁目2番3号です。",
    "神南3番地に配送してください。",
    "大阪市中央区1-2-3が届け先です。",
])
def test_address_sentences_are_risky(text):
    assert server.segment_has_address(text), text
    assert server.redact_free_text(text, _no_ner_hit) == "[ADDRESS]"


@pytest.mark.parametrize("text", [
    "バージョン 1-2-3 をリリースしました。",
    "納期は2026-07-28です。",
    "市場調査を2026-01-02に実施しました。",
    "受注番号 12-34 で登録しました。",
    "在庫が3個あります。",
])
def test_dates_and_versions_are_not_treated_as_addresses(text):
    assert not server.segment_has_address(text), text
    assert server.redact_free_text(text, _no_ner_hit) == text


# ── fail-closed 縮退: NER 利用不可 ─────────────────────────

def test_all_free_text_is_redacted_when_ner_unavailable():
    text = "先日購入した製品が届きません。至急確認をお願いします。"
    assert server.redact_free_text(text, None) == "[REDACTED]"


def test_degraded_mode_preserves_line_structure():
    text = "一行目です。\n二行目です。\n"
    assert server.redact_free_text(text, None) == "[REDACTED]\n[REDACTED]\n"


def test_consecutive_identical_placeholders_are_collapsed():
    text = "田中です。佐藤です。鈴木です。"
    assert server.redact_free_text(text, _no_ner_hit) == "[NAME]"


# ── 検出種別の識別 ───────────────────────────────────────

def test_different_placeholders_are_not_collapsed():
    # 種別が異なれば畳まず、どちらが落ちたか判別できる。
    text = "田中です。東京都に住んでいます。"
    assert server.redact_free_text(text, _no_ner_hit) == "[NAME][ADDRESS]"


def test_sentence_with_both_kinds_reports_both():
    text = "田中の住所は東京都渋谷区神南1-2-3です。"
    assert server.redact_free_text(text, _no_ner_hit) == "[ADDRESS][NAME]"


@pytest.mark.parametrize("kinds, expected", [
    (frozenset(), "[REDACTED]"),
    (frozenset({"NAME"}), "[NAME]"),
    (frozenset({"ADDRESS"}), "[ADDRESS]"),
    (frozenset({"NAME", "ADDRESS"}), "[ADDRESS][NAME]"),
])
def test_placeholder_for_kinds(kinds, expected):
    assert server.placeholder_for_kinds(kinds) == expected


def test_segment_risk_kinds_reports_kind():
    assert server.segment_risk_kinds("田中です。") == frozenset({"NAME"})
    assert server.segment_risk_kinds("東京都です。") == frozenset({"ADDRESS"})
    assert server.segment_risk_kinds("製品が届きません。") == frozenset()


def test_ner_detected_kinds_are_reflected_in_placeholder():
    # 規則ベースでは拾えない珍しい姓を NER が Person として検出した場合。
    def ner(segment):
        return frozenset({"NAME"}) if "翠川" in segment else frozenset()

    assert server.redact_free_text("翠川と呼ばれています。", ner) == "[NAME]"


def test_ner_location_maps_to_address_placeholder():
    def ner(segment):
        return frozenset({"ADDRESS"}) if "難読町" in segment else frozenset()

    assert server.redact_free_text("難読町に配送してください。", ner) == "[ADDRESS]"


# ── 純粋性 ───────────────────────────────────────────────

def test_redact_free_text_ignores_mask_pii_flag():
    # redact_free_text は MASK_PII を参照しない純粋関数（ON/OFF は呼び出し側の責務）。
    text = "田中です。"
    with mock.patch.object(server, "MASK_PII", False):
        assert server.redact_free_text(text, _no_ner_hit) == "[NAME]"


def test_redact_free_text_is_idempotent():
    text = "お世話になっております。田中です。至急確認をお願いします。"
    once = server.redact_free_text(text, _no_ner_hit)
    assert server.redact_free_text(once, _no_ner_hit) == once


def test_redact_free_text_is_deterministic():
    text = "田中です。製品が届きません。"
    assert server.redact_free_text(text, _no_ner_hit) == server.redact_free_text(text, _no_ner_hit)


def test_mask_free_text_passthrough_when_off():
    with mock.patch.object(server, "MASK_PII", False):
        assert server.mask_free_text("田中です。") == "田中です。"


# ── ツール結合: get_ticket ────────────────────────────────

_FAKE_TICKET = {
    "id": 42,
    "subject": "配送状況の確認",
    "status": "open",
    "priority": "normal",
    "created_at": "2026-07-01T00:00:00Z",
    "updated_at": "2026-07-02T00:00:00Z",
    "requester_id": 111,
    "assignee_id": 222,
    "tags": ["delivery", "urgent"],
}

_FAKE_COMMENTS = [
    {
        "author_id": 111,
        "created_at": "2026-07-01T00:00:00Z",
        "body": "お世話になっております。田中です。\n製品が届きません。\n配送先は東京都渋谷区神南1-2-3です。",
    },
]


def _fake_zd_get(path, params=None):
    if path.endswith("/comments.json"):
        return {"comments": _FAKE_COMMENTS}
    return {"ticket": _FAKE_TICKET}


def test_get_ticket_redacts_pii_sentences_but_keeps_structured_fields():
    with mock.patch.object(server, "MASK_PII", True), \
         mock.patch.object(server, "ner_available", return_value=True), \
         mock.patch.object(server, "_ner_segment_kinds", _no_ner_hit), \
         mock.patch.object(server, "zd_get", _fake_zd_get):
        out = server.tool_get_ticket({"ticket_id": 42})
    # 落とした理由が種別で識別できる
    assert "[NAME]" in out
    assert "[ADDRESS]" in out
    assert "田中" not in out
    assert "東京都" not in out
    # 構造化フィールドと本題は残る
    assert "製品が届きません" in out
    assert "open" in out
    assert "delivery" in out
    assert "2026-07-01" in out
    assert "111" in out


def test_get_ticket_redacts_all_free_text_when_ner_unavailable():
    with mock.patch.object(server, "MASK_PII", True), \
         mock.patch.object(server, "ner_available", return_value=False), \
         mock.patch.object(server, "zd_get", _fake_zd_get):
        out = server.tool_get_ticket({"ticket_id": 42})
    # 自由記述（コメント本文・件名）はすべて落ちる
    assert "製品が届きません" not in out
    assert "配送状況の確認" not in out
    assert "[REDACTED]" in out
    # 構造化フィールドは引き続き返る
    assert "open" in out
    assert "delivery" in out
    assert "2026-07-01" in out
    assert "111" in out


def test_suggest_reply_redacts_pii_sentences():
    with mock.patch.object(server, "MASK_PII", True), \
         mock.patch.object(server, "ner_available", return_value=True), \
         mock.patch.object(server, "_ner_segment_kinds", _no_ner_hit), \
         mock.patch.object(server, "zd_get", _fake_zd_get):
        out = server.tool_suggest_reply({"ticket_id": 42})
    assert "田中" not in out
    assert "東京都" not in out
    assert "[NAME]" in out
    assert "[ADDRESS]" in out
    assert "製品が届きません" in out


# ── ツール結合: get_user のカスタムフィールド ────────────────

_FAKE_USER_WITH_ADDRESS = {
    "user": {
        "id": 12345,
        "name": "山田太郎",
        "email": "taro@example.com",
        "role": "end-user",
        "organization_id": 999,
        "tags": [],
        "created_at": "2020-01-01T00:00:00Z",
        "last_login_at": "2026-07-01T00:00:00Z",
        "active": True,
        "user_fields": {
            "address": "東京都渋谷区神南1-2-3",
            "postal_code": "150-0001",
            "os_version": "iOS 17",
        },
    }
}


def test_get_user_masks_address_fields_by_key():
    with mock.patch.object(server, "MASK_PII", True), \
         mock.patch.object(server, "zd_get", return_value=_FAKE_USER_WITH_ADDRESS):
        out = server.tool_get_user({"requester_id": 12345})
    assert "[ADDRESS]" in out
    assert "東京都渋谷区神南1-2-3" not in out
    assert "150-0001" not in out
    # 住所以外のカスタムフィールドは運用上必要なので残す
    assert "iOS 17" in out


@pytest.mark.parametrize("key", [
    "address", "home_address", "ADDRESS", "postal_code", "zip", "city",
    "住所", "郵便番号", "所在地",
])
def test_address_like_keys_are_detected(key):
    with mock.patch.object(server, "MASK_PII", True):
        assert server.mask_user_field(key, "なにか") == "[ADDRESS]"


def test_user_field_with_name_is_redacted_even_without_ner():
    with mock.patch.object(server, "MASK_PII", True), \
         mock.patch.object(server, "ner_available", return_value=False):
        assert server.mask_user_field("note", "田中様より連絡あり") == "[NAME]"


# ── KB ツールは縮退時も影響を受けない ───────────────────────

def test_kb_tool_unaffected_when_ner_unavailable():
    raw = "サポート窓口は東京都渋谷区にあります。担当は田中です。"
    with mock.patch.object(server, "MASK_PII", True), \
         mock.patch.object(server, "ner_available", return_value=False):
        resp = _call_tool("get_kb_article", lambda args: raw)
    assert resp["result"]["content"][0]["text"] == raw


def test_kb_tools_remain_exempt():
    for name in ("get_kb_article", "search_kb_articles", "list_kb_articles",
                 "list_kb_categories", "list_kb_sections"):
        assert name in server._MASK_EXEMPT_TOOLS


# ── OFF スイッチの警告 ────────────────────────────────────

def test_off_switch_prepends_warning_notice():
    with mock.patch.object(server, "MASK_PII", False):
        resp = _call_tool("get_ticket", lambda args: "本文")
    text = resp["result"]["content"][0]["text"]
    assert text.startswith(server._MASK_OFF_NOTICE)
    assert "ZENDESK_MASK_PII" in text
    assert text.endswith("本文")


def test_off_switch_notice_not_added_when_on():
    with mock.patch.object(server, "MASK_PII", True):
        resp = _call_tool("get_ticket", lambda args: "本文")
    assert resp["result"]["content"][0]["text"] == "本文"


# ── NER 層の可用性判定 ───────────────────────────────────

# ── モデル選定とラベル対応の固定 ────────────────────────────

# ja_core_news_{lg,md,sm} が実際に出力する 22 ラベル。
_JA_CORE_NEWS_LABELS = {
    "CARDINAL", "DATE", "EVENT", "FAC", "GPE", "LANGUAGE", "LAW", "LOC",
    "MONEY", "MOVEMENT", "NORP", "ORDINAL", "ORG", "PERCENT", "PERSON",
    "PET_NAME", "PHONE", "PRODUCT", "QUANTITY", "TIME", "TITLE_AFFIX",
    "WORK_OF_ART",
}


def test_ginza_is_excluded_for_license_reasons():
    # ja-ginza は学習元に CC BY-NC-SA 4.0（NonCommercial）を含むため採用しない。
    # 誤って復活させないようテストで固定する。
    assert not any("ginza" in m for m in server._NER_MODELS)


def test_supported_models_are_ja_core_news_in_accuracy_order():
    assert server._NER_MODELS == (
        "ja_core_news_lg", "ja_core_news_md", "ja_core_news_sm",
    )


def test_label_sets_are_subsets_of_actual_model_vocabulary():
    # 実在しないラベル名を見ていると取りこぼしに気付けない（ja_ginza の
    # GPE_Other を GPE と書いていた不具合の再発防止）。
    known = server._NER_PERSON_LABELS | server._NER_LOCATION_LABELS
    assert known <= _JA_CORE_NEWS_LABELS, known - _JA_CORE_NEWS_LABELS


def test_person_and_location_labels_are_covered():
    assert "PERSON" in server._NER_PERSON_LABELS
    for label in ("GPE", "LOC", "FAC"):
        assert label in server._NER_LOCATION_LABELS, label


def test_out_of_scope_labels_are_not_masked():
    # 法人名・国籍はスコープ外（spec の Non-Goal）。
    for label in ("ORG", "NORP", "PRODUCT", "DATE", "MONEY"):
        assert label not in server._NER_PERSON_LABELS
        assert label not in server._NER_LOCATION_LABELS


@pytest.mark.parametrize("label, expected", [
    ("PERSON", {"NAME"}),
    ("GPE", {"ADDRESS"}),
    ("LOC", {"ADDRESS"}),
    ("FAC", {"ADDRESS"}),
    ("ORG", set()),
    ("DATE", set()),
])
def test_ner_label_maps_to_expected_kind(label, expected):
    class _Ent:
        def __init__(self, lbl):
            self.label_ = lbl

    class _Doc:
        ents = (_Ent(label),)

    with mock.patch.object(server, "_load_ner", return_value=lambda _t: _Doc()):
        assert server._ner_segment_kinds("なにか") == frozenset(expected)


def test_ner_unavailable_falls_back_to_degraded_mode():
    # spacy が import できない環境を模す（sys.modules に None を置くと ImportError）。
    # 実環境に spacy が入っているかに依存せず縮退動作を検証する。
    with mock.patch.dict(sys.modules, {"spacy": None}), \
         mock.patch.dict(server._NER_STATE,
                         {"nlp": None, "loaded": False, "reason": ""}):
        assert server.ner_available() is False
        assert "spacy" in server._NER_STATE["reason"]


# ── 同梱済み環境での実 NER 結合テスト ──────────────────────

_HAS_SPACY = importlib.util.find_spec("spacy") is not None


@pytest.mark.skipif(not _HAS_SPACY, reason="spacy 未導入（uv sync / pip install 前）")
def test_real_ner_detects_name_and_address_missed_by_rules():
    assert server.ner_available() is True
    # 姓辞書に無い姓・番地を伴わない地名は規則層では拾えず、NER が担当する。
    assert server.segment_risk_kinds("翠川と呼ばれています。") == frozenset()
    assert server._ner_segment_kinds("翠川と呼ばれています。") == frozenset({"NAME"})
    assert server.segment_risk_kinds("日野市栄町に配送してください。") == frozenset()
    assert server._ner_segment_kinds("日野市栄町に配送してください。") == frozenset({"ADDRESS"})


@pytest.mark.skipif(not _HAS_SPACY, reason="spacy 未導入（uv sync / pip install 前）")
def test_real_ner_does_not_over_mask_ordinary_business_text():
    for text in ("製品が届きません。",
                 "バージョン 1-2-3 をリリースしました。",
                 "在庫が3個あります。"):
        assert server.redact_free_text(text, server._ner_segment_kinds) == text, text
