/*
 * Claude Reply Drafts — チケットサイドバーの UI。
 *
 * 流れ: ボタンで Claude Desktop を開く → Claude が zendesk_mcp_server の
 * add_internal_note (kind: draft) で下書きを社内メモに投稿 → ここに表示 → 返信欄に挿入。
 * Zendesk API はエージェント本人のセッションで呼ぶ (client.request)。トークンは持たない。
 */
(function () {
  'use strict';

  var Core = window.ClaudeDraftsCore;
  var STORAGE_KEY = 'claude-reply-drafts:project_url';
  var POLL_INTERVAL_MS = 10000;
  var POLL_LIMIT_MS = 10 * 60 * 1000;

  var MESSAGES = {
    ja: {
      launch: 'Claude で下書きを作る',
      launchProject: 'Claude プロジェクトで下書きを作る',
      hintNew: 'Claude Desktop が依頼文入りで開きます。送信すると、下書きがこのチケットの社内メモに届きます。',
      hintProject: 'プロジェクトが開き、依頼文がクリップボードにコピーされます。入力欄に貼り付けて送信してください。',
      statusNew: 'Claude Desktop で送信してください。下書きが届いたらここに表示します。',
      statusProject: '依頼文をコピーしました。プロジェクトの入力欄に貼り付け（⌘V / Ctrl+V）て送信してください。下書きが届いたらここに表示します。',
      statusProjectNoCopy: '依頼文を自動でコピーできませんでした。下の文をコピーし、プロジェクトの入力欄に貼り付けて送信してください。',
      statusArrived: '新しい下書きが届きました。',
      statusTimeout: 'まだ下書きが届いていません。届いたら「更新」で表示できます。',
      copy: '依頼文をコピー',
      copied: '依頼文をコピーしました。',
      copyFailed: 'コピーできませんでした。下の文を選択してコピーしてください。',
      draftsTitle: '返信下書き',
      refresh: '更新',
      empty: 'まだ下書きはありません。',
      loadError: '下書きを読み込めませんでした: ',
      latest: '最新の下書き',
      insert: '返信欄に入れる',
      inserted: '返信欄に入れました。内容を確認してから送信してください。',
      insertError: '返信欄に入れられませんでした: ',
      more: '以前の下書きが {n} 件あります。',
      settings: '設定',
      projectLabel: '自分の Claude プロジェクトの URL',
      sourceMine: '自分の設定を使っています。',
      sourceAdmin: '管理者が設定したプロジェクトを使っています。',
      sourceNone: 'プロジェクトは未設定です（新しいチャットで開きます）。',
      invalidProject: 'https://claude.ai/project/... の形の URL を入れてください。',
      save: '保存',
      clear: '自分の設定を消す',
    },
    en: {
      launch: 'Draft with Claude',
      launchProject: 'Draft in Claude project',
      hintNew: 'Claude Desktop opens with the request filled in. Once you send it, the draft arrives as an internal note on this ticket.',
      hintProject: 'The project opens and the request is copied to the clipboard. Paste it into the input and send.',
      statusNew: 'Send the request in Claude Desktop. The draft will appear here.',
      statusProject: 'Request copied. Paste it into the project input (Cmd+V / Ctrl+V) and send. The draft will appear here.',
      statusProjectNoCopy: 'Could not copy the request automatically. Copy the text below, paste it into the project input, and send.',
      statusArrived: 'A new draft has arrived.',
      statusTimeout: 'No draft yet. Use "Refresh" once it arrives.',
      copy: 'Copy request',
      copied: 'Request copied.',
      copyFailed: 'Could not copy. Select the text below and copy it.',
      draftsTitle: 'Reply drafts',
      refresh: 'Refresh',
      empty: 'No drafts yet.',
      loadError: 'Could not load drafts: ',
      latest: 'Latest draft',
      insert: 'Insert into reply',
      inserted: 'Inserted into the reply. Review it before sending.',
      insertError: 'Could not insert: ',
      more: '{n} earlier draft(s).',
      settings: 'Settings',
      projectLabel: 'My Claude project URL',
      sourceMine: 'Using your own setting.',
      sourceAdmin: 'Using the project set by your admin.',
      sourceNone: 'No project set (opens a new chat).',
      invalidProject: 'Enter a URL like https://claude.ai/project/... .',
      save: 'Save',
      clear: 'Clear my setting',
    },
  };

  var client = ZAFClient.init();
  var $ = function (id) { return document.getElementById(id); };
  var state = { t: MESSAGES.ja, settings: {}, ticketId: null, drafts: [], waitingSince: null, pollTimer: null };

  function show(el, on) { el.hidden = !on; }

  function readStorage() {
    try { return window.localStorage.getItem(STORAGE_KEY) || ''; } catch (e) { return ''; }
  }
  function writeStorage(value) {
    try {
      if (value) window.localStorage.setItem(STORAGE_KEY, value);
      else window.localStorage.removeItem(STORAGE_KEY);
    } catch (e) { /* 保存できない環境では管理者の設定だけを使う */ }
  }

  function projectSetting() {
    var mine = readStorage();
    if (mine) return { url: mine, source: 'mine' };
    var admin = state.settings.claude_project_url || '';
    return admin ? { url: admin, source: 'admin' } : { url: '', source: 'none' };
  }

  function prompt() {
    return Core.buildPrompt(state.settings.prompt_template, state.ticketId);
  }

  function marker() {
    return state.settings.draft_marker || Core.DEFAULT_MARKER;
  }

  function setStatus(text) {
    $('status').textContent = text || '';
    show($('status'), !!text);
  }

  // クリップボードへのコピー。iframe では navigator.clipboard が許可されないことがあるので、
  // クリックの処理中に同期で動く execCommand を先に試す (この後のリンク遷移を妨げないため)。
  function copyText(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    if (!ok && navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { show($('manual-copy'), false); }, function () {});
    }
    return ok;
  }

  function showManualCopy(text) {
    $('manual-copy-label').textContent = state.t.copyFailed;
    $('manual-copy-text').value = text;
    show($('manual-copy'), true);
    $('manual-copy-text').select();
  }

  function renderLaunch() {
    var t = state.t;
    var launch = Core.buildLaunch(prompt(), projectSetting().url);
    $('launch').href = launch.url;
    $('launch').textContent = launch.copy ? t.launchProject : t.launch;
    $('launch-hint').textContent = launch.copy ? t.hintProject : t.hintNew;
  }

  function renderSettings() {
    var t = state.t;
    var p = projectSetting();
    $('settings-title').textContent = t.settings;
    $('project-label').textContent = t.projectLabel;
    $('project-url').value = readStorage();
    $('project-source').textContent = p.source === 'mine' ? t.sourceMine : p.source === 'admin' ? t.sourceAdmin : t.sourceNone;
    $('project-save').textContent = t.save;
    $('project-clear').textContent = t.clear;
    show($('project-clear'), !!readStorage());
  }

  function formatTime(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleString(state.t === MESSAGES.ja ? 'ja-JP' : 'en-US', {
      month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  }

  function renderDrafts() {
    var t = state.t;
    var drafts = state.drafts;
    show($('drafts-empty'), drafts.length === 0);
    show($('draft'), drafts.length > 0);
    if (!drafts.length) return;
    $('draft-meta').textContent = t.latest + '（' + formatTime(drafts[0].createdAt) + '）';
    $('draft-text').textContent = drafts[0].text;
    show($('draft-more'), drafts.length > 1);
    $('draft-more').textContent = t.more.replace('{n}', String(drafts.length - 1));
  }

  function loadDrafts() {
    show($('drafts-error'), false);
    return client.request({
      url: '/api/v2/tickets/' + encodeURIComponent(state.ticketId) + '/comments.json?sort_order=desc&per_page=100',
      type: 'GET',
    }).then(function (data) {
      state.drafts = Core.findDrafts(data.comments, marker());
      renderDrafts();
      if (state.waitingSince && Core.hasDraftSince(state.drafts, state.waitingSince)) {
        stopWaiting();
        setStatus(state.t.statusArrived);
      }
    }, function (err) {
      $('drafts-error').textContent = state.t.loadError + ((err && (err.statusText || err.status)) || 'error');
      show($('drafts-error'), true);
    });
  }

  function stopWaiting() {
    state.waitingSince = null;
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = null;
  }

  function startWaiting() {
    stopWaiting();
    state.waitingSince = new Date();
    var started = Date.now();
    state.pollTimer = setInterval(function () {
      if (Date.now() - started > POLL_LIMIT_MS) {
        stopWaiting();
        setStatus(state.t.statusTimeout);
        return;
      }
      loadDrafts();
    }, POLL_INTERVAL_MS);
  }

  function onLaunchClick() {
    // preventDefault しない: このクリックでそのまま claude:// のリンクを開く
    var launch = Core.buildLaunch(prompt(), projectSetting().url);
    show($('manual-copy'), false);
    if (launch.copy) {
      if (copyText(prompt())) setStatus(state.t.statusProject);
      else { setStatus(state.t.statusProjectNoCopy); showManualCopy(prompt()); }
    } else {
      setStatus(state.t.statusNew);
    }
    startWaiting();
  }

  function onCopyClick() {
    show($('manual-copy'), false);
    if (copyText(prompt())) setStatus(state.t.copied);
    else showManualCopy(prompt());
  }

  function onInsertClick() {
    var draft = state.drafts[0];
    if (!draft) return;
    client.set('ticket.comment.type', 'publicReply')
      .then(function () { return client.invoke('ticket.comment.appendText', draft.text); })
      .then(function () { setStatus(state.t.inserted); },
            function (err) { setStatus(state.t.insertError + ((err && err.message) || 'error')); });
  }

  function onProjectSave() {
    var value = $('project-url').value.trim();
    if (value && !Core.parseProjectId(value)) {
      $('project-error').textContent = state.t.invalidProject;
      show($('project-error'), true);
      return;
    }
    show($('project-error'), false);
    writeStorage(value);
    renderSettings();
    renderLaunch();
  }

  function onProjectClear() {
    writeStorage('');
    show($('project-error'), false);
    renderSettings();
    renderLaunch();
  }

  function renderStatic() {
    var t = state.t;
    $('copy').textContent = t.copy;
    $('drafts-title').textContent = t.draftsTitle;
    $('refresh').textContent = t.refresh;
    $('drafts-empty').textContent = t.empty;
    $('insert').textContent = t.insert;
  }

  function autoResize() {
    // documentElement.scrollHeight は iframe の現在の高さを下回らないため縮まない。中身の高さで測る。
    var app = $('app');
    var last = 0;
    var resize = function () {
      var h = Math.ceil(app.getBoundingClientRect().height) + 12;
      if (h === last) return;
      last = h;
      client.invoke('resize', { width: '100%', height: h + 'px' });
    };
    if (window.ResizeObserver) new ResizeObserver(resize).observe(app);
    resize();
  }

  function init() {
    Promise.all([client.metadata(), client.get(['ticket.id', 'currentUser.locale'])]).then(function (res) {
      state.settings = res[0].settings || {};
      state.ticketId = res[1]['ticket.id'];
      var locale = String(res[1]['currentUser.locale'] || 'ja');
      state.t = locale.indexOf('ja') === 0 ? MESSAGES.ja : MESSAGES.en;
      document.documentElement.lang = state.t === MESSAGES.ja ? 'ja' : 'en';

      renderStatic();
      renderLaunch();
      renderSettings();
      renderDrafts();
      autoResize();
      loadDrafts();

      $('launch').addEventListener('click', onLaunchClick);
      $('copy').addEventListener('click', onCopyClick);
      $('refresh').addEventListener('click', loadDrafts);
      $('insert').addEventListener('click', onInsertClick);
      $('project-save').addEventListener('click', onProjectSave);
      $('project-clear').addEventListener('click', onProjectClear);
      client.on('app.activated', loadDrafts);
    });
  }

  init();
})();
