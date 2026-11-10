/*
 * Claude Reply Drafts — Zendesk に依存しない純粋な処理。
 * ブラウザでは window.ClaudeDraftsCore、Node (テスト) では module.exports として使う。
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.ClaudeDraftsCore = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var DEFAULT_MARKER = '【返信下書き】';
  var UUID_RE = /([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i;

  /** 依頼文のテンプレートにチケット番号を埋める。チケットの中身は入れない (MCP 側のマスクを迂回させないため)。 */
  function buildPrompt(template, ticketId) {
    return String(template || '').split('{{ticket_id}}').join(String(ticketId));
  }

  /** https://claude.ai/project/<id>、claude://claude.ai/project/<id>、id だけ のどれからでもプロジェクト ID を取り出す。 */
  function parseProjectId(value) {
    var text = String(value || '').trim();
    if (!text) return null;
    var m = text.match(/project\/([^/?#\s]+)/i);
    var candidate = m ? m[1] : text;
    var u = candidate.match(UUID_RE);
    return u && u[1].length === candidate.length ? u[1].toLowerCase() : null;
  }

  /**
   * Claude Desktop を開くリンクを作る。
   * プロジェクトのリンクでは入力欄に依頼文を入れられない (?q= が効かない) ため、
   * プロジェクト指定時は依頼文をクリップボードにコピーして貼ってもらう (copy: true)。
   */
  function buildLaunch(prompt, projectUrl) {
    var projectId = parseProjectId(projectUrl);
    if (projectId) {
      return { url: 'claude://claude.ai/project/' + projectId, copy: true, projectId: projectId };
    }
    return { url: 'claude://claude.ai/new?q=' + encodeURIComponent(prompt), copy: false, projectId: null };
  }

  function stripMarker(body, marker) {
    var text = String(body || '').replace(/^\s+/, '');
    if (text.indexOf(marker) === 0) text = text.slice(marker.length);
    return text.replace(/^\s+/, '').replace(/\s+$/, '');
  }

  /** コメント一覧から返信下書き (目印で始まる社内メモ) を新しい順に返す。 */
  function findDrafts(comments, marker) {
    var mk = marker || DEFAULT_MARKER;
    return (comments || [])
      .filter(function (c) {
        if (c.public) return false;
        var body = String(c.plain_body || c.body || '').replace(/^\s+/, '');
        return body.indexOf(mk) === 0;
      })
      .map(function (c) {
        return {
          id: c.id,
          createdAt: c.created_at,
          authorId: c.author_id,
          text: stripMarker(c.plain_body || c.body, mk),
        };
      })
      .sort(function (a, b) {
        return a.createdAt < b.createdAt ? 1 : a.createdAt > b.createdAt ? -1 : 0;
      });
  }

  /** since (Date または ISO 文字列) より後に作られた下書きがあるか。 */
  function hasDraftSince(drafts, since) {
    var t = new Date(since).getTime();
    return (drafts || []).some(function (d) {
      return new Date(d.createdAt).getTime() > t;
    });
  }

  return {
    DEFAULT_MARKER: DEFAULT_MARKER,
    buildPrompt: buildPrompt,
    parseProjectId: parseProjectId,
    buildLaunch: buildLaunch,
    stripMarker: stripMarker,
    findDrafts: findDrafts,
    hasDraftSince: hasDraftSince,
  };
});
