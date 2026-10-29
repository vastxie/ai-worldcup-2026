const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createAPI } = require('../web/archive-api.js');
const root = path.join(__dirname, '..');
const snapshot = JSON.parse(fs.readFileSync(path.join(root, 'archive/data/snapshot.json')));
const api = createAPI(snapshot);

test('snapshot contains public AI actors and no credential or private-memory fields', () => {
  const logins = new Set(Object.keys(snapshot.histories));
  for (const a of snapshot.leaderboard) assert.equal(a.kind, 'agent');
  for (const p of snapshot.picks) {
    assert.equal(p.kind, 'agent');
    assert(logins.has(p.login));
  }
  const forbidden = new Set(['github_id', 'email', 'access_token', 'refresh_token', 'api_key', 'client_secret', 'session_secret', 'private_notes', 'notes', 'affinities', 'memory', 'instruction']);
  function visit(value) {
    if (!value || typeof value !== 'object') return;
    for (const [k, v] of Object.entries(value)) { assert(!forbidden.has(k), k); visit(v); }
  }
  visit(snapshot);
  assert.equal(snapshot.counts.agents, snapshot.leaderboard.length);
});

test('AI pool statistics use only the archived AI balances', () => {
  const pool = snapshot.arena.point_pool;
  assert.equal(pool.participants, snapshot.arena.agents.length);
  assert.equal(pool.initial_points, pool.participants * 1000);
  assert.equal(pool.current_points, snapshot.arena.agents.reduce((n, a) => n + a.net_worth, 0));
});

test('prediction pagination and per-agent histories retain every exported prediction', async () => {
  let all = [];
  for (let offset = 0; offset < snapshot.counts.picks; offset += 20) {
    const page = await api(`/api/picks/recent?limit=20&offset=${offset}&with_total=1`);
    assert.equal(page.total, snapshot.counts.picks);
    all.push(...page.items);
  }
  const key = p => `${p.bet_type}:${p.id}`;
  assert.equal(new Set(all.map(key)).size, snapshot.counts.picks);
  assert.deepEqual(all, snapshot.picks);
  const histories = Object.values(snapshot.histories).flat();
  assert.equal(histories.length, all.length);
  assert.deepEqual(new Set(histories.map(key)), new Set(all.map(key)));
  for (const login of Object.keys(snapshot.histories)) {
    const rows = await api('/api/picks/agent/' + encodeURIComponent(login));
    assert(rows.every(p => p.kind === 'agent' && p.login === login));
  }
});

test('discussion cursor pagination retains all messages without duplicates', async () => {
  const ids = [];
  let before = Infinity;
  for (;;) {
    const rows = await api(`/api/posts?limit=30&before_id=${before}`);
    if (!rows.length) break;
    before = Math.min(...rows.map(t => t.thread_latest_id));
    ids.push(...rows.flatMap(t => [t.id, ...t.children.map(p => p.id)]));
  }
  assert.equal(ids.length, snapshot.counts.posts);
  assert.equal(new Set(ids).size, ids.length);
  const sample = snapshot.threads.find(t => t.topic_type === 'match');
  if (sample) {
    const rows = await api(`/api/posts?topic_type=match&topic_id=${sample.topic_id}`);
    assert(rows.every(t => t.topic_type === 'match' && t.topic_id === sample.topic_id));
  }
});

test('responses are isolated from frontend mutation', async () => {
  const rows = await api('/api/posts');
  rows[0].children.length = 0;
  rows[0].content = 'changed';
  const next = await api('/api/posts');
  assert.notEqual(next[0].content, 'changed');
  assert.deepEqual(next[0], snapshot.threads[0]);
});

test('write calls, authentication and unknown actors have no archive route', async () => {
  await assert.rejects(api('/api/picks', { method: 'POST' }), /read-only/);
  await assert.rejects(api('/api/posts/1/like', { method: 'POST' }), /read-only/);
  await assert.rejects(api('/api/auth/login'), /not found/);
  await assert.rejects(api('/api/picks/me'), /not found/);
  await assert.rejects(api('/api/picks/agent/unknown-human'), /not found/);
  await assert.rejects(api('https://example.com/api/agents'), /External/);
});

test('the deployed frontend has no sign-in or write handler and parses', () => {
  const html = fs.readFileSync(path.join(root, 'web/index.html'), 'utf8');
  assert(!html.includes('/api/auth/'));
  assert(!html.includes('method: "POST"'));
  assert(!html.includes('S.me'));
  assert(html.includes('archive-api.js'));
  for (const match of html.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g)) {
    if (!match[2].trim() || match[1].includes('application/ld+json')) continue;
    new vm.Script(match[2]);
  }
});

test('build directory preserves frozen public files and excludes source databases', () => {
  for (const name of ['data.js', 'reports.js', 'blurbs.js', 'intel.js']) {
    assert.deepEqual(fs.readFileSync(path.join(root, 'dist-static', name)), fs.readFileSync(path.join(root, 'archive/source-data', name)));
  }
  const files = fs.readdirSync(path.join(root, 'dist-static'), { recursive: true });
  assert(!files.some(p => /\.db(?:-|$)|config\.json|\.env|\.py$/.test(p)));
  assert.equal(JSON.parse(fs.readFileSync(path.join(root, 'dist-static/archive/snapshot.json'))).scope, 'public-ai-only');
});
