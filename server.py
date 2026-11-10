#!/usr/bin/env python3
"""
Zendesk MCP Server（ローカル実行版）
通信方式: stdio (Claude Desktop / Claude Code 向け)

使い方:
  python server.py

環境変数:
  ZENDESK_SUBDOMAIN
  ZENDESK_OAUTH_CLIENT_ID      Zendesk で作成した OAuth クライアント(public)の識別子
  ZENDESK_OAUTH_SCOPES         任意。既定 "read write"
  ZENDESK_OAUTH_REDIRECT_PORT  任意。既定 47823 (リダイレクト URL http://127.0.0.1:<port>/callback)
  ZENDESK_OAUTH_TOKEN_CACHE    任意。トークンの保存先 (既定 ~/.config/zendesk-mcp/token-<subdomain>.json)
"""

import json
import os
import re
import sys
import ssl
import base64
import difflib
import hashlib
import html
import http.server
import secrets
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
import webbrowser
from datetime import datetime, timezone

# ── SSL コンテキスト ──────────────────────────────────────
# python.org 版の macOS Python や uv 管理の Python は OpenSSL の CA ストアが空のことがあり、
# ssl.create_default_context() だけでは CERTIFICATE_VERIFY_FAILED になる。
# 検証は無効化せず、次の順で信頼する CA を用意する:
#   1. truststore: OS の証明書ストア (macOS キーチェーン / Windows 証明書ストア) を使う。
#      社内プロキシ等で追加されたルート CA にも追従できる
#   2. 標準のデフォルト (SSL_CERT_FILE / SSL_CERT_DIR 環境変数も尊重される)
#      + certifi の CA バンドル
#   3. それでも CA が空なら OS 同梱の既知パス (/etc/ssl/cert.pem 等) を読む
#      (依存を入れずに system python3 で動かす経路向け)

_FALLBACK_CA_FILES = (
    "/etc/ssl/cert.pem",                    # macOS / Alpine / BSD
    "/etc/ssl/certs/ca-certificates.crt",   # Debian / Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",     # RHEL / Fedora
)


def _build_ssl_context() -> ssl.SSLContext:
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass

    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass
    if ctx.cert_store_stats().get("x509_ca", 0) == 0:
        for path in _FALLBACK_CA_FILES:
            if os.path.isfile(path):
                try:
                    ctx.load_verify_locations(cafile=path)
                    break
                except ssl.SSLError:
                    continue
    return ctx


_SSL_CTX = _build_ssl_context()

# ── 環境変数 ──────────────────────────────────────────────
def _normalize_subdomain(raw: str | None) -> str:
    """サブドメインの入力ゆれを吸収する。

    設定画面に URL やホスト名ごと入れられることが多いので、
    "https://yourcompany.zendesk.com/" や "yourcompany.zendesk.com" も "yourcompany" にする。"""
    s = (raw or "").strip().lower()
    s = re.sub(r"^https?://", "", s).split("/")[0]
    if s.endswith(".zendesk.com"):
        s = s[: -len(".zendesk.com")]
    return s


ZENDESK_SUBDOMAIN = _normalize_subdomain(os.environ.get("ZENDESK_SUBDOMAIN"))
ZENDESK_BASE      = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2"

ZENDESK_OAUTH_CLIENT_ID     = (os.environ.get("ZENDESK_OAUTH_CLIENT_ID") or "").strip()
ZENDESK_OAUTH_SCOPES        = os.environ.get("ZENDESK_OAUTH_SCOPES") or "read write"
ZENDESK_OAUTH_REDIRECT_PORT = int(os.environ.get("ZENDESK_OAUTH_REDIRECT_PORT") or 47823)
ZENDESK_OAUTH_TOKEN_CACHE   = os.environ.get("ZENDESK_OAUTH_TOKEN_CACHE", "")


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


def _parse_opt_in_env(value: str | None) -> bool:
    # MASK_PII とは逆に、未設定や不明値は安全側 (OFF) に倒す。明示的な ON のみ True。
    if value is None:
        return False
    return value.strip().lower() in ("1", "true", "yes", "on")


# 誤爆すると外部・顧客に影響しうる書き込み (チケットへの社内メモ投稿、ヘルプ記事の公開) の許可。
# デフォルト OFF。OFF の間は add_internal_note を tools/list に出さず、記事の公開も拒否する。
ENABLE_SENSITIVE_WRITES = _parse_opt_in_env(os.environ.get("ZENDESK_ENABLE_SENSITIVE_WRITES"))

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


# ── OAuth 認証 ────────────────────────────────────────────
# Zendesk の OAuth クライアント(public / PKCE)で Authorization Code Grant を行う。
# 初回のツール呼び出し時にブラウザで認可画面を開き、127.0.0.1 のコールバックで
# 認可コードを受け取ってトークンに交換する。トークンはローカルにキャッシュし、
# 期限切れ時は refresh token で自動更新する(refresh token は使い捨てなので毎回保存し直す)。

ZENDESK_OAUTH_URL = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/oauth"
_REDIRECT_URI     = f"http://127.0.0.1:{ZENDESK_OAUTH_REDIRECT_PORT}/callback"

# 認可待ちでツール呼び出しをブロックする上限秒数。MCP クライアント側のタイムアウトより
# 短くし、間に合わなければ案内文を返す(コールバック待ちはバックグラウンドで継続する)。
_AUTH_WAIT_SECONDS     = 45
_AUTH_CALLBACK_TIMEOUT = 300
# 期限ぎりぎりのトークンで失敗しないよう、残り時間がこれを切ったら更新する。
_TOKEN_REFRESH_MARGIN  = 60


class AuthRequired(Exception):
    """ユーザーのブラウザ操作が必要で、トークンをまだ用意できない状態。"""


def _token_cache_path() -> str:
    if ZENDESK_OAUTH_TOKEN_CACHE:
        return ZENDESK_OAUTH_TOKEN_CACHE
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "zendesk-mcp", f"token-{ZENDESK_SUBDOMAIN}.json")


def _load_token() -> dict | None:
    try:
        with open(_token_cache_path(), encoding="utf-8") as f:
            token = json.load(f)
    except (OSError, ValueError):
        return None
    # クライアントを差し替えたら古いトークンは使わない
    if token.get("client_id") != ZENDESK_OAUTH_CLIENT_ID:
        return None
    return token


def _save_token(token: dict) -> None:
    path = _token_cache_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(token, f)
    os.replace(tmp, path)


def _delete_token() -> None:
    try:
        os.remove(_token_cache_path())
    except OSError:
        pass


def _token_request(params: dict) -> dict:
    """/oauth/tokens を叩き、キャッシュ用に期限を絶対時刻へ変換して返す。"""
    data = urllib.parse.urlencode({**params, "client_id": ZENDESK_OAUTH_CLIENT_ID}).encode()
    req  = urllib.request.Request(
        f"{ZENDESK_OAUTH_URL}/tokens", data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as resp:
        body = json.loads(resp.read())
    now = time.time()
    token = {
        "client_id":     ZENDESK_OAUTH_CLIENT_ID,
        "access_token":  body["access_token"],
        "refresh_token": body.get("refresh_token"),
        "scope":         body.get("scope"),
    }
    if body.get("expires_in"):
        token["expires_at"] = now + int(body["expires_in"])
    if body.get("refresh_token_expires_in"):
        token["refresh_expires_at"] = now + int(body["refresh_token_expires_in"])
    return token


def _refresh(token: dict) -> dict | None:
    rt = token.get("refresh_token")
    if not rt:
        return None
    refresh_expires_at = token.get("refresh_expires_at")
    if refresh_expires_at is not None and refresh_expires_at <= time.time():
        return None
    try:
        new_token = _token_request({"grant_type": "refresh_token", "refresh_token": rt})
    except urllib.error.HTTPError as e:
        # invalid_grant 等。refresh token が失効しているので再認可に回す
        sys.stderr.write(f"[zendesk-mcp] トークン更新に失敗しました (HTTP {e.code})。再認可が必要です\n")
        _delete_token()
        return None
    _save_token(new_token)
    return new_token


class _PendingAuth:
    """進行中のブラウザ認可 1 件分。コールバックサーバーはバックグラウンドで動く。"""

    def __init__(self):
        self.state    = secrets.token_urlsafe(32)
        self.verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(self.verifier.encode()).digest()
        ).decode().rstrip("=")
        self.authorize_url = f"{ZENDESK_OAUTH_URL}/authorizations/new?" + urllib.parse.urlencode({
            "response_type":         "code",
            "client_id":             ZENDESK_OAUTH_CLIENT_ID,
            "redirect_uri":          _REDIRECT_URI,
            "scope":                 ZENDESK_OAUTH_SCOPES,
            "state":                 self.state,
            "code_challenge":        challenge,
            "code_challenge_method": "S256",
        })
        self.done   = threading.Event()
        self.error: str | None = None
        self.token: dict | None = None

        pending = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                if url.path != "/callback":
                    self.send_error(404)
                    return
                q = urllib.parse.parse_qs(url.query)
                if q.get("state", [""])[0] != pending.state:
                    # 別タブ等からの不正なリクエストは無視し、正規のコールバックを待ち続ける
                    self._reply(400, "state が一致しません。Claude からもう一度やり直してください。")
                    return
                if "error" in q:
                    pending.error = q.get("error_description", q["error"])[0]
                    self._reply(400, f"認可が拒否されました: {pending.error}")
                elif "code" in q:
                    try:
                        pending.token = _token_request({
                            "grant_type":    "authorization_code",
                            "code":          q["code"][0],
                            "redirect_uri":  _REDIRECT_URI,
                            "code_verifier": pending.verifier,
                            "scope":         ZENDESK_OAUTH_SCOPES,
                        })
                        _save_token(pending.token)
                        self._reply(200, "Zendesk の認可が完了しました。このタブを閉じて Claude に戻ってください。")
                    except Exception as e:
                        pending.error = f"トークン交換に失敗しました: {e}"
                        self._reply(500, pending.error)
                else:
                    self._reply(400, "認可コードがありません。")
                    return
                pending.done.set()

            def _reply(self, status: int, message: str):
                body = f"<!doctype html><meta charset=utf-8><title>Zendesk MCP</title><p>{html.escape(message)}</p>"
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *args):
                # stdout は MCP の JSON-RPC 専用なので、アクセスログは出さない
                pass

        # 127.0.0.1 のみで待ち受ける(他ホストから認可コードを送り込まれないように)
        self.server = http.server.HTTPServer(("127.0.0.1", ZENDESK_OAUTH_REDIRECT_PORT), Handler)
        self.server.timeout = 1
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        deadline = time.time() + _AUTH_CALLBACK_TIMEOUT
        while not self.done.is_set() and time.time() < deadline:
            self.server.handle_request()
        if not self.done.is_set():
            self.error = "認可の待ち時間が切れました"
            self.done.set()
        self.server.server_close()


_token_lock = threading.Lock()
_pending_auth: _PendingAuth | None = None


def _start_or_get_pending_auth() -> _PendingAuth:
    global _pending_auth
    if _pending_auth is None or (_pending_auth.done.is_set() and _pending_auth.token is None):
        try:
            _pending_auth = _PendingAuth()
        except OSError as e:
            raise RuntimeError(
                f"OAuth コールバック用のポート {ZENDESK_OAUTH_REDIRECT_PORT} を開けません ({e})。"
                "ZENDESK_OAUTH_REDIRECT_PORT で別のポートを指定し、Zendesk の OAuth クライアントの"
                "リダイレクト URL も合わせて変更してください。"
            ) from e
        sys.stderr.write(f"[zendesk-mcp] ブラウザで Zendesk の認可を行ってください: {_pending_auth.authorize_url}\n")
        try:
            webbrowser.open(_pending_auth.authorize_url)
        except Exception:
            pass
    return _pending_auth


def _get_access_token(force_refresh: bool = False) -> str:
    global _pending_auth
    with _token_lock:
        token = _load_token()
        if token:
            expires_at = token.get("expires_at")
            if not force_refresh and (expires_at is None or expires_at - _TOKEN_REFRESH_MARGIN > time.time()):
                return token["access_token"]
            token = _refresh(token)
            if token:
                return token["access_token"]

        pending = _start_or_get_pending_auth()

    pending.done.wait(_AUTH_WAIT_SECONDS)
    with _token_lock:
        if pending.token:
            _pending_auth = None
            return pending.token["access_token"]
        if pending.done.is_set():
            raise AuthRequired(f"Zendesk の認可に失敗しました: {pending.error}。もう一度実行すると認可をやり直します。")
        raise AuthRequired(
            "Zendesk の OAuth 認可が必要です。ブラウザで開いた認可画面で「許可」を押してから、"
            "もう一度実行してください。ブラウザが開かない場合は次の URL を開いてください:\n"
            f"{pending.authorize_url}"
        )


# ── Zendesk API ヘルパー ──────────────────────────────────

def zd_request(url: str, method: str = "GET", payload: dict | None = None,
               timeout: int = 20) -> dict:
    """Bearer トークン付きで Zendesk API を呼ぶ。401 のときは 1 度だけトークンを更新して再試行する。"""
    data = json.dumps(payload).encode() if payload is not None else None
    for attempt in range(2):
        headers = {"Authorization": f"Bearer {_get_access_token(force_refresh=attempt > 0)}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code != 401 or attempt > 0:
                raise
    raise AssertionError("unreachable")


def zd_get(path: str, params: dict | None = None) -> dict:
    url = f"{ZENDESK_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return zd_request(url)


def zd_post(path: str, payload: dict) -> dict:
    return zd_request(f"{ZENDESK_BASE}{path}", "POST", payload)


def zd_put(path: str, payload: dict) -> dict:
    return zd_request(f"{ZENDESK_BASE}{path}", "PUT", payload)


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
        "description": "チケットをキーワード・ステータス・タグ・担当者・日付・カスタムフィールドの値等で検索する。keywordはタイトル・コメント本文の全文検索。custom_fields を指定すると各チケットのカスタムフィールドの値も一覧に出す",
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
                "custom_field_filters": {"type": "object", "additionalProperties": {"type": ["string", "number", "boolean"]}, "description": "カスタムフィールドの値で絞り込む。{\"表示名またはフィールドID\": \"値\"} の形（例: {\"製品名\": \"Model X\"}）。選択肢は表示名でも値（タグ）でも可。複数指定はAND。フィールド名・選択肢は list_ticket_fields で確認できる"},
                "custom_fields":  {"type": "array", "items": {"type": ["string", "integer"]}, "description": "各チケットの行に値を表示するカスタムフィールド（表示名またはID）のリスト（例: ['製品名', 'OS']）"},
                "include_all_custom_fields": {"type": "boolean", "description": "値の入っているカスタムフィールドをすべて各行に表示する（件数が多いと長くなる）", "default": False},
            },
        },
    },
    {
        "name": "get_ticket",
        "description": (
            "チケットIDを指定して詳細情報・カスタムフィールド・コメント一覧を取得する。"
            "コメントごとに公開返信/社内メモの別と、投稿者（顧客/エージェント/Claude など）を表示する"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id":      {"type": "integer", "description": "ZendeskチケットID"},
                "max_body_chars": {"type": "integer", "description": "コメント1件あたりの最大文字数（デフォルト800）。0で全文", "default": 800},
            },
            "required": ["ticket_id"],
        },
    },
    {
        "name": "list_tickets",
        "description": "最近のチケット一覧を取得する（傾向分析向け）。タグ・日付・カスタムフィールドの値で絞り込める。custom_fields を指定すると各チケットのカスタムフィールドの値も一覧に出す",
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
                "custom_field_filters": {"type": "object", "additionalProperties": {"type": ["string", "number", "boolean"]}, "description": "カスタムフィールドの値で絞り込む。{\"表示名またはフィールドID\": \"値\"} の形（例: {\"製品名\": \"Model X\"}）。選択肢は表示名でも値（タグ）でも可。複数指定はAND。フィールド名・選択肢は list_ticket_fields で確認できる"},
                "custom_fields":  {"type": "array", "items": {"type": ["string", "integer"]}, "description": "各チケットの行に値を表示するカスタムフィールド（表示名またはID）のリスト（例: ['製品名', 'OS']）"},
                "include_all_custom_fields": {"type": "boolean", "description": "値の入っているカスタムフィールドをすべて各行に表示する（件数が多いと長くなる）", "default": False},
            },
        },
    },
    {
        "name": "list_ticket_fields",
        "description": (
            "チケットのカスタムフィールドの定義一覧（フィールドID・表示名・種類・ドロップダウンの選択肢と値）を取得する。"
            "search_tickets / list_tickets の custom_field_filters や custom_fields に指定する名前・値を調べるのに使う"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword":          {"type": "string",  "description": "フィールド名または選択肢名に含まれる文字列で絞り込む（例: '製品'）"},
                "include_inactive": {"type": "boolean", "description": "無効化されたフィールドも含める", "default": False},
                "include_system":   {"type": "boolean", "description": "件名・ステータスなどの標準フィールドも含める", "default": False},
                "max_options":      {"type": "integer", "description": "フィールドごとに表示する選択肢の上限（デフォルト: keyword なしは5、ありは200）。0で全件"},
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

    # ── チケット書き込み (ENABLE_SENSITIVE_WRITES のときだけ公開) ──
    {
        "name": "add_internal_note",
        "description": (
            "チケットに社内メモ（非公開コメント・顧客には見えない）を追加する。公開返信は送れない。"
            "顧客への返信の下書きを残すときだけ kind=\"draft\" にする（本文先頭に【返信下書き】、チケットに claude_draft タグが付き、"
            "compare_draft_replies で実際の返信と比較できるようになる）。調査メモ・引き継ぎ等は kind=\"memo\"（デフォルト）。"
            "本文に [EMAIL] などのマスク用プレースホルダを含めないこと"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer", "description": "社内メモを追加するチケットID"},
                "body":      {"type": "string",  "description": "社内メモの本文（プレーンテキスト）"},
                "kind":      {"type": "string",  "enum": ["memo", "draft"], "default": "memo",
                              "description": "memo: 通常の社内メモ（デフォルト） / draft: 顧客への返信の下書き"},
            },
            "required": ["ticket_id", "body"],
        },
    },
    {
        "name": "compare_draft_replies",
        "description": (
            "add_internal_note(kind=draft) で残した返信下書きと、その後にエージェントが実際に送った公開返信を組にして返す。"
            "類似度（0〜100%）と両方の本文を返すので、どこが直されたかの分析に使う。"
            "ticket_id を指定するとそのチケットだけ、省略すると claude_draft タグの付いたチケットをまとめて調べる"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id":      {"type": "integer", "description": "対象チケットID（省略時は claude_draft タグのチケットを検索）"},
                "updated_after":  {"type": "string",  "description": "この日付以降に更新されたチケット（YYYY-MM-DD）"},
                "updated_before": {"type": "string",  "description": "この日付以前に更新されたチケット（YYYY-MM-DD）"},
                "max_tickets":    {"type": "integer", "description": "調べるチケットの上限（デフォルト20、最大100）", "default": 20},
                "max_body_chars": {"type": "integer", "description": "本文1件あたりの最大文字数（デフォルト2000）。0で全文", "default": 2000},
            },
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
                "draft":      {"type": "boolean", "description": "Trueで下書きに戻す、Falseで公開（省略時は変更なし）。公開は「社内メモ投稿・記事公開を許可」がオンのときだけ可能"},
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
        data = zd_request(url, timeout=30)

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

    cf_query = _custom_field_query(args.get("custom_field_filters"))
    if cf_query:
        query = f"{query} {cf_query}".strip()
    selector = _list_field_selector(args)

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
        lines.append(f"[#{t['id']}] {subject} | {t['status']} | requester_id: {requester_id} | 担当者ID: {assignee_id} | 作成: {t['created_at'][:10]} | タグ: {','.join(t.get('tags',[]))}{_inline_custom_fields(t, selector)}")
    return "\n".join(lines) + _remaining_msg(remaining)


# ── チケットフィールド名の解決 ────────────────────────────

_TICKET_FIELD_TITLES: dict[int, str] | None = None
_TICKET_FIELD_TYPES: dict[int, str] = {}
# id -> {選択肢の値(タグ): 表示名}。ドロップダウン・複数選択の値を表示名に戻すのに使う。
_TICKET_FIELD_OPTIONS: dict[int, dict[str, str]] = {}
# /ticket_fields.json の生データ (list_ticket_fields 用)
_TICKET_FIELD_DEFS: list[dict] = []


def _ticket_field_titles() -> dict[int, str]:
    """カスタムフィールド id -> 表示名。初回のみ API を叩いてプロセス内にキャッシュ。
    同じ応答からフィールドの種類 (id -> type)・選択肢 (id -> {値: 表示名})・定義そのものも保持する。

    取得に失敗したときは空の辞書を返すが、キャッシュはしない (次の呼び出しで再取得する)。"""
    global _TICKET_FIELD_TITLES, _TICKET_FIELD_TYPES, _TICKET_FIELD_OPTIONS, _TICKET_FIELD_DEFS
    if _TICKET_FIELD_TITLES is None:
        try:
            # 100 件を超えるとページングされるので全ページ取る
            fields, _ = zd_get_all("/ticket_fields.json", "ticket_fields", max_total=10000)
        except Exception:
            return {}
        _TICKET_FIELD_DEFS = fields
        _TICKET_FIELD_TYPES = {f["id"]: f.get("type", "") for f in fields}
        _TICKET_FIELD_OPTIONS = {
            f["id"]: {
                str(o.get("value")): o.get("name") or str(o.get("value"))
                for o in (f.get("custom_field_options") or [])
                if o.get("value") is not None
            }
            for f in fields
            if f.get("custom_field_options")
        }
        _TICKET_FIELD_TITLES = {f["id"]: f.get("title", "") for f in fields}
    return _TICKET_FIELD_TITLES


def _option_name(field_id, value) -> str:
    """ドロップダウン・複数選択の値 (タグ) を表示名にする。定義に無ければ値のまま。"""
    return _TICKET_FIELD_OPTIONS.get(field_id, {}).get(str(value), str(value))


def _resolve_ticket_field(ref) -> int:
    """フィールド ID (数値・数字文字列) または表示名からカスタムフィールド ID を引く。

    表示名は 完全一致 → 大文字小文字を無視した一致 → 部分一致 (1件に絞れるときだけ) の順に探す。
    見つからない・複数に当たるときは、候補を添えて ValueError を投げる。"""
    titles = _ticket_field_titles()
    text = str(ref).strip()
    if text.isdigit():
        fid = int(text)
        if titles and fid not in titles:
            raise ValueError(f"カスタムフィールド ID {fid} は存在しません。list_ticket_fields で確認してください")
        return fid
    for match in (
        lambda t: t == text,
        lambda t: t.lower() == text.lower(),
        lambda t: text.lower() in t.lower(),
    ):
        hits = [fid for fid, t in titles.items() if match(t)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            names = ", ".join(f"{titles[h]} (ID {h})" for h in hits[:10])
            raise ValueError(f"カスタムフィールド「{text}」に当てはまるものが複数あります: {names}。ID で指定してください")
    raise ValueError(f"カスタムフィールド「{text}」が見つかりません。list_ticket_fields で確認してください")


def _search_value(fid: int, value) -> str:
    """検索条件の値を Zendesk 検索の書式にする。選択肢は表示名でも値 (タグ) でも受け付ける。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip()
    options = _TICKET_FIELD_OPTIONS.get(fid)
    if options and text not in options:
        by_name = [v for v, n in options.items() if n == text] or \
                  [v for v, n in options.items() if n.lower() == text.lower() or v.lower() == text.lower()]
        if len(by_name) == 1:
            text = by_name[0]
        else:
            # 表示名が「親::子」形式のとき、末尾の名前だけでも当てる
            by_leaf = [v for v, n in options.items() if n.split("::")[-1].strip() == text]
            if len(by_leaf) == 1:
                text = by_leaf[0]
            else:
                sample = ", ".join(f"{n} (= {v})" for v, n in list(options.items())[:10])
                raise ValueError(f"「{text}」は {_TICKET_FIELD_TITLES.get(fid, fid)} の選択肢にありません。選択肢の例: {sample}")
    if re.search(r"\s", text) or '"' in text:
        text = '"' + text.replace('"', '') + '"'
    return text


def _custom_field_query(filters) -> str:
    """{フィールド(表示名 or ID): 値} を `custom_field_<id>:<値>` の検索修飾子にする。"""
    if not filters:
        return ""
    if not isinstance(filters, dict):
        raise ValueError("custom_field_filters は {\"フィールド名またはID\": \"値\"} の形で指定してください")
    parts = []
    for ref, value in filters.items():
        fid = _resolve_ticket_field(ref)
        parts.append(f"custom_field_{fid}:{_search_value(fid, value)}")
    return " ".join(parts)


# 人名を保持するカスタムフィールド。regex では人名を検出できないため、
# get_user と同様にフィールド単位でプレースホルダに差し替える。
_NAME_FIELD_TITLES = ("名前", "氏名", "お名前")

# 値が選択肢・数値・日付に限られる種類。自由記述ではないので文単位 redaction を通さない。
# (NER は ID・タグ・数値を人名/地名と誤判定しやすく、通すと値が読めなくなる)
_STRUCTURED_FIELD_TYPES = {
    "tagger", "multiselect", "checkbox", "integer", "decimal", "date", "regexp", "lookup",
}

# シリアル番号・会話IDなどの識別子とみなす値 (空白・かなを含まない英数字と記号のみ)。
_IDENTIFIER_VALUE_RE = re.compile(r"[A-Za-z0-9_\-.:/#+]+")


def _mask_ticket_field(title: str, field_type: str, value: str) -> str:
    if not MASK_PII:
        return value
    if title in _NAME_FIELD_TITLES:
        return _NAME_PLACEHOLDER
    if _ADDRESS_FIELD_KEY_RE.search(title):
        return _ADDRESS_PLACEHOLDER
    if field_type in _STRUCTURED_FIELD_TYPES or _IDENTIFIER_VALUE_RE.fullmatch(value):
        # メール・電話番号は handle() のチョークポイント (mask_pii_text) で別途置換される
        return value
    # 自由記述はユーザーカスタムフィールドと同じ文単位 redaction を通す
    return mask_user_field(title, value)


def _custom_field_pairs(t: dict, only_ids: list[int] | None = None) -> list[tuple[str, str]]:
    """値が入っているカスタムフィールドを (表示名, 値) の組にする。

    選択肢の値 (タグ) は表示名に戻し、マスク規則を適用する。
    only_ids を渡すとそのフィールドだけを、その順で返す。"""
    titles = _ticket_field_titles()
    by_id = {cf.get("id"): cf.get("value") for cf in (t.get("custom_fields") or [])}
    ids = only_ids if only_ids is not None else list(by_id)
    pairs = []
    for fid in ids:
        v = by_id.get(fid)
        if v is None or v == "" or v is False or v == []:
            continue
        if isinstance(v, list):
            v = ", ".join(_option_name(fid, x) for x in v)
        else:
            v = _option_name(fid, v)
        title = titles.get(fid) or f"field_{fid}"
        pairs.append((title, _mask_ticket_field(title, _TICKET_FIELD_TYPES.get(fid, ""), str(v))))
    return pairs


def _format_custom_fields(t: dict) -> list[str]:
    """値が入っているカスタムフィールドだけを「表示名: 値」の行にする。"""
    return [f"- {title}: {v}" for title, v in _custom_field_pairs(t)]


def _list_field_selector(args: dict):
    """一覧系ツールの custom_fields / include_all_custom_fields 引数を解釈する。
    戻り値: None (表示しない) / [] 以外の ID リスト / "all"。"""
    if args.get("include_all_custom_fields"):
        return "all"
    refs = args.get("custom_fields") or []
    if isinstance(refs, (str, int)):
        refs = [refs]
    return [_resolve_ticket_field(r) for r in refs] or None


def _inline_custom_fields(t: dict, selector) -> str:
    """一覧の1行に付け足すカスタムフィールド部分 (例: ' | 製品名: Model X; OS: iOS')。"""
    if not selector:
        return ""
    pairs = _custom_field_pairs(t, None if selector == "all" else selector)
    if not pairs:
        return ""
    return " | " + "; ".join(f"{title}: {v.replace(chr(10), ' ')}" for title, v in pairs)


# Zendesk 標準のチケットフィールドの種類 (removable が true でも標準扱いにする)
_SYSTEM_FIELD_TYPES = {
    "subject", "description", "status", "custom_status", "tickettype", "priority",
    "group", "assignee", "brand",
}


def _is_system_field(f: dict) -> bool:
    return not f.get("removable", True) or f.get("type") in _SYSTEM_FIELD_TYPES


def tool_list_ticket_fields(args: dict) -> str:
    keyword          = (args.get("keyword") or "").strip()
    include_inactive = bool(args.get("include_inactive"))
    include_system   = bool(args.get("include_system"))
    max_options      = args.get("max_options", 5 if not keyword else 200)

    if not _ticket_field_titles():
        return "チケットフィールドの定義を取得できませんでした（権限がないか、通信に失敗しました）"

    fields = []
    for f in _TICKET_FIELD_DEFS:
        if not include_system and _is_system_field(f):
            continue  # 件名・ステータスなどの標準フィールド
        if not include_inactive and not f.get("active", True):
            continue
        if keyword:
            names = [f.get("title", "")] + [o.get("name", "") for o in (f.get("custom_field_options") or [])]
            if not any(keyword.lower() in n.lower() for n in names):
                continue
        fields.append(f)

    label = f"（「{keyword}」で絞り込み）" if keyword else ""
    lines = [
        f"チケットフィールド一覧{label}: {len(fields)} 件",
        "検索・一覧の絞り込みは custom_field_filters に {\"表示名またはID\": \"値\"} を渡す"
        "（Zendesk 検索修飾子では custom_field_<ID>:<値>）。選択肢は表示名でも値でも指定できる。\n",
    ]
    for f in fields:
        flags = []
        if not f.get("active", True):
            flags.append("無効")
        if _is_system_field(f):
            flags.append("標準")
        flag = f" [{'・'.join(flags)}]" if flags else ""
        lines.append(f"[ID {f['id']}] {f.get('title', '')}（{f.get('type', '?')}）{flag}")
        options = f.get("custom_field_options") or []
        if options:
            if keyword and keyword.lower() not in f.get("title", "").lower():
                # 選択肢の名前で当たったときは、当たった選択肢だけ出す
                options = [o for o in options if keyword.lower() in (o.get("name") or "").lower()]
            shown = options[:max_options] if max_options else options
            for o in shown:
                lines.append(f"    - {o.get('name', '')} = {o.get('value', '')}")
            if len(options) > len(shown):
                lines.append(f"    …他 {len(options) - len(shown)} 件（keyword で絞り込むか max_options を増やすと表示）")
    return "\n".join(lines)


# ── コメント取得・投稿者の判定 ────────────────────────────

# 返信下書きの目印。本文先頭に付け、チケットにはタグを付ける (タグはチケット単位なので、
# 同じチケット内のメモと下書きを区別するためにコメント単位の目印が要る)。
DRAFT_MARKER = "【返信下書き】"
DRAFT_TAG    = "claude_draft"


def _all_comments(tid: int) -> list:
    """チケットの全コメントを古い順に取得する (100 件を超える場合もページを辿る)。"""
    cmts, _ = zd_get_all(f"/tickets/{tid}/comments.json", "comments", max_total=10_000)
    return cmts


def _user_roles(user_ids) -> dict[int, str]:
    """user_id -> role (end-user / agent / admin)。取得に失敗したら空 dict。"""
    ids = sorted({i for i in user_ids if i})
    roles: dict[int, str] = {}
    try:
        for i in range(0, len(ids), 100):
            chunk = ",".join(str(x) for x in ids[i:i + 100])
            for u in zd_get("/users/show_many.json", {"ids": chunk}).get("users", []):
                roles[u["id"]] = u.get("role", "")
    except Exception:
        return {}
    return roles


def _claude_author_id() -> int | None:
    try:
        return _note_author_id()
    except RuntimeError:
        return None


def _is_agent(aid, requester_id, roles: dict) -> bool:
    """エージェント/管理者か。ロールが引けないときは「依頼者以外ならエージェント」とみなす。"""
    role = roles.get(aid)
    if role:
        return role in _AGENT_ROLES
    return aid != requester_id


def _comment_author_label(c: dict, requester_id, roles: dict, claude_id) -> str:
    aid = c.get("author_id")
    if claude_id is not None and aid == claude_id:
        return "Claude"
    # 依頼者がエージェント自身の社内チケットでは、本人の社内メモを「顧客」と表示しない
    if aid == requester_id and (c.get("public") or not _is_agent(aid, requester_id, roles)):
        return "顧客"
    role = roles.get(aid)
    if role in _AGENT_ROLES:
        return "エージェント"
    if role == "end-user":
        return "エンドユーザー"
    return "エージェント"  # ロール不明時は従来どおり


def _truncate(text: str, limit: int) -> str:
    if limit and len(text) > limit:
        return text[:limit] + f"…（以下 {len(text) - limit} 文字省略）"
    return text


def _is_draft(c: dict) -> bool:
    return (not c.get("public")) and (c.get("body") or "").lstrip().startswith(DRAFT_MARKER)


def tool_get_ticket(args: dict) -> str:
    tid   = args["ticket_id"]
    limit = max(int(args.get("max_body_chars", 800) or 0), 0)
    t     = zd_get(f"/tickets/{tid}.json")["ticket"]
    cmts  = _all_comments(tid)
    roles = _user_roles(c.get("author_id") for c in cmts)
    claude_id = _claude_author_id()
    lines = [
        f"# チケット #{tid}: {mask_free_text(t.get('subject') or '')}",
        f"ステータス: {t['status']} | 優先度: {t.get('priority','なし')}",
        f"作成: {t['created_at'][:10]} | 更新: {t['updated_at'][:10]}",
        f"requester_id: {t.get('requester_id','なし')} | assignee_id: {t.get('assignee_id','なし')}",
        f"タグ: {', '.join(t.get('tags',[])) or 'なし'}",
    ]
    cf_rows = _format_custom_fields(t)
    if cf_rows:
        lines += ["", "## カスタムフィールド"] + cf_rows
    lines += ["", f"## コメント（{len(cmts)} 件）"]
    for c in cmts:
        author = _comment_author_label(c, t.get("requester_id"), roles, claude_id)
        kind   = "公開返信" if c.get("public") else ("社内メモ・返信下書き" if _is_draft(c) else "社内メモ")
        body   = mask_free_text(_truncate(c.get("body") or "", limit))
        lines.append(f"\n[{author} / {kind} / {c['created_at'][:16].replace('T', ' ')}]\n{body}")
    return "\n".join(lines)


# ── 返信下書きと実際の返信の比較 ──────────────────────────

def _strip_marker(text: str) -> str:
    text = text.lstrip()
    return text[len(DRAFT_MARKER):].lstrip() if text.startswith(DRAFT_MARKER) else text


def _similarity(a: str, b: str) -> int:
    """空白の違いを無視した文字列の類似度 (0〜100)。"""
    norm = lambda x: re.sub(r"\s+", "", x)
    return round(difflib.SequenceMatcher(None, norm(a), norm(b), autojunk=False).ratio() * 100)


def _pair_drafts(cmts: list, requester_id, roles: dict, claude_id) -> list[dict]:
    """下書きごとに、その後に最初に送られたエージェントの公開返信を対応付ける。
    返信より前に次の下書きが来た場合、前の下書きは「差し替え」扱いにする。"""
    pairs = []
    for i, c in enumerate(cmts):
        if not _is_draft(c):
            continue
        pair = {"draft": c, "reply": None, "superseded": False}
        for later in cmts[i + 1:]:
            if _is_draft(later):
                pair["superseded"] = True
                break
            aid = later.get("author_id")
            if later.get("public") and aid != claude_id and _is_agent(aid, requester_id, roles):
                pair["reply"] = later
                break
        pairs.append(pair)
    return pairs


def tool_compare_draft_replies(args: dict) -> str:
    limit = max(int(args.get("max_body_chars", 2000) or 0), 0)
    if args.get("ticket_id"):
        tickets = [zd_get(f"/tickets/{args['ticket_id']}.json")["ticket"]]
        remaining = None
    else:
        q = f"type:ticket tags:{DRAFT_TAG}"
        if args.get("updated_after"):
            q += f" updated>{args['updated_after']}"
        if args.get("updated_before"):
            q += f" updated<{args['updated_before']}"
        max_tickets = min(max(int(args.get("max_tickets", 20) or 20), 1), 100)
        tickets, remaining = zd_get_all("/search.json", "results",
                                        {"query": q, "sort_by": "updated_at", "sort_order": "desc"},
                                        max_total=max_tickets)

    claude_id = _claude_author_id()
    sections, sims, n_pending, n_superseded = [], [], 0, 0
    for t in tickets:
        cmts  = _all_comments(t["id"])
        roles = _user_roles(c.get("author_id") for c in cmts)
        for p in _pair_drafts(cmts, t.get("requester_id"), roles, claude_id):
            d = p["draft"]
            head = (f"### チケット #{t['id']}: {mask_free_text(t.get('subject') or '')}\n"
                    f"下書き: {d['created_at'][:16].replace('T', ' ')}")
            draft_text = _strip_marker(d.get("body") or "")
            if p["superseded"]:
                n_superseded += 1
                sections.append(head + "\n状態: 差し替え（返信より前に新しい下書きあり）")
                continue
            if p["reply"] is None:
                n_pending += 1
                sections.append(head + "\n状態: 未返信（下書きの後にエージェントの公開返信なし）\n\n"
                                f"--- 下書き ---\n{mask_free_text(_truncate(draft_text, limit))}")
                continue
            r = p["reply"]; reply_text = r.get("body") or ""
            sim = _similarity(draft_text, reply_text); sims.append(sim)
            sections.append(
                head + f" → 返信: {r['created_at'][:16].replace('T', ' ')}\n"
                f"類似度: {sim}%\n\n"
                f"--- 下書き ---\n{mask_free_text(_truncate(draft_text, limit))}\n\n"
                f"--- 実際の返信 ---\n{mask_free_text(_truncate(reply_text, limit))}"
            )

    summary = [
        "# 返信下書きと実際の返信の比較", "",
        f"対象チケット: {len(tickets)} 件 | 比較できた組: {len(sims)} 件 | 未返信: {n_pending} 件 | 差し替え: {n_superseded} 件",
    ]
    if sims:
        summary.append(f"類似度: 平均 {round(sum(sims) / len(sims))}% / 最小 {min(sims)}% / 最大 {max(sims)}%")
    if not sections:
        summary.append("\n返信下書き（kind=draft の社内メモ）が見つかりませんでした。")
    return "\n".join(summary) + "\n\n" + "\n\n".join(sections) + _remaining_msg(remaining)


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

    cf_query = _custom_field_query(args.get("custom_field_filters"))
    selector = _list_field_selector(args)

    if tags or date_query or cf_query:
        tag_query = " ".join(f"tags:{t}" for t in tags)
        query     = f"type:ticket status:{status} {tag_query}{date_query} {cf_query}".strip()
        query     = re.sub(r"\s+", " ", query)
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
        if cf_query:
            label_parts.append(cf_query)
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
        lines.append(f"[#{t['id']}] {subject} | 更新: {t['updated_at'][:10]} | タグ: {','.join(t.get('tags',[]))}{_inline_custom_fields(t, selector)}")
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
    return zd_request(url)


def _get_survey_response_detail(sr_id: str, locale: str = "ja") -> dict:
    url = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2/guide/{locale}/survey_responses/{sr_id}"
    return zd_request(url).get("survey_response", {})


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
        if args["draft"] is False and not ENABLE_SENSITIVE_WRITES:
            return (
                "記事の公開は無効化されています。下書きのまま更新するか、拡張機能の設定で"
                "「社内メモ投稿・記事公開を許可」をオンにしてください。"
            )
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


# ── 社内メモ投稿 ──────────────────────────────────────────
# 誤爆防止の方針:
#   - public は常に False をコードで固定し、呼び出し側から変更する手段を持たせない
#   - 投稿後は Zendesk の監査ログで、作られたコメントが非公開であることを確かめる
#   - 投稿者を別ユーザー (例: Claude 用のエージェント) にする場合は、そのユーザーが
#     エージェント/管理者であることを投稿前に確かめる。エンドユーザーは社内メモを
#     書けないため、投稿者に指定すると公開コメント扱いになるおそれがある

# 社内メモの投稿者にする Zendesk ユーザーID。未設定ならログイン中の本人が投稿者になる。
# いずれの場合も、監査ログ上の更新者 (updater) はログイン中の本人として残る。
ZENDESK_NOTE_AUTHOR_ID = (os.environ.get("ZENDESK_NOTE_AUTHOR_ID") or "").strip()

_AGENT_ROLES = ("agent", "admin")
_NOTE_AUTHOR_CACHE: dict[int, str] = {}  # user_id -> 表示名 (検証済みのもの)


def _note_author_id() -> int | None:
    if not ZENDESK_NOTE_AUTHOR_ID:
        return None
    try:
        return int(ZENDESK_NOTE_AUTHOR_ID)
    except ValueError:
        raise RuntimeError(f"社内メモの投稿者ID（ZENDESK_NOTE_AUTHOR_ID）が数値ではありません: {ZENDESK_NOTE_AUTHOR_ID!r}")


def _verify_note_author(user_id: int) -> str:
    """投稿者に指定したユーザーがエージェント/管理者であることを確かめ、表示名を返す。"""
    if user_id not in _NOTE_AUTHOR_CACHE:
        user = zd_get(f"/users/{user_id}.json").get("user") or {}
        if user.get("role") not in _AGENT_ROLES or not user.get("active", True):
            raise RuntimeError(
                f"社内メモの投稿者に指定されたユーザー {user_id} は有効なエージェント/管理者ではありません"
                f"（role: {user.get('role')}）。エンドユーザーは社内メモを書けないため投稿を中止しました。"
            )
        _NOTE_AUTHOR_CACHE[user_id] = user.get("name") or str(user_id)
    return _NOTE_AUTHOR_CACHE[user_id]


def tool_add_internal_note(args: dict) -> str:
    if not ENABLE_SENSITIVE_WRITES:
        return "社内メモの投稿は無効化されています。拡張機能の設定で「社内メモ投稿・記事公開を許可」をオンにしてください。"
    tid  = int(args["ticket_id"])
    body = args.get("body") or ""
    kind = args.get("kind") or "memo"
    if not body.strip():
        return "本文が空です。"
    if kind not in ("memo", "draft"):
        return f"kind は memo か draft を指定してください（指定値: {kind!r}）。"
    if kind == "draft" and not body.lstrip().startswith(DRAFT_MARKER):
        body = f"{DRAFT_MARKER}\n{body}"

    author_id = _note_author_id()
    author_label = "ログイン中のユーザー"
    if author_id is not None:
        author_label = f"{_verify_note_author(author_id)}（ID: {author_id}）"

    comment = {"body": body, "public": False}
    if author_id is not None:
        comment["author_id"] = author_id
    data   = zd_put(f"/tickets/{tid}.json", {"ticket": {"comment": comment}})
    events = (data.get("audit") or {}).get("events") or []
    comments = [e for e in events if e.get("type") == "Comment"]
    if not comments:
        raise RuntimeError(f"チケット #{tid} の更新結果にコメントが見つかりません。Zendesk 上で内容を確認してください。")
    if any(c.get("public") for c in comments):
        raise RuntimeError(
            f"警告: チケット #{tid} に公開コメントとして記録された可能性があります。"
            "直ちに Zendesk 上で確認してください。"
        )
    note = comments[0]
    tag_warning = None
    if kind == "draft":
        # PUT /tags は既存タグを残したまま追加する (POST は置き換え)。チケット更新の
        # additional_tags は単体更新では無視されるため使わない。
        try:
            zd_put(f"/tickets/{tid}/tags.json", {"tags": [DRAFT_TAG]})
        except Exception as e:
            tag_warning = f"⚠️ {DRAFT_TAG} タグの付与に失敗しました（{e}）。下書きは投稿済みです。"
    lines = [
        "# 社内メモを投稿しました",
        "",
        f"チケット: #{tid}",
        f"コメントID: {note.get('id')}（非公開を確認済み）",
        f"投稿者: {author_label}",
        f"種類: {'返信下書き（claude_draft タグを付与）' if kind == 'draft' else '社内メモ'}",
    ]
    if tag_warning:
        lines.append(tag_warning)
    if author_id is not None and note.get("author_id") != author_id:
        lines.append(
            f"⚠️ 投稿者が指定どおりになっていません（実際の author_id: {note.get('author_id')}）。"
            "ログイン中のユーザーに他人名義で投稿する権限が無い可能性があります。"
        )
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
    "list_ticket_fields": tool_list_ticket_fields,
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
    "add_internal_note":  tool_add_internal_note,
    "compare_draft_replies": tool_compare_draft_replies,
}

# ENABLE_SENSITIVE_WRITES が OFF のとき tools/list から隠すツール
_SENSITIVE_TOOLS = {"add_internal_note"}


def _visible_tools() -> list:
    if ENABLE_SENSITIVE_WRITES:
        return TOOLS
    return [t for t in TOOLS if t["name"] not in _SENSITIVE_TOOLS]


# ── MCP stdio ループ ──────────────────────────────────────

def handle(body: dict) -> dict | None:
    method = body.get("method", "")
    req_id = body.get("id")

    def ok(result):
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code, msg):
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": msg}}

    if method == "initialize":
        config_error = _config_error()
        if config_error:
            sys.stderr.write(f"[ERROR] {config_error}\n")
        return ok({
            "protocolVersion": "2024-11-05",
            "capabilities":    {"tools": {}},
            "serverInfo":      {"name": "zendesk-mcp", "version": "2.0.0"},
        })

    # JSON-RPC の通知 (id を持たないメッセージ) には応答してはならない。
    # notifications/initialized 以外にも notifications/cancelled 等が届くため、
    # id の有無で判定する。応答すると id: null のエラーとなりクライアントが検証エラーを出す。
    if "id" not in body:
        return None

    if method == "ping":
        return ok({})

    if method == "tools/list":
        return ok({"tools": _visible_tools()})

    if method == "tools/call":
        name = body["params"]["name"]
        args = body["params"].get("arguments", {})
        if name not in TOOL_HANDLERS:
            return err(-32601, f"Unknown tool: {name}")
        config_error = _config_error()
        if config_error:
            # 設定が空のまま進むと「https://.zendesk.com/...」のような開けない認可 URL を出してしまう。
            # 利用者に直し方を見せたいので、JSON-RPC エラーではなくツールのエラー結果として返す。
            return ok({"content": [{"type": "text", "text": config_error}], "isError": True})
        try:
            result = TOOL_HANDLERS[name](args)
            if MASK_PII and name not in _MASK_EXEMPT_TOOLS:
                result = mask_pii_text(result)
            if not MASK_PII:
                result = _MASK_OFF_NOTICE + result
            return ok({"content": [{"type": "text", "text": result}]})
        except AuthRequired as e:
            # 利用者に認可 URL を見せたいので、JSON-RPC エラーではなくツールのエラー結果として返す
            return ok({"content": [{"type": "text", "text": str(e)}], "isError": True})
        except Exception as e:
            msg = str(e)
            if MASK_PII:
                msg = mask_pii_text(msg)
            return err(-32603, msg)

    return err(-32601, f"Method not found: {method}")


_SUBDOMAIN_RE = re.compile(r"[a-z0-9][a-z0-9-]*")


def _config_error() -> str:
    """必須の設定が足りない・不正なときに、利用者向けの直し方を返す。問題がなければ空文字。"""
    missing = []
    if not ZENDESK_SUBDOMAIN:
        missing.append("サブドメイン（Zendesk Subdomain）")
    if not ZENDESK_OAUTH_CLIENT_ID:
        missing.append("OAuth クライアント ID（Zendesk OAuth Client ID）")
    if missing:
        problem = f"{'、'.join(missing)}が空です。"
    elif not _SUBDOMAIN_RE.fullmatch(ZENDESK_SUBDOMAIN):
        problem = (f"サブドメイン「{ZENDESK_SUBDOMAIN}」の形式が正しくありません"
                   "（https://yourcompany.zendesk.com の yourcompany 部分を入れてください）。")
    else:
        return ""
    return (
        f"Zendesk MCP Server の設定が足りません: {problem}\n"
        "Claude Desktop の「設定 → 拡張機能 → Zendesk MCP Server」で入力し、"
        "Claude Desktop を再起動してください。\n"
        "（拡張機能以外から起動している場合は、環境変数 ZENDESK_SUBDOMAIN / "
        "ZENDESK_OAUTH_CLIENT_ID を設定してください）"
    )


def _force_utf8_stdio() -> None:
    """stdio を UTF-8 に固定する。

    Windows の Python 3.12/3.13 はパイプ接続の stdio を ANSI コードページ (日本語版は cp932、
    英語版は cp1252) で読み書きする。MCP クライアントは UTF-8 前提のため、日本語を含む
    応答が壊れた JSON になる / cp932 で表せない文字 (絵文字等) で UnicodeEncodeError になる。
    stdout は改行を \n に固定し、Windows のテキストモードによる \r\n 変換も避ける。
    """
    for stream, kwargs in (
        (sys.stdin,  {"encoding": "utf-8"}),
        (sys.stdout, {"encoding": "utf-8", "newline": "\n"}),
        (sys.stderr, {"encoding": "utf-8", "errors": "backslashreplace"}),
    ):
        try:
            stream.reconfigure(**kwargs)
        except (AttributeError, ValueError):
            pass


def main():
    _force_utf8_stdio()
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