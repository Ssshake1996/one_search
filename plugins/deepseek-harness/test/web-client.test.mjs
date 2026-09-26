import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const source = await readFile(new URL('../client.js', import.meta.url), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
function load(React = {}, overrides = {}) {
  let plugin, id;
  const document = { visibilityState: 'visible', addEventListener() {}, removeEventListener() {} };
  const window = { addEventListener() {}, removeEventListener() {}, __ModuleLoader__: { load(module) { id = module.id; plugin = module.factory(name => { assert.equal(name, 'react'); return React; }); } } };
  vm.runInNewContext(source, { window, document, setTimeout, clearTimeout, console, ...overrides });
  return { plugin, id, document, window };
}
function fakeClock() {
  let next = 0;
  const timers = new Map(), listeners = new Map();
  const doc = { visibilityState: 'visible', addEventListener: (event, handler) => listeners.set(event, handler), removeEventListener: event => listeners.delete(event) };
  return { doc, timers, listeners,
    setTimer: (callback, delay) => { timers.set(++next, { callback, delay }); return next; },
    clearTimer: id => timers.delete(id),
    fire() { const [id, timer] = timers.entries().next().value; timers.delete(id); timer.callback(); },
    visible(value) { doc.visibilityState = value ? 'visible' : 'hidden'; listeners.get('visibilitychange')?.(); },
  };
}

test('official module registers one_search sidebar and matching main panel through shared React only', () => {
  const { plugin, id } = load();
  assert.equal(id, 'one-search-bundle');
  assert.deepEqual(Array.from(plugin.inject), ['slots', 'connection']);
  const registrations = [];
  plugin.apply({ connection: { rpc: { call() {} } }, slots: { inject(name, fn) { fn(); }, register(options, component) { registrations.push({ options, component }); } } });
  assert.deepEqual(registrations.map(x => x.options.name), ['sidebar.panellist', 'main']);
  assert.equal(registrations[0].options.id, registrations[1].options.key);
  assert.equal(registrations[0].options.label, 'one_search');
});

test('transport and application failure are both errors, including optimistic revision conflict', () => {
  const { unwrap } = load().plugin.__testing;
  assert.equal(unwrap({ ok: true, value: { ok: true, result: 42 } }), 42);
  assert.throws(() => unwrap({ ok: false, error: { code: 'disconnected', message: 'offline' } }), e => e.code === 'disconnected' && e.message === 'offline');
  assert.throws(() => unwrap({ ok: true, value: { ok: false, error: { code: 'revision_conflict', message: 'changed' } } }), e => e.code === 'revision_conflict');
  assert.throws(() => unwrap({ ok: true }), /操作未完成/);
});

test('poller has no overlapping calls, pauses when hidden, refreshes on return and disposes listeners', async () => {
  const { createPoller } = load().plugin.__testing;
  const clock = fakeClock(), values = [];
  let calls = 0, fulfill;
  const poller = createPoller({ document: clock.doc, setTimer: clock.setTimer, clearTimer: clock.clearTimer,
    request: () => { calls++; return new Promise(resolve => { fulfill = resolve; }); }, onValue: value => values.push(value), onError: error => { throw error; } });
  assert.equal(calls, 1);
  poller.refresh(); poller.refresh();
  assert.equal(calls, 1);
  clock.visible(false); fulfill('first'); await tick();
  assert.deepEqual(values, ['first']); assert.equal(clock.timers.size, 0);
  clock.visible(true); assert.equal(calls, 2);
  fulfill('second'); await tick();
  assert.equal(clock.timers.values().next().value.delay, 2000);
  poller.dispose();
  assert.equal(clock.timers.size, 0); assert.equal(clock.listeners.size, 0);
  poller.refresh(); assert.equal(calls, 2);
});

test('poller backs off bounded failures and ignores an in-flight reply after unmount', async () => {
  const { createPoller } = load().plugin.__testing;
  const clock = fakeClock(), delays = [], values = [];
  let shouldFail = true, finish;
  const poller = createPoller({ document: clock.doc, setTimer: clock.setTimer, clearTimer: clock.clearTimer,
    request: () => shouldFail ? Promise.reject(new Error('offline')) : new Promise(resolve => { finish = resolve; }),
    onValue: value => values.push(value), onError: (error, delay) => delays.push(delay) });
  for (let i = 0; i < 5; i++) { await tick(); clock.fire(); }
  await tick();
  assert.deepEqual(delays, [4000, 8000, 16000, 30000, 30000, 30000]);
  shouldFail = false; clock.fire(); poller.dispose(); finish('late'); await tick();
  assert.deepEqual(values, []); assert.equal(clock.timers.size, 0);
});

test('initially hidden panel makes no request', async () => {
  const { createPoller } = load().plugin.__testing;
  const clock = fakeClock(); clock.doc.visibilityState = 'hidden'; let calls = 0;
  const poller = createPoller({ document: clock.doc, setTimer: clock.setTimer, clearTimer: clock.clearTimer, request: async () => ++calls, onValue() {}, onError() {} });
  await tick(); assert.equal(calls, 0); assert.equal(clock.timers.size, 0);
  clock.visible(true); await tick(); assert.equal(calls, 1); poller.dispose();
});

test('draft values and baseline are isolated from the server response', () => {
  const { freshDraft } = load().plugin.__testing;
  const snapshot = { revision: 'a', values: { roots: ['D:/data'], indexing: { content_roots: [] } } };
  const draft = freshDraft(snapshot); draft.values.roots.push('D:/new');
  assert.deepEqual(snapshot.values.roots, ['D:/data']);
  assert.deepEqual(Array.from(draft.original.roots), ['D:/data']);
  assert.equal(draft.revision, 'a');
});

test('discovery suggestions do not grant fields, and index selections require explicit key and watermark confirmation', () => {
  const { selectionFor, packSelections } = load().plugin.__testing;
  const state = selectionFor({ table: 'notes', index_recommendation: { id_column: 'id', text_columns: ['body'] } }, {});
  assert.equal(state.enabled, false); assert.equal(state.columns.length, 0); assert.equal(state.index_text_columns.length, 0);
  state.enabled = true;
  assert.throws(() => packSelections({ notes: state }), /至少一个/);
  state.columns = ['body']; state.index_text_columns = ['body'];
  assert.throws(() => packSelections({ notes: state }), /唯一键/);
  state.columns = ['id', 'body', 'modified']; state.updated_column = 'modified';
  assert.throws(() => packSelections({ notes: state }), /水位/);
  state.watermark_confirmed = true;
  const result = packSelections({ notes: state });
  assert.equal(result[0].updated_column, 'modified');
  assert.equal(result[0].watermark_confirmed, undefined);
  assert.deepEqual(Array.from(result[0].columns), ['id','body','modified']);
});

test('connection preparation strips incompatible auth and validates ports without mutating edited fields', () => {
  const { connectionSource } = load().plugin.__testing;
  const original = { id: ' notes ', kind: 'sqlite', path: 'D:/notes.db', credential_ref: 'secret-ref', password_env: 'PW', host: 'old' };
  const result = connectionSource(original);
  assert.equal(result.id, 'notes'); assert.equal(result.credential_ref, undefined); assert.equal(result.password_env, undefined);
  assert.equal(original.credential_ref, 'secret-ref');
  assert.throws(() => connectionSource({ id: 'pg', kind: 'postgres', host: 'localhost', database: 'db', user: 'reader', port: 'oops' }), /端口/);
  const pg = connectionSource({ id: 'pg', kind: 'postgres', host: 'localhost', database: 'db', user: 'reader', ssl: { sslmode: '', sslrootcert: '' } });
  assert.equal(pg.port, 5432); assert.equal(pg.ssl, undefined);
});

function element(type, props, ...children) { return { type, props: props || {}, children: children.flat(Infinity) }; }
function textOf(tree) {
  if (tree === null || tree === undefined || typeof tree === 'boolean') return '';
  if (typeof tree !== 'object') return String(tree);
  return (tree.children || []).map(textOf).join(' ');
}
function all(tree, predicate) { if (!tree || typeof tree !== 'object') return []; return [...(predicate(tree) ? [tree] : []), ...(tree.children || []).flatMap(node => all(node, predicate))]; }
test('overview distinguishes unknown discovery total, stale/error states and queue counts without a fabricated percentage', () => {
  const React = { createElement: element, Fragment: 'fragment', useState: initial => [initial, () => {}] };
  const { Overview } = load(React).plugin.__testing;
  const status = { index: { semantic: { lifecycle: { state: 'missing', ready: false } }, progress: {
    overall: { state: 'waiting', reason: 'model_not_ready' }, known_unique_files: 230,
    discovery: { active: true, complete: false, total: null, queued_directories: 5, roots: [] },
    content: { pending: 10, retry_waiting: 2, queued_events: 3, counts: { ready: 100 } },
    semantic: { enabled: true, embedded: 40, eligible: 200, model_state: 'missing' },
    resources: { rss_mb: 55 }, runtime_policy: {}, error_summary: { source_errors: {}, scan_errors: { count: 0 } }, databases: { sources: {} },
  } } };
  const tree = Overview({ status, run() {}, busy: false });
  const text = textOf(tree);
  assert.match(text, /首次扫描总量未知/); assert.match(text, /等待模型就绪/);
  assert.doesNotMatch(text, /当前向量批次已处理/); assert.doesNotMatch(text, /需要关注/);
  assert.equal(all(tree, node => node.type === 'progress' || node.props.role === 'progressbar').length, 0);
  status.index.progress.error_summary.scan_errors.count = 2;
  assert.match(textOf(Overview({ status, run() {}, busy: false })), /需要关注/);
});

test('pause control submits a supported seconds parameter', async () => {
  const React = { createElement: element, Fragment: 'fragment', useState: initial => [initial, () => {}] };
  const { Overview } = load(React).plugin.__testing;
  const calls = [];
  const tree = Overview({ status: { index: { progress: { overall: {}, content: {}, semantic: {}, discovery: {}, databases: {}, error_summary: {} } } }, run: (...args) => calls.push(args), busy: false });
  const button = all(tree, node => textOf(node) === '暂停索引' && node.props.onClick)[0];
  button.props.onClick();
  assert.equal(calls[0][0], 'pause'); assert.equal(calls[0][1].seconds, 1800); assert.equal(calls[0][1].duration_seconds, undefined);
});

function hookHarness() {
  const state = [], effects = [], jobs = [];
  let position = 0;
  return {
    React: {
      createElement: element, Fragment: 'fragment',
      useState(initial) { const i = position++; if (!(i in state)) state[i] = initial; return [state[i], value => { state[i] = typeof value === 'function' ? value(state[i]) : value; }]; },
      useRef(initial) { const i = position++; if (!(i in state)) state[i] = { current: initial }; return state[i]; },
      useEffect(fn, dependencies) {
        const i = position++, old = effects[i];
        if (old && dependencies.every((value, index) => value === old.dependencies[index])) return;
        jobs.push(() => { old?.cleanup?.(); effects[i] = { dependencies, cleanup: fn() }; });
      },
    },
    render(component, props) { position = 0; const tree = component(props); while (jobs.length) jobs.shift()(); return tree; },
    dispose() { for (const effect of effects) effect?.cleanup?.(); },
  };
}
test('polling cannot overwrite dirty settings; pending saves disable forms, reject double saves, then show applied snapshot', async () => {
  const harness = hookHarness(), clock = fakeClock();
  const { Panel } = load(harness.React, { document: clock.doc, setTimeout: clock.setTimer, clearTimeout: clock.clearTimer }).plugin.__testing;
  const initial = { revision: 'first', values: { scope: 'directories', roots: ['D:/before'], indexing: {}, runtime_policy: {}, databases: [] }, resource: {} };
  let saves = 0, resolveSave;
  const request = async action => {
    if (action === 'settings_get') return initial;
    if (action === 'status') return { index: { progress: { sampled_at: new Date().toISOString() } } };
    if (action === 'settings_save') { saves++; return new Promise(resolve => { resolveSave = resolve; }); }
  };
  let tree = harness.render(Panel, { request }); await tick(); tree = harness.render(Panel, { request });
  let scope = all(tree, node => node.type?.name === 'Scope')[0];
  scope.props.update('roots', ['D:/edited']); tree = harness.render(Panel, { request });
  clock.fire(); await tick(); tree = harness.render(Panel, { request });
  scope = all(tree, node => node.type?.name === 'Scope')[0];
  assert.deepEqual(Array.from(scope.props.values.roots), ['D:/edited']);
  const saveButton = all(tree, node => textOf(node) === '保存并应用' && node.props.onClick)[0];
  const saving = saveButton.props.onClick(); saveButton.props.onClick();
  tree = harness.render(Panel, { request });
  assert.equal(all(tree, node => node.type === 'fieldset')[0].props.disabled, true);
  assert.equal(saves, 1);
  resolveSave({ ...initial, revision: 'second', values: { ...initial.values, roots: ['D:/edited'] }, applied: true });
  await saving; tree = harness.render(Panel, { request });
  assert.equal(all(tree, node => node.type === 'fieldset')[0].props.disabled, false);
  assert.match(textOf(tree), /已保存并应用/);
  harness.dispose();
});

test('revision conflict retains edited values and prevents blind overwrite', async () => {
  const harness = hookHarness(), clock = fakeClock();
  const { Panel } = load(harness.React, { document: clock.doc, setTimeout: clock.setTimer, clearTimeout: clock.clearTimer }).plugin.__testing;
  const request = async action => {
    if (action === 'settings_get') return { revision: 'old', values: { scope: 'directories', roots: ['D:/original'], indexing: {}, runtime_policy: {}, databases: [] }, resource: {} };
    if (action === 'status') return { index: {} };
    if (action === 'settings_save') { const error = new Error('revision changed'); error.code = 'revision_conflict'; throw error; }
  };
  harness.render(Panel, { request }); await tick(); let tree = harness.render(Panel, { request });
  all(tree, node => node.type?.name === 'Scope')[0].props.update('roots', ['D:/mine']);
  tree = harness.render(Panel, { request });
  await all(tree, node => textOf(node) === '保存并应用' && node.props.onClick)[0].props.onClick();
  tree = harness.render(Panel, { request });
  assert.deepEqual(Array.from(all(tree, node => node.type?.name === 'Scope')[0].props.values.roots), ['D:/mine']);
  assert.equal(all(tree, node => textOf(node) === '保存并应用' && node.props.onClick)[0].props.disabled, true);
  assert.match(textOf(tree), /你的编辑仍保留/);
  harness.dispose();
});

test('maintenance keeps edits, disables settings actions and recovers through status polling', async () => {
  const harness = hookHarness(), clock = fakeClock();
  const { Panel } = load(harness.React, { document: clock.doc, setTimeout: clock.setTimer, clearTimeout: clock.clearTimer }).plugin.__testing;
  let maintenance = false, saves = 0;
  const request = async action => {
    if (action === 'settings_get') return { revision: 'before-upgrade', values: { roots: ['D:/original'], indexing: {}, databases: [] } };
    if (action === 'status') return { service: { status: maintenance ? 'maintenance' : 'running' }, index: {} };
    if (action === 'settings_save') saves++;
  };
  harness.render(Panel, { request }); await tick(); let tree = harness.render(Panel, { request });
  all(tree, node => node.type?.name === 'Scope')[0].props.update('roots', ['D:/mine']);
  maintenance = true; clock.fire(); await tick(); tree = harness.render(Panel, { request });
  assert.match(textOf(tree), /正在升级或恢复 one_search/);
  assert.equal(all(tree, node => node.type === 'fieldset')[0].props.disabled, true);
  const save = all(tree, node => textOf(node) === '保存并应用' && node.props.onClick)[0];
  assert.equal(save.props.disabled, true); await save.props.onClick(); assert.equal(saves, 0);
  assert.deepEqual(Array.from(all(tree, node => node.type?.name === 'Scope')[0].props.values.roots), ['D:/mine']);
  maintenance = false; clock.fire(); await tick(); tree = harness.render(Panel, { request });
  assert.equal(all(tree, node => node.type === 'fieldset')[0].props.disabled, false);
  assert.deepEqual(Array.from(all(tree, node => node.type?.name === 'Scope')[0].props.values.roots), ['D:/mine']);
  harness.dispose();
});
