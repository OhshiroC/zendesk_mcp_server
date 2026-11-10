// Zendesk アプリ (zendesk_app/) の純粋な処理のテスト。実行: node --test tests/js/*.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const Core = require('../../zendesk_app/assets/core.js');

const PID = '01234567-89ab-cdef-0123-456789abcdef';

test('buildPrompt はチケット番号だけを埋める', () => {
  assert.equal(Core.buildPrompt('チケット #{{ticket_id}} / {{ticket_id}}', 42), 'チケット #42 / 42');
  assert.equal(Core.buildPrompt('', 1), '');
});

test('parseProjectId は URL・claude:// ・ID のみを受け付ける', () => {
  assert.equal(Core.parseProjectId(`https://claude.ai/project/${PID}`), PID);
  assert.equal(Core.parseProjectId(`https://claude.ai/project/${PID}?x=1`), PID);
  assert.equal(Core.parseProjectId(`claude://claude.ai/project/${PID.toUpperCase()}`), PID);
  assert.equal(Core.parseProjectId(`  ${PID} `), PID);
  assert.equal(Core.parseProjectId('https://claude.ai/project/not-a-uuid'), null);
  assert.equal(Core.parseProjectId(`https://claude.ai/project/${PID}xyz`), null);
  assert.equal(Core.parseProjectId(''), null);
  assert.equal(Core.parseProjectId(null), null);
});

test('buildLaunch: プロジェクトなしは依頼文入りの新しいチャット', () => {
  const l = Core.buildLaunch('返信 #1 & 下書き', '');
  assert.equal(l.copy, false);
  assert.equal(l.url, 'claude://claude.ai/new?q=' + encodeURIComponent('返信 #1 & 下書き'));
  assert.equal(decodeURIComponent(l.url.split('?q=')[1]), '返信 #1 & 下書き');
});

test('buildLaunch: プロジェクトありはプロジェクトを開いてコピー', () => {
  const l = Core.buildLaunch('依頼', `https://claude.ai/project/${PID}`);
  assert.deepEqual(l, { url: `claude://claude.ai/project/${PID}`, copy: true, projectId: PID });
});

test('buildLaunch: 不正なプロジェクト URL は新しいチャットにフォールバック', () => {
  assert.equal(Core.buildLaunch('依頼', 'https://example.com').copy, false);
});

const M = '【返信下書き】';
const c = (id, at, pub, body) => ({ id, created_at: at, public: pub, author_id: 9, plain_body: body });

test('findDrafts は目印で始まる社内メモだけを新しい順に返す', () => {
  const drafts = Core.findDrafts([
    c(1, '2026-11-10T01:00:00Z', false, `${M}\n古い下書き`),
    c(2, '2026-11-10T02:00:00Z', false, 'ただのメモ'),
    c(3, '2026-11-10T03:00:00Z', true, `${M}\n公開コメントは対象外`),
    c(4, '2026-11-10T04:00:00Z', false, `  ${M}\n新しい下書き\n\n2行目\n`),
    c(5, '2026-11-10T05:00:00Z', false, `本文の途中の${M}は対象外`),
  ], M);
  assert.deepEqual(drafts.map(d => d.id), [4, 1]);
  assert.equal(drafts[0].text, '新しい下書き\n\n2行目');
  assert.equal(drafts[1].text, '古い下書き');
});

test('findDrafts は目印を変えられ、body にフォールバックする', () => {
  const drafts = Core.findDrafts([{ id: 1, created_at: 'x', public: false, body: '[DRAFT] hi' }], '[DRAFT]');
  assert.equal(drafts[0].text, 'hi');
  assert.deepEqual(Core.findDrafts(undefined, M), []);
});

test('hasDraftSince', () => {
  const drafts = [{ createdAt: '2026-11-10T03:00:00Z' }];
  assert.equal(Core.hasDraftSince(drafts, '2026-11-10T02:59:59Z'), true);
  assert.equal(Core.hasDraftSince(drafts, new Date('2026-11-10T03:00:00Z')), false);
  assert.equal(Core.hasDraftSince([], new Date()), false);
});
