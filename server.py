#!/usr/bin/env python3
"""
Zendesk MCP Server（ローカル実行版）
通信方式: stdio (Claude Desktop / Claude Code 向け)

使い方:
  python server.py

環境変数:
  ZENDESK_SUBDOMAIN
  ZENDESK_EMAIL
  ZENDESK_API_TOKEN  Zendesk 管理画面で発行したAPIトークン
"""

import json
import os
import re
import sys
import ssl
import base64
import urllib.request
import urllib.parse
from datetime import datetime, timezone

# SSL コンテキスト
# SSLCertVerificationError 対策の暫定対応のため、セキュリティ注意
_SSL_CTX = ssl._create_unverified_context()

# ── 環境変数 ──────────────────────────────────────────────
ZENDESK_SUBDOMAIN = os.environ.get("ZENDESK_SUBDOMAIN", "")
ZENDESK_EMAIL     = os.environ.get("ZENDESK_EMAIL", "")
ZENDESK_API_TOKEN = os.environ.get("ZENDESK_API_TOKEN", "")
ZENDESK_BASE      = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2"


# ── PII マスキング ────────────────────────────────────────
# Zendesk から得た応答に含まれる個人情報(メール・電話番号)を固定プレースホルダに
# 置換してクライアントに返す。CLAUDE.md の Functional Core 方針に従い、マスク処理は
# 副作用のない純粋関数として実装し、ON/OFF の判定は呼び出し側(チョークポイント)で行う。

def _parse_mask_pii_env(value: str | None) -> bool:
    # 未設定(None) や不明値は安全側に倒して ON とみなす。明示的な OFF のみ False。
    if value is None:
        return True
    return value.strip().lower() not in ("0", "false")


# デフォルト ON。ZENDESK_MASK_PII=0 / false でマスクを無効化できる。
MASK_PII = _parse_mask_pii_env(os.environ.get("ZENDESK_MASK_PII"))

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-']+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# 電話番号は形式別パターンで最低桁数を保証する。セパレータには \s を使わない
# (\s は \n にマッチし、改行を跨いだ誤マッチ — 例: 行末の日付 07-23 と次行先頭の 0120 —
#  を起こすため)。リテラルスペースのみ許可する。
_INTL_PHONE_RE = re.compile(r"\+81[\- ]?\d{1,4}[\- ]?\d{1,4}[\- ]?\d{4}(?!\d)")
_NATIONAL_PHONE_RES = [
    re.compile(r"(?<![#\d])0[5-9]0[\- ]?\d{4}[\- ]?\d{4}(?!\d)"),               # 携帯 11桁
    re.compile(r"(?<![#\d])0120[\- ]?\d{3}[\- ]?\d{3}(?!\d)"),                  # フリーダイヤル 0120 10桁
    re.compile(r"(?<![#\d])0800[\- ]?\d{3}[\- ]?\d{4}(?!\d)"),                  # フリーダイヤル 0800 11桁
    re.compile(r"(?<![#\d])\(0[1-9]\d{0,3}\)[\- ]?\d{2,4}[\- ]?\d{4}(?!\d)"),   # 括弧付き市外局番
    re.compile(r"(?<![#\d])0[1-9]\d{2}[\- ]?\d{2}[\- ]?\d{4}(?!\d)"),           # 固定 4桁市外局番 10桁
    re.compile(r"(?<![#\d])0[1-9]\d[\- ]?\d{3}[\- ]?\d{4}(?!\d)"),              # 固定 3桁市外局番 10桁
    re.compile(r"(?<![#\d])0[1-9][\- ]?\d{4}[\- ]?\d{4}(?!\d)"),                # 固定 2桁市外局番 10桁
]


# 既知の制限(意図的な検出漏れ): 全角数字の電話番号(０９０…)、0081 形式の国際表記、
# IDN(非ASCII)メールは検出しない。全文の全角→半角正規化は PII でない全角数字まで
# 書き換えてしまい、設計原則「Precision over Recall(誤検出の悪影響 > 検出漏れ)」に反するため。
def _mask_intl_phone(m: "re.Match") -> str:
    # +81 以降の桁数が 9-10 桁のときだけ電話番号とみなす(短すぎる +81XXXX は誤検出)
    digits = sum(c.isdigit() for c in m.group()[3:])
    return "[PHONE]" if 9 <= digits <= 10 else m.group()


def mask_pii_text(text: str) -> str:
    # この関数は MASK_PII を参照せず、常にマスクを実行する純粋関数。
    # メールを先に置換することで、メール内の数字列が電話番号として誤検出されるのを防ぐ。
    text = _EMAIL_RE.sub("[EMAIL]", text)
    text = _INTL_PHONE_RE.sub(_mask_intl_phone, text)
    for pat in _NATIONAL_PHONE_RES:
        text = pat.sub("[PHONE]", text)
    return text


# ── 氏名・住所の redaction ────────────────────────────────
# メール・電話は span 置換で足りるが、氏名・住所は脅威モデルが異なる。
# 「PII が外部 LLM へ渡ること自体が規程違反(一件の漏れ = 違反)」であるため、
# 危険と判定した文を丸ごとプレースホルダに置き換える fail-closed redaction を採る。
#
# Why not span 置換: 日本語 NER の人名再現率は実データで 90%台前半に留まり、
# 「一件も漏らさない」要件を span 単位で満たすことは原理的にできない。文単位に
# 落とせば判定は「危険か否か」の真偽1ビットで足り、境界を外しても漏れない。
#
# Why not Precision over Recall: メール・電話は原則 #5「Precision over Recall」に
# 従うが、氏名・住所についてはこの原則を意図的に反転させている(過剰マスクによる
# 可読性低下を受容する)。両者で基準が異なるのは意図的。

# 落としたセグメントには「なぜ落としたか(検出種別)」を示すプレースホルダを置く。
# 文全体を落とす動作は変わらず、[NAME] は「人名を含む文を落とした」という意味。
# 種別が判定できない縮退時のみ [REDACTED] を使い、両者を区別できるようにする。
_KIND_NAME = "NAME"
_KIND_ADDRESS = "ADDRESS"
_REDACTED = "[REDACTED]"
_ADDRESS_PLACEHOLDER = f"[{_KIND_ADDRESS}]"
_NAME_PLACEHOLDER = f"[{_KIND_NAME}]"

# OFF スイッチは残す判断だが、無効化されていることを LLM 側でも認識できるよう
# 応答先頭に注意文を差し込む。
_MASK_OFF_NOTICE = (
    "⚠️ 警告: このサーバは PII マスクが無効(ZENDESK_MASK_PII=0)の状態で動作しています。"
    "以下の内容には氏名・住所・メール・電話番号が含まれる可能性があります。\n\n"
)

# 文の区切り。行単位に分けたうえで行内を句点で割ることで改行を跨がせない
# (既存の電話 regex で \s を避けているのと同じ理由: 行を跨いだ誤結合を防ぐ)。
_SENTENCE_END_RE = re.compile(r"(?<=[。！？!?])")

_PREFECTURE_RE = re.compile(
    "北海道|青森県|岩手県|宮城県|秋田県|山形県|福島県|茨城県|栃木県|群馬県|埼玉県|"
    "千葉県|東京都|神奈川県|新潟県|富山県|石川県|福井県|山梨県|長野県|岐阜県|静岡県|"
    "愛知県|三重県|滋賀県|京都府|大阪府|兵庫県|奈良県|和歌山県|鳥取県|島根県|岡山県|"
    "広島県|山口県|徳島県|香川県|愛媛県|高知県|福岡県|佐賀県|長崎県|熊本県|大分県|"
    "宮崎県|鹿児島県|沖縄県"
)

# 丁目・番地・郵便番号は単独でも住所と断定できる強いシグナル。
_ADDRESS_STRONG_RES = [
    re.compile(r"〒\s*[0-9０-９]{3}[\-‐−ー－]?[0-9０-９]{4}"),
    re.compile(r"[0-9０-９一二三四五六七八九十]+\s*丁目"),
    re.compile(r"[0-9０-９]+\s*番地"),
    re.compile(r"[0-9０-９]+\s*番\s*[0-9０-９]+\s*号"),
]

# 市区町村名。直前が漢字/カタカナであることを要求して「〜の市場」のような
# 助詞 + 一般名詞の誤検出を落とす(地名は漢字・カタカナで始まる)。
_CITY_RE = re.compile(r"[一-龥ヶァ-ヴー]{1,8}?[市区町村][一-龥ヶァ-ヴー0-9０-９]")

# 「1-2-3」「1の2」等の番地形式。
_HOUSE_NUMBER_RE = re.compile(r"[0-9０-９]{1,4}\s*[\-‐−ー－の]\s*[0-9０-９]{1,4}")

# Why not 「1-2-3」単独で住所とみなさない: 日付(2026-07-28)・バージョン(1-2-3)・
# 型番と衝突して大半の文が落ちる。かつ地名を伴わない番地は個人を特定しないため、
# recall 優先の要件下でも実質的な検出漏れにならない。市区町村との共起を要求する。

# 2文字以上の姓は一般名詞との衝突が少ないため単独ヒットで氏名とみなす。
_SURNAMES_MULTI = (
    "佐藤|鈴木|高橋|田中|伊藤|渡辺|渡部|山本|中村|小林|加藤|吉田|山田|佐々木|山口|"
    "松本|井上|木村|斉藤|斎藤|清水|山崎|阿部|池田|橋本|山下|石川|中島|前田|藤田|"
    "後藤|小川|岡田|村上|長谷川|近藤|石井|坂本|遠藤|藤井|青木|福田|三浦|西村|藤原|"
    "太田|松田|原田|岡本|中川|中野|小野|田村|竹内|金子|和田|中山|石田|上田|森田|"
    "柴田|宮崎|酒井|工藤|横山|宮本|内田|高木|安藤|島田|谷口|大野|高田|丸山|今井|"
    "河野|藤本|村田|武田|上野|杉山|増田|小島|大塚|平野|菅原|久保|松井|千葉|岩崎|"
    "桜井|木下|野口|松尾|菊地|菊池|野村|新井|佐野|市川|水野|大西|吉川|中田|白石|"
    "五十嵐|北村|安田|平田|小山|川口|川崎|飯田|星野|大久保|松岡|山内|吉村|熊谷|"
    "秋山|若林|服部|川上|浅野|西川|大谷|松下|小松|田口|岡崎|成田|早川|荒木|本田|"
    "青山|中島|西田|吉岡|沢田|小泉|片山|水谷|富田|大島|石原|高山|栗原|今村|望月|"
    "土屋|野田|岩田|寺田|馬場|浜田|吉本|尾崎|松村|久保田|杉本|吉野|関口|黒田|平井"
)
_SURNAME_MULTI_RE = re.compile(_SURNAMES_MULTI)

# 1文字姓は一般名詞と大量に衝突する(森林・関係・東京…)ため、氏名文脈との
# 共起を必須にする。
_SURNAME_SINGLE_CONTEXT_RE = re.compile(
    r"(?:林|森|関|原|辻|東|南|北|宮|岡|堀|沖|巽|柳|樋|畑|栗|桑|藤)"
    r"(?:です|でございます|より|宛|行\b|様|さん|氏|殿)"
)

# 敬称つきの呼びかけ。氏名でない定型語は除外する。
_HONORIFIC_RE = re.compile(r"([一-龥ヶァ-ヴー々]{1,10})(様|さま|さん|氏|殿|くん|君|ちゃん)")
_HONORIFIC_EXCEPTIONS = {
    "お客", "客", "皆", "皆々", "奥", "神", "仏", "王", "殿", "貴", "何", "どちら",
    "担当者", "ご担当", "ご担当者", "御担当者", "責任者", "各", "御", "ご",
    "母", "父", "兄", "姉", "弟", "妹", "息子", "娘", "主人", "旦那", "嫁",
    "医者", "業者", "他", "皆さん", "運転手", "配達",
}

# 自己紹介の定型句。直前に必ず氏名が来るため、句そのものを危険シグナルとする。
_SELF_INTRO_RE = re.compile(r"と申します|と申しま|と言います|といいます|と名乗")


def split_segments(text: str) -> list[str]:
    """text を文単位に分割する。"".join(結果) == text を満たす純粋関数。

    改行は独立したセグメントとして扱い、redaction で改行が失われないようにする。
    """
    segments: list[str] = []
    for line in text.splitlines(keepends=True):
        body, newline = line, ""
        while body and body[-1] in "\r\n":
            newline = body[-1] + newline
            body = body[:-1]
        if body:
            segments.extend(p for p in _SENTENCE_END_RE.split(body) if p)
        if newline:
            segments.append(newline)
    return segments


def segment_has_address(segment: str) -> bool:
    """セグメントに住所が含まれるかを判定する純粋関数。"""
    if any(pat.search(segment) for pat in _ADDRESS_STRONG_RES):
        return True
    if _PREFECTURE_RE.search(segment):
        return True
    return bool(_CITY_RE.search(segment) and _HOUSE_NUMBER_RE.search(segment))


def segment_has_person_name(segment: str) -> bool:
    """セグメントに人名が含まれるかを判定する純粋関数。"""
    if _SELF_INTRO_RE.search(segment):
        return True
    if _SURNAME_MULTI_RE.search(segment):
        return True
    if _SURNAME_SINGLE_CONTEXT_RE.search(segment):
        return True
    for prefix, _honorific in _HONORIFIC_RE.findall(segment):
        if prefix not in _HONORIFIC_EXCEPTIONS:
            return True
    return False


def segment_risk_kinds(segment: str) -> frozenset:
    """辞書・正規表現で検出した PII 種別を返す純粋関数。空集合なら安全。

    NER の副層として使う。真偽ではなく種別を返すことで、落としたセグメントの
    プレースホルダに検出理由(人名 / 住所)を反映できる。
    """
    kinds = set()
    if segment_has_person_name(segment):
        kinds.add(_KIND_NAME)
    if segment_has_address(segment):
        kinds.add(_KIND_ADDRESS)
    return frozenset(kinds)


def segment_is_risky_by_rules(segment: str) -> bool:
    """規則ベースで危険と判定されるかを返す純粋関数。"""
    return bool(segment_risk_kinds(segment))


def placeholder_for_kinds(kinds) -> str:
    """検出種別からプレースホルダ文字列を組み立てる純粋関数。

    種別不明(縮退時)は [REDACTED]、人名と住所の両方を含む場合は
    [ADDRESS][NAME] のように連結する。
    """
    if not kinds:
        return _REDACTED
    return "".join(f"[{kind}]" for kind in sorted(kinds))


def redact_free_text(text: str, ner_kinds=None) -> str:
    """自由記述から氏名・住所を含む文を落とす純粋関数。

    ner_kinds: セグメントを受け取り検出種別の集合を返す callable。
               None(= NER 利用不可)のときは種別を問わず全セグメントを
               [REDACTED] に落とす fail-closed 動作。
    """
    if not text:
        return text
    out: list[str] = []
    for seg in split_segments(text):
        if not seg.strip():
            out.append(seg)
            continue
        if ner_kinds is None:
            placeholder = _REDACTED
        else:
            kinds = segment_risk_kinds(seg) | frozenset(ner_kinds(seg))
            if not kinds:
                out.append(seg)
                continue
            placeholder = placeholder_for_kinds(kinds)
        # 同じプレースホルダが連続する場合は 1 個に畳んで可読性を保つ。
        if not (out and out[-1] == placeholder):
            out.append(placeholder)
    return "".join(out)


# ── NER 層(任意依存 / Imperative Shell) ──────────────────
# spaCy の日本語モデルが利用できる場合のみ第1層として使う。利用できない環境では
# ner_check=None となり、自由記述は全落としされる(fail-closed 縮退)。

# 対応モデルは spaCy 公式の日本語パイプライン ja_core_news_{lg,md,sm} のみ。
# 精度の高い順に探索し、最初に読めたものを使う。推奨は md
# (ents_f 0.7043 / recall 0.6755 / 40MB。lg は 530MB で +0.018 しか伸びない)。
#
# Why not ja_ginza: パッケージは MIT 宣言だが、wheel 内 meta.json の sources に
# 学習元として UD_Japanese-BCCWJ (CC BY-NC-SA 4.0) と GSK2014-A(個別に定める
# 商用ライセンス) が記載されている。NC は再配布だけでなく商用利用自体を制限する
# ため、業務利用および .mcpb への同梱配布に適さない。探索順から意図的に除外する。
# ja_core_news_{md,lg} は ja_ginza と同一の chiVe ベクトル
# (chive-1.1-mc90-500k / 300次元 / 480,443キー)を使うため、未知語(珍しい姓・
# 難読地名)への一般化能力は同等。ライセンスは CC BY-SA 4.0 で NC 制限がない。
_NER_MODELS = ("ja_core_news_lg", "ja_core_news_md", "ja_core_news_sm")

# ラベル集合は上記モデルが実際に出力する 22 ラベルに対して監査済み。
# ja_core_news_* の全ラベル:
#   CARDINAL DATE EVENT FAC GPE LANGUAGE LAW LOC MONEY MOVEMENT NORP ORDINAL
#   ORG PERCENT PERSON PET_NAME PHONE PRODUCT QUANTITY TIME TITLE_AFFIX WORK_OF_ART
#
# Why not ORG / NORP: 本機能のスコープは氏名・住所であり、法人名・国籍/民族は
# 対象外(spec の Non-Goal)。PHONE / DATE 等は既存の span マスクと規則層が扱う。
# Why not ja_ginza のラベル名(GPE_Other, Facility_Other, Country, Region_Other …):
# ja_ginza は 189 ラベルあり全列挙の監査が現実的でない。22 ラベルに限定することで
# 「拾い漏れているラベルがないか」を確実に検証できる(テストで固定している)。
_NER_PERSON_LABELS = {"PERSON"}
_NER_LOCATION_LABELS = {"GPE", "LOC", "FAC"}

_NER_STATE: dict = {"nlp": None, "loaded": False, "reason": ""}


def _load_ner():
    """spaCy と日本語モデルを遅延ロードする（Imperative Shell）。

    依存は実行中のインタプリタから import するだけで、パス操作は行わない。
    .mcpb 配布では Claude Desktop の UV ランタイムがインストール時に
    `uv sync` で venv を作り、`uv run` でその venv から起動する
    （manifest.json の server.type = "uv"）。手動実行の場合は
    requirements-ner.txt を pip install した環境で起動する。
    """
    if _NER_STATE["loaded"]:
        return _NER_STATE["nlp"]
    _NER_STATE["loaded"] = True
    try:
        import spacy
    except ImportError:
        _NER_STATE["reason"] = (
            f"spacy が import できません (python {sys.version.split()[0]})"
        )
        return None
    for model in _NER_MODELS:
        try:
            _NER_STATE["nlp"] = spacy.load(model)
            _NER_STATE["reason"] = f"model={model}"
            return _NER_STATE["nlp"]
        except Exception:
            continue
    _NER_STATE["reason"] = f"モデル未導入 (候補: {', '.join(_NER_MODELS)})"
    return None


def ner_available() -> bool:
    return _load_ner() is not None


def _ner_segment_kinds(segment: str) -> frozenset:
    """NER が検出したエンティティを PII 種別に写す。"""
    nlp = _load_ner()
    if nlp is None:
        # 呼ばれない想定。万一呼ばれても安全側(両種別あり)に倒す。
        return frozenset({_KIND_NAME, _KIND_ADDRESS})
    kinds = set()
    for ent in nlp(segment).ents:
        if ent.label_ in _NER_PERSON_LABELS:
            kinds.add(_KIND_NAME)
        elif ent.label_ in _NER_LOCATION_LABELS:
            kinds.add(_KIND_ADDRESS)
    return frozenset(kinds)


def mask_free_text(text: str) -> str:
    """ツール実装から呼ぶ入口。ON/OFF と NER 可用性の判定を担う。"""
    if not MASK_PII:
        return text
    check = _ner_segment_kinds if ner_available() else None
    return redact_free_text(text, check)


# 住所を含みうるユーザーカスタムフィールドのキー名。
_ADDRESS_FIELD_KEY_RE = re.compile(
    r"addr|address|zip|postal|pref|city|town|street|building|room|"
    r"住所|郵便|都道府県|市区町村|番地|建物|部屋|所在地",
    re.IGNORECASE,
)


def mask_user_field(key: str, value) -> str:
    """ユーザーカスタムフィールドをフィールド単位でマスクする。"""
    if not MASK_PII:
        return str(value)
    if _ADDRESS_FIELD_KEY_RE.search(key):
        return _ADDRESS_PLACEHOLDER
    # Why not fail-closed: user_fields は自由記述本文ではなく構造化フィールドで、
    # 実際にはプラン名・OS バージョン等の運用上必要な短い値が入る。NER 不在時に
    # 全落としすると get_user が実質使えなくなるため、ここは規則ベース判定のみ
    # 適用する(住所系キーは上で [ADDRESS] に置換済み)。
    check = _ner_segment_kinds if ner_available() else (lambda _segment: frozenset())
    return redact_free_text(str(value), check)


# ── Zendesk API ヘルパー ──────────────────────────────────

def _auth() -> str:
    cred = f"{ZENDESK_EMAIL}/token:{ZENDESK_API_TOKEN}"
    return "Basic " + base64.b64encode(cred.encode()).decode()


def zd_get(path: str, params: dict | None = None) -> dict:
    url = f"{ZENDESK_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": _auth()})
    with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as resp:
        return json.loads(resp.read())


def zd_post(path: str, payload: dict) -> dict:
    url  = f"{ZENDESK_BASE}{path}"
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Authorization": _auth(), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as resp:
        return json.loads(resp.read())


def zd_put(path: str, payload: dict) -> dict:
    url  = f"{ZENDESK_BASE}{path}"
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url, data=data, method="PUT",
        headers={"Authorization": _auth(), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as resp:
        return json.loads(resp.read())


def zd_get_all(path: str, result_key: str, params: dict | None = None,
               max_total: int = 200) -> tuple[list, int | None]:
    PAGE_SIZE = 100
    p = dict(params or {})
    p["per_page"] = PAGE_SIZE
    p.setdefault("page", 1)

    items: list = []
    while True:
        data       = zd_get(path, p)
        page_items = data.get(result_key, [])
        items     += page_items

        if len(items) >= max_total:
            items = items[:max_total]
            total     = data.get("count") or data.get("total")
            remaining = (total - max_total) if total and total > max_total else None
            return items, remaining

        next_page = data.get("next_page")
        if not next_page or len(page_items) < PAGE_SIZE:
            break

        p["page"] = p["page"] + 1

    return items, None


# ── ツール定義 ────────────────────────────────────────────

TOOLS = [
    # ── チケット系 ──────────────────────────────────────
    {
        "name": "search_tickets",
        "description": "チケットをキーワード・ステータス・タグ・担当者・日付等で検索する。keywordはタイトル・コメント本文の全文検索",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword":        {"type": "string",  "description": "タイトルまたはコメント本文に含まれる文字列で全文検索（例: 'ログインできない'）"},
                "query":          {"type": "string",  "description": "Zendesk検索修飾子（例: 'status:open tags:vip'）。keywordと併用可"},
                "assignee":       {"type": "string",  "description": "担当者のメールアドレスまたはユーザー名で絞り込む（例: 'john@example.com'）"},
                "created_after":  {"type": "string",  "description": "この日付以降に作成（YYYY-MM-DD）"},
                "created_before": {"type": "string",  "description": "この日付以前に作成（YYYY-MM-DD）"},
                "updated_after":  {"type": "string",  "description": "この日付以降に更新（YYYY-MM-DD）"},
                "updated_before": {"type": "string",  "description": "この日付以前に更新（YYYY-MM-DD）"},
                "per_page":       {"type": "integer", "description": "1ページあたりの取得件数（最大100）", "default": 100},
                "max_total":      {"type": "integer", "description": "取得上限件数（デフォルト200）。1000超を指定するとExport APIで大量取得可能（ソートはcreated_at昇順固定）", "default": 200},
            },
        },
    },
    {
        "name": "get_ticket",
        "description": "チケットIDを指定して詳細情報とコメント一覧を取得する",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer", "description": "ZendeskチケットID"},
            },
            "required": ["ticket_id"],
        },
    },
    {
        "name": "list_tickets",
        "description": "最近のチケット一覧を取得する（傾向分析向け）。タグで絞り込むことも可能",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status":         {"type": "string",  "description": "open / pending / solved / closed", "default": "open"},
                "per_page":       {"type": "integer", "description": "取得件数（最大100）", "default": 30},
                "tags":           {"type": "array", "items": {"type": "string"}, "description": "絞り込むタグのリスト（例: ['bug', 'vip']）。複数指定するとAND条件になる"},
                "created_after":  {"type": "string",  "description": "この日付以降に作成（YYYY-MM-DD）"},
                "created_before": {"type": "string",  "description": "この日付以前に作成（YYYY-MM-DD）"},
                "updated_after":  {"type": "string",  "description": "この日付以降に更新（YYYY-MM-DD）"},
                "updated_before": {"type": "string",  "description": "この日付以前に更新（YYYY-MM-DD）"},
                "max_total":      {"type": "integer", "description": "取得上限件数（デフォルト200）。上限に達した場合は残り件数を通知する", "default": 200},
            },
        },
    },
    {
        "name": "get_ticket_stats",
        "description": "現在のチケット件数をステータス別に集計して返す",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "suggest_reply",
        "description": "チケットの内容を取得し、返信案を生成するためのコンテキストを組み立てる",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer", "description": "返信案を作りたいチケットID"},
            },
            "required": ["ticket_id"],
        },
    },

    # ── ナレッジベース系 ─────────────────────────────────
    {
        "name": "search_kb_articles",
        "description": "ナレッジベース記事をキーワードで全文検索する",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query":    {"type": "string",  "description": "検索キーワード"},
                "per_page": {"type": "integer", "description": "取得件数（最大30）", "default": 10},
                "locale":   {"type": "string",  "description": "言語コード（例: ja, en-us）。省略時は全言語", "default": ""},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_kb_articles",
        "description": "ナレッジベース記事の一覧を取得する（カテゴリ・セクション絞り込み可）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "per_page":   {"type": "integer", "description": "取得件数（最大100）", "default": 20},
                "section_id": {"type": "integer", "description": "セクションIDで絞り込む（省略可）"},
                "locale":     {"type": "string",  "description": "言語コード（例: ja, en-us）。省略時は全言語", "default": ""},
            },
        },
    },
    {
        "name": "get_kb_article",
        "description": "ナレッジベース記事IDを指定して本文全体を取得する",
        "inputSchema": {
            "type": "object",
            "properties": {
                "article_id": {"type": "integer", "description": "KB記事ID"},
                "locale":     {"type": "string",  "description": "言語コード（例: ja, en-us）。省略時はデフォルト言語", "default": ""},
            },
            "required": ["article_id"],
        },
    },
    {
        "name": "list_kb_categories",
        "description": "ナレッジベースのカテゴリ一覧を取得する",
        "inputSchema": {
            "type": "object",
            "properties": {
                "locale": {"type": "string", "description": "言語コード（省略可）", "default": ""},
            },
        },
    },
    {
        "name": "list_kb_sections",
        "description": "ナレッジベースのセクション一覧を取得する（カテゴリIDで絞り込み可）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category_id": {"type": "integer", "description": "カテゴリIDで絞り込む（省略可）"},
                "locale":      {"type": "string",  "description": "言語コード（省略可）", "default": ""},
            },
        },
    },

    # ── CSAT系 ──────────────────────────────────────────
    {
        "name": "get_csat_rating",
        "description": "チケットIDを指定してそのチケットのCSAT評価・コメント・bad理由を取得する",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer", "description": "ZendeskチケットID"},
                "locale":    {"type": "string",  "description": "言語コード（デフォルト: ja）", "default": "ja"},
            },
            "required": ["ticket_id"],
        },
    },

    # ── ユーザー系 ──────────────────────────────────────
    {
        "name": "get_user",
        "description": "ZendeskユーザーIDまたはメールアドレスでユーザー情報・カスタムフィールド（OSやアプリバージョン等）を取得する",
        "inputSchema": {
            "type": "object",
            "properties": {
                "requester_id": {"type": "integer", "description": "ZendeskユーザーID（get_ticketで取得したrequester_idなど）"},
                "email":        {"type": "string",  "description": "メールアドレスで検索する場合に指定"},
            },
        },
    },
    {
        "name": "list_user_fields",
        "description": "Zendeskに定義されているユーザーカスタムフィールドの一覧を取得する（フィールドキー・型・選択肢を確認できる）",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },

    # ── ヘルプ記事書き込み ───────────────────────────────
    {
        "name": "create_kb_article",
        "description": "ヘルプセンターに記事を下書きとして新規作成する。section_idはlist_kb_sectionsで取得できる",
        "inputSchema": {
            "type": "object",
            "properties": {
                "section_id": {"type": "integer", "description": "投稿先のセクションID（list_kb_sectionsで確認）"},
                "title":      {"type": "string",  "description": "記事タイトル"},
                "body":       {"type": "string",  "description": "記事本文（HTML形式）"},
                "locale":     {"type": "string",  "description": "言語コード（デフォルト: ja）", "default": "ja"},
            },
            "required": ["section_id", "title", "body"],
        },
    },
    {
        "name": "update_kb_article",
        "description": "既存のヘルプ記事を更新する。タイトル・本文・下書き状態を変更できる。article_idはget_kb_articleやlist_kb_articlesで取得できる",
        "inputSchema": {
            "type": "object",
            "properties": {
                "article_id": {"type": "integer", "description": "更新対象の記事ID"},
                "title":      {"type": "string",  "description": "新しいタイトル（省略時は変更なし）"},
                "body":       {"type": "string",  "description": "新しい本文（HTML形式）（省略時は変更なし）"},
                "draft":      {"type": "boolean", "description": "Trueで下書きに戻す、Falseで公開（省略時は変更なし）"},
                "locale":     {"type": "string",  "description": "言語コード（デフォルト: ja）", "default": "ja"},
            },
            "required": ["article_id"],
        },
    },
]


# ── ツール実装 ────────────────────────────────────────────

def zd_search_export(query: str, max_total: int = 10000) -> tuple[list, int | None]:
    PAGE_SIZE = 1000
    params = {
        "query":        query,
        "filter[type]": "ticket",
        "page[size]":   PAGE_SIZE,
    }
    items: list = []

    while True:
        url = f"{ZENDESK_BASE}/search/export.json?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": _auth()})
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as resp:
            data = json.loads(resp.read())

        page_items = data.get("results", [])
        items     += page_items

        if len(items) >= max_total:
            items = items[:max_total]
            meta  = data.get("meta", {})
            remaining = -1 if meta.get("has_more") else None
            return items, remaining

        meta = data.get("meta", {})
        if not meta.get("has_more"):
            break

        after = meta.get("after_cursor")
        if not after:
            break
        params["page[after]"] = after

    return items, None


def _remaining_msg(remaining: int | None, name: str = "件") -> str:
    if remaining:
        return f"\n\n⚠️ 上限に達しました。まだ約{remaining}{name}あります。続けて取得しますか？（max_total を増やして再実行してください）"
    return ""


def tool_search_tickets(args: dict) -> str:
    query     = args.get("query", "")
    keyword   = args.get("keyword", "")
    assignee  = args.get("assignee", "")
    max_total = args.get("max_total", 200)

    if keyword:
        query = f'"{keyword}" {query}'.strip()
    if assignee:
        query = f"{query} assignee:{assignee}"
    for key, modifier in [
        ("created_after",  "created>"),
        ("created_before", "created<"),
        ("updated_after",  "updated>"),
        ("updated_before", "updated<"),
    ]:
        if args.get(key):
            query = f"{query} {modifier}{args[key]}"

    full_query = f"type:ticket {query}"
    if max_total > 1000:
        tickets, remaining = zd_search_export(full_query, max_total=max_total)
        lines = [f"検索結果: {len(tickets)} 件（Export API使用 / created_at昇順）\n"]
    else:
        tickets, remaining = zd_get_all(
            "/search.json", "results",
            {"query": full_query, "sort_by": "updated_at", "sort_order": "desc"},
            max_total=max_total,
        )
        lines = [f"検索結果: {len(tickets)} 件\n"]
    for t in tickets:
        assignee_id  = t.get("assignee_id", "")
        requester_id = t.get("requester_id", "")
        subject = mask_free_text(t.get("subject") or "")
        lines.append(f"[#{t['id']}] {subject} | {t['status']} | requester_id: {requester_id} | 担当者ID: {assignee_id} | 作成: {t['created_at'][:10]} | タグ: {','.join(t.get('tags',[]))}")
    return "\n".join(lines) + _remaining_msg(remaining)


def tool_get_ticket(args: dict) -> str:
    tid  = args["ticket_id"]
    t    = zd_get(f"/tickets/{tid}.json")["ticket"]
    cmts = zd_get(f"/tickets/{tid}/comments.json")["comments"]
    lines = [
        f"# チケット #{tid}: {mask_free_text(t.get('subject') or '')}",
        f"ステータス: {t['status']} | 優先度: {t.get('priority','なし')}",
        f"作成: {t['created_at'][:10]} | 更新: {t['updated_at'][:10]}",
        f"requester_id: {t.get('requester_id','なし')} | assignee_id: {t.get('assignee_id','なし')}",
        f"タグ: {', '.join(t.get('tags',[])) or 'なし'}",
        "", "## コメント",
    ]
    for c in cmts:
        author = "顧客" if c["author_id"] == t["requester_id"] else "エージェント"
        body = mask_free_text((c.get("body") or "")[:800])
        lines.append(f"\n[{author} / {c['created_at'][:10]}]\n{body}")
    return "\n".join(lines)


def tool_list_tickets(args: dict) -> str:
    status    = args.get("status", "open")
    tags      = args.get("tags", [])
    max_total = args.get("max_total", 200)

    date_query = ""
    for key, modifier in [
        ("created_after",  "created>"),
        ("created_before", "created<"),
        ("updated_after",  "updated>"),
        ("updated_before", "updated<"),
    ]:
        if args.get(key):
            date_query += f" {modifier}{args[key]}"

    if tags or date_query:
        tag_query = " ".join(f"tags:{t}" for t in tags)
        query     = f"type:ticket status:{status} {tag_query}{date_query}".strip()
        tickets, remaining = zd_get_all(
            "/search.json", "results",
            {"query": query, "sort_by": "updated_at", "sort_order": "desc"},
            max_total=max_total,
        )
        label_parts = []
        if tags:
            label_parts.append(f"タグ: {', '.join(tags)}")
        if date_query:
            label_parts.append(date_query.strip())
        lines = [f"チケット一覧（{status} / {' / '.join(label_parts)}）: {len(tickets)} 件\n"]
    else:
        tickets, remaining = zd_get_all(
            "/tickets.json", "tickets",
            {"status": status, "sort_by": "updated_at", "sort_order": "desc"},
            max_total=max_total,
        )
        lines = [f"チケット一覧（{status}）: {len(tickets)} 件\n"]

    for t in tickets:
        subject = mask_free_text(t.get("subject") or "")
        lines.append(f"[#{t['id']}] {subject} | 更新: {t['updated_at'][:10]} | タグ: {','.join(t.get('tags',[]))}")
    return "\n".join(lines) + _remaining_msg(remaining)


def tool_get_ticket_stats(args: dict) -> str:
    results = {}
    for status in ("open", "pending", "solved", "closed"):
        data = zd_get("/search.json", {"query": f"type:ticket status:{status}", "per_page": 1})
        results[status] = data.get("count", 0)
    total = sum(results.values())
    lines = ["📊 チケット統計（現在）", f"合計: {total} 件", ""]
    for k, v in results.items():
        pct = round(v / total * 100) if total else 0
        lines.append(f"  {k:8s}: {v:4d} 件 ({pct}%)")
    return "\n".join(lines)


def tool_suggest_reply(args: dict) -> str:
    tid  = args["ticket_id"]
    t    = zd_get(f"/tickets/{tid}.json")["ticket"]
    cmts = zd_get(f"/tickets/{tid}/comments.json")["comments"]
    conv_history = []
    for c in cmts[-6:]:
        role = "顧客" if c["author_id"] == t["requester_id"] else "エージェント"
        conv_history.append(f"[{role}] {mask_free_text((c.get('body') or '')[:300])}")
    return (
        f"## 返信案生成コンテキスト\n\n"
        f"**件名**: {mask_free_text(t.get('subject') or '')}\n"
        f"**ステータス**: {t['status']} | **優先度**: {t.get('priority','なし')}\n"
        f"**タグ**: {', '.join(t.get('tags',[]))}\n\n"
        f"### 会話履歴（直近）\n" + "\n\n".join(conv_history) + "\n\n"
        f"---\n"
        f"上記を踏まえて、丁寧かつ簡潔な返信案を日本語で作成してください。\n"
        f"・顧客の問題を正確に理解して解決策を提示する\n"
        f"・専門用語は避け、わかりやすい言葉を使う\n"
        f"・必要に応じてナレッジベース記事への誘導を含める"
    )


# ── KB ツール実装 ─────────────────────────────────────────

def tool_search_kb_articles(args: dict) -> str:
    query    = args["query"]
    per_page = min(args.get("per_page", 10), 30)
    params   = {"query": query, "per_page": per_page}
    if args.get("locale"):
        params["locale"] = args["locale"]
    data     = zd_get("/help_center/articles/search.json", params)
    articles = data.get("results", [])
    lines    = [f"KB検索結果: {len(articles)} 件（クエリ: {query}）\n"]
    for a in articles:
        lines.append(f"[ID:{a['id']}] {a['title']} | セクション:{a.get('section_id','?')} | 更新:{a['updated_at'][:10]}")
    return "\n".join(lines)


def tool_list_kb_articles(args: dict) -> str:
    per_page   = min(args.get("per_page", 20), 100)
    section_id = args.get("section_id")
    locale     = args.get("locale", "")
    params     = {"per_page": per_page, "sort_by": "updated_at", "sort_order": "desc"}
    if locale:
        params["locale"] = locale

    if section_id:
        path = f"/help_center/sections/{section_id}/articles.json"
    else:
        path = "/help_center/articles.json"

    data     = zd_get(path, params)
    articles = data.get("articles", [])
    lines    = [f"KB記事一覧: {len(articles)} 件\n"]
    for a in articles:
        draft = " [下書き]" if a.get("draft") else ""
        lines.append(f"[ID:{a['id']}] {a['title']}{draft} | セクション:{a.get('section_id','?')} | 更新:{a['updated_at'][:10]}")
    return "\n".join(lines)


def tool_get_kb_article(args: dict) -> str:
    article_id = args["article_id"]
    locale     = args.get("locale", "")
    path       = f"/help_center/articles/{article_id}.json"
    if locale:
        path = f"/help_center/locales/{locale}/articles/{article_id}.json"
    data    = zd_get(path)
    article = data.get("article", {})

    body = re.sub(r"<[^>]+>", "", article.get("body", ""))
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    return (
        f"# {article.get('title','（タイトルなし）')}\n\n"
        f"ID: {article.get('id')} | セクション: {article.get('section_id','?')} | "
        f"更新: {article.get('updated_at','?')[:10]}\n\n"
        f"{body}"
    )


def tool_list_kb_categories(args: dict) -> str:
    locale = args.get("locale", "")
    params = {}
    if locale:
        params["locale"] = locale
    data       = zd_get("/help_center/categories.json", params or None)
    categories = data.get("categories", [])
    lines      = [f"カテゴリ一覧: {len(categories)} 件\n"]
    for c in categories:
        lines.append(f"[ID:{c['id']}] {c['name']} | 説明: {(c.get('description') or '')[:60]}")
    return "\n".join(lines)


def tool_list_kb_sections(args: dict) -> str:
    category_id = args.get("category_id")
    locale      = args.get("locale", "")
    params      = {}
    if locale:
        params["locale"] = locale
    path     = f"/help_center/categories/{category_id}/sections.json" if category_id else "/help_center/sections.json"
    data     = zd_get(path, params or None)
    sections = data.get("sections", [])
    lines    = [f"セクション一覧: {len(sections)} 件\n"]
    for s in sections:
        lines.append(f"[ID:{s['id']}] {s['name']} | カテゴリ:{s.get('category_id','?')} | 更新:{s['updated_at'][:10]}")
    return "\n".join(lines)


# ── CSAT ─────────────────────────────────────────────────

def _parse_survey_response(sr: dict, locale: str = "ja") -> dict:
    result = {
        "id":              sr.get("id"),
        "ticket_id":       None,
        "rating":          None,
        "rating_category": None,
        "comment":         None,
        "bad_reason":      None,
        "created_at":      None,
    }
    for subj in sr.get("subjects", []):
        if subj.get("type") == "ticket":
            result["ticket_id"] = subj.get("id")

    for ans in sr.get("answers", []):
        atype = ans.get("type")
        q     = ans.get("question", {})
        alias = q.get("alias", "")

        if atype == "rating_scale" and q.get("sub_type") == "customer_satisfaction":
            result["rating"]          = ans.get("rating")
            result["rating_category"] = ans.get("rating_category")
            result["created_at"]      = ans.get("created_at", "")[:10]

        elif atype == "closed_ended" and alias == "reason":
            for sel in ans.get("selections", []):
                opt_id = sel.get("option_id")
                for opt in q.get("options", []):
                    if opt.get("id") == opt_id:
                        label = opt.get("label", {})
                        result["bad_reason"] = label.get("value") or label.get("key", "")

        elif atype == "open_ended" and alias == "comment":
            result["comment"] = ans.get("value")

    return result


def _zd_get_survey_responses_page(params: dict) -> dict:
    url = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2/guide/survey_responses"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": _auth()})
    with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as resp:
        return json.loads(resp.read())


def _get_survey_response_detail(sr_id: str, locale: str = "ja") -> dict:
    url = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2/guide/{locale}/survey_responses/{sr_id}"
    req = urllib.request.Request(url, headers={"Authorization": _auth()})
    with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as resp:
        return json.loads(resp.read()).get("survey_response", {})


def tool_get_csat_rating(args: dict) -> str:
    tid    = args["ticket_id"]
    locale = args.get("locale", "ja")

    data      = _zd_get_survey_responses_page({
        "filter[subject_zrns]": f"zen:ticket:{tid}",
        "page[size]": 10,
    })
    responses = data.get("survey_responses", [])

    if not responses:
        return f"チケット #{tid} にはCSAT評価がありません。"

    detail = _get_survey_response_detail(responses[0]["id"], locale)
    parsed = _parse_survey_response(detail, locale)

    t     = zd_get(f"/tickets/{tid}.json")["ticket"]
    lines = [
        f"# チケット #{tid} のCSAT評価",
        f"件名: {mask_free_text(t.get('subject') or '')}",
        f"評価: {parsed.get('rating_category','?')}（スコア: {parsed.get('rating','?')}）",
    ]
    if parsed.get("bad_reason"):
        lines.append(f"bad理由: {parsed['bad_reason']}")
    comment = parsed.get("comment")
    lines += [
        f"コメント: {mask_free_text(comment) if comment else '（コメントなし）'}",
        f"評価日時: {parsed.get('created_at','?')}",
    ]
    return "\n".join(lines)


# ── ユーザー系 ────────────────────────────────────────────

def tool_get_user(args: dict) -> str:
    user_id = args.get("requester_id")
    email   = args.get("email", "")

    if user_id:
        data = zd_get(f"/users/{user_id}.json")
        user = data.get("user", {})
    elif email:
        data  = zd_get("/users/search.json", {"query": email})
        users = data.get("users", [])
        if not users:
            return f"メールアドレス '{email}' に一致するユーザーが見つかりませんでした。"
        user = users[0]
    else:
        return "user_id または email を指定してください。"

    custom_fields = user.get("user_fields", {})
    cf_lines = []
    for k, v in custom_fields.items():
        if v not in (None, "", []):
            # カスタムフィールドは自由記述なので、住所系キーは [ADDRESS]、
            # それ以外は自由記述と同じ redaction を通す。
            cf_lines.append(f"  {k}: {mask_user_field(k, v)}")

    # 名前・メールは構造フィールドの PII。regex では人名を検出できないため、
    # ここでフィールド単位でプレースホルダに差し替える(ON/OFF は MASK_PII で制御)。
    name_display  = "[NAME]" if MASK_PII else user.get('name')
    email_display = "[EMAIL]" if MASK_PII else user.get('email')

    lines = [
        "# ユーザー情報",
        f"ID      : {user.get('id')}",
        f"名前    : {name_display}",
        f"メール  : {email_display}",
        f"ロール  : {user.get('role')}",
        f"組織ID  : {user.get('organization_id', 'なし')}",
        f"タグ    : {', '.join(user.get('tags', [])) or 'なし'}",
        f"作成日  : {user.get('created_at', '')[:10]}",
        f"最終ログイン: {user.get('last_login_at', 'なし')[:10] if user.get('last_login_at') else 'なし'}",
        f"アクティブ: {user.get('active')}",
        "",
        "## カスタムフィールド",
    ]
    lines += cf_lines if cf_lines else ["  （カスタムフィールドなし）"]
    return "\n".join(lines)


def tool_list_user_fields(args: dict) -> str:
    data   = zd_get("/user_fields.json")
    fields = data.get("user_fields", [])
    lines  = [f"ユーザーカスタムフィールド一覧: {len(fields)} 件\n"]
    for f in fields:
        field_type = f.get("type", "?")
        key        = f.get("key", "?")
        title      = f.get("title", "?")
        options    = f.get("custom_field_options", [])
        opt_str    = ""
        if options:
            opt_labels = [o.get("name", "") for o in options[:5]]
            opt_str    = f" | 選択肢: {', '.join(opt_labels)}"
            if len(options) > 5:
                opt_str += f" 他{len(options)-5}件"
        lines.append(f"[{key}] {title}（{field_type}）{opt_str}")
    return "\n".join(lines)


# ── KB 書き込み ────────────────────────────────────────────

def tool_create_kb_article(args: dict) -> str:
    section_id = args["section_id"]
    title      = args["title"]
    body       = args["body"]
    locale     = args.get("locale", "ja")
    payload    = {"article": {"title": title, "body": body, "locale": locale, "draft": True}}
    data       = zd_post(f"/help_center/sections/{section_id}/articles.json", payload)
    article    = data.get("article", {})
    lines = [
        "# 記事を下書きとして作成しました",
        f"ID      : {article.get('id')}",
        f"タイトル: {article.get('title')}",
        f"セクション: {article.get('section_id')}",
        f"URL     : {article.get('html_url', 'なし')}",
        f"作成日  : {article.get('created_at', '')[:10]}",
        "ステータス: 下書き（draft）",
    ]
    return "\n".join(lines)


def tool_update_kb_article(args: dict) -> str:
    article_id = args["article_id"]
    locale     = args.get("locale", "ja")
    payload    = {}
    if "title" in args:
        payload["title"] = args["title"]
    if "body" in args:
        payload["body"] = args["body"]
    if "draft" in args:
        payload["draft"] = args["draft"]
    if not payload:
        return "更新内容が指定されていません。title / body / draft のいずれかを指定してください。"
    data    = zd_put(f"/help_center/articles/{article_id}/translations/{locale}.json", {"translation": payload})
    article = data.get("translation", {})
    draft_status = "下書き（draft）" if article.get("draft") else "公開済み"
    lines = [
        "# 記事を更新しました",
        f"ID      : {article.get('source_id', article_id)}",
        f"タイトル: {article.get('title', '（取得不可）')}",
        f"URL     : {article.get('html_url', 'なし')}",
        f"更新日  : {article.get('updated_at', '')[:10]}",
        f"ステータス: {draft_status}",
    ]
    return "\n".join(lines)


# KB ツールは記事本文(サポート窓口の連絡先等)を返すため、regex マスクの対象外にする。
# Spec の Non-Goal「KB 記事本文のマスク」を尊重する。
_MASK_EXEMPT_TOOLS = {
    "get_kb_article", "search_kb_articles", "list_kb_articles",
    "list_kb_categories", "list_kb_sections",
}


TOOL_HANDLERS = {
    "search_tickets":     tool_search_tickets,
    "get_ticket":         tool_get_ticket,
    "list_tickets":       tool_list_tickets,
    "get_ticket_stats":   tool_get_ticket_stats,
    "suggest_reply":      tool_suggest_reply,
    "search_kb_articles": tool_search_kb_articles,
    "list_kb_articles":   tool_list_kb_articles,
    "get_kb_article":     tool_get_kb_article,
    "list_kb_categories": tool_list_kb_categories,
    "list_kb_sections":   tool_list_kb_sections,
    "get_csat_rating":    tool_get_csat_rating,
    "get_user":           tool_get_user,
    "list_user_fields":   tool_list_user_fields,
    "create_kb_article":  tool_create_kb_article,
    "update_kb_article":  tool_update_kb_article,
}


# ── MCP stdio ループ ──────────────────────────────────────

def handle(body: dict) -> dict | None:
    method = body.get("method", "")
    req_id = body.get("id")

    def ok(result):
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code, msg):
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": msg}}

    if method == "initialize":
        if not all([ZENDESK_SUBDOMAIN, ZENDESK_EMAIL, ZENDESK_API_TOKEN]):
            sys.stderr.write("[ERROR] 環境変数 ZENDESK_SUBDOMAIN / ZENDESK_EMAIL / ZENDESK_API_TOKEN が未設定です\n")
        return ok({
            "protocolVersion": "2024-11-05",
            "capabilities":    {"tools": {}},
            "serverInfo":      {"name": "zendesk-mcp", "version": "1.1.0"},
        })

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return ok({"tools": TOOLS})

    if method == "tools/call":
        name = body["params"]["name"]
        args = body["params"].get("arguments", {})
        if name not in TOOL_HANDLERS:
            return err(-32601, f"Unknown tool: {name}")
        try:
            result = TOOL_HANDLERS[name](args)
            if MASK_PII and name not in _MASK_EXEMPT_TOOLS:
                result = mask_pii_text(result)
            if not MASK_PII:
                result = _MASK_OFF_NOTICE + result
            return ok({"content": [{"type": "text", "text": result}]})
        except Exception as e:
            msg = str(e)
            if MASK_PII:
                msg = mask_pii_text(msg)
            return err(-32603, msg)

    return err(-32601, f"Method not found: {method}")


def main():
    sys.stderr.write("[zendesk-mcp] 起動しました（stdio モード）\n")
    sys.stderr.write(f"[zendesk-mcp] PII masking: {'ON' if MASK_PII else 'OFF'}\n")
    if not MASK_PII:
        # OFF スイッチは残す判断だが、監査上の抜け穴になりうるため警告を強く出す。
        sys.stderr.write(
            "[zendesk-mcp] *** 警告: ZENDESK_MASK_PII=0 により PII マスクが無効です ***\n"
            "[zendesk-mcp] *** 氏名・住所・メール・電話番号がそのまま LLM へ送信されます ***\n"
            "[zendesk-mcp] *** 社内規程・顧客との契約に違反する可能性があります ***\n"
        )
    elif ner_available():
        sys.stderr.write(f"[zendesk-mcp] 氏名・住所 redaction: NER 有効 ({_NER_STATE['reason']})\n")
    else:
        sys.stderr.write(
            f"[zendesk-mcp] 氏名・住所 redaction: NER 利用不可 ({_NER_STATE['reason']}) — "
            "縮退運転(自由記述は全て [REDACTED])\n"
        )
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            body = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle(body)
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()