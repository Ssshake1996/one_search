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
  const details = { action: '检查后台运行状态。' };
  assert.throws(() => unwrap({ ok: false, error: { code: 'connection_error', details } }), e => e.details === details);
  assert.throws(() => unwrap({ ok: true, value: { ok: false, error: { code: 'service_connection_refused', details } } }), e => e.details === details);
  assert.throws(() => unwrap({ ok: true }), /操作未完成/);
});

test('public error description keeps the code and actionable guidance without dumping private details', () => {
  const { errorDescription } = load().plugin.__testing;
  const error = { code: 'service_auth_rejected', message: '后台拒绝连接认证。', details: { action: '请重新连接 one_search。', token: 'private-token', password: 'private-password', raw: 'private-log' } };
  const info = errorDescription(error);
  assert.equal(info.code, 'service_auth_rejected'); assert.equal(info.action, error.details.action);
  assert.doesNotMatch(JSON.stringify(info), /private-/);
  assert.equal(errorDescription({ message: 'x'.repeat(2000), code: 'bad code' }).message.length, 1000);
  assert.equal(errorDescription({}).code, 'operation_failed');
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

test('pause control works without progress and supports indefinite and timed pauses', async () => {
  const harness = hookHarness();
  const { IndexControls } = load(harness.React).plugin.__testing;
  const calls = [];
  const props = { status: { service: { status: 'running' }, index: {} }, run: (...args) => calls.push(args), busy: false };
  let tree = harness.render(IndexControls, props);
  let button = all(tree, node => textOf(node) === '暂停索引' && node.props.onClick)[0];
  assert.equal(button.props.disabled, false);
  button.props.onClick();
  assert.equal(calls[0][0], 'pause'); assert.equal(calls[0][1].seconds, null);
  all(tree, node => node.props['aria-label'] === '暂停时长')[0].props.onChange('30');
  tree = harness.render(IndexControls, props);
  button = all(tree, node => textOf(node) === '暂停索引' && node.props.onClick)[0]; button.props.onClick();
  assert.equal(calls[1][1].seconds, 1800); assert.equal(calls[1][1].duration_seconds, undefined);
  assert.match(textOf(tree), /已有索引仍可检索/);
  harness.dispose();
});

test('pause control shows draining, paused and unknown states without treating a stopped service as paused', () => {
  const React = { createElement: element, Fragment: 'fragment', useState: initial => [initial, () => {}] };
  const { IndexControls } = load(React).plugin.__testing;
  const calls = [];
  const props = { status: { service: { status: 'running' }, index: { pause_state: 'pausing' } }, run: (...args) => calls.push(args), busy: false };
  let tree = IndexControls(props);
  assert.match(textOf(tree), /正在暂停，等待当前任务收尾/);
  all(tree, node => textOf(node) === '恢复索引' && node.props.onClick)[0].props.onClick();
  assert.equal(calls[0][0], 'resume');
  props.status.index = { paused: true };
  assert.match(textOf(IndexControls(props)), /后台索引已暂停/);
  tree = IndexControls({ ...props, disconnected: true });
  assert.match(textOf(tree), /暂停状态待确认/); assert.doesNotMatch(textOf(tree), /后台索引已暂停/);
  assert.equal(all(tree, node => node.props.onClick)[0].props.disabled, true);
  tree = IndexControls({ ...props, status: { service: { status: 'stopped' }, index: {} } });
  assert.match(textOf(tree), /服务已停止，索引操作不可用/); assert.equal(all(tree, node => node.props.onClick)[0].props.disabled, true);
  tree = IndexControls({ ...props, status: null });
  assert.match(textOf(tree), /暂停索引/); assert.equal(all(tree, node => node.props.onClick)[0].props.disabled, true);
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

test('pause controls stay outside tab panels and immediately show the acknowledged pause state', async () => {
  const harness = hookHarness(), clock = fakeClock();
  const { Panel } = load(harness.React, { document: clock.doc, setTimeout: clock.setTimer, clearTimeout: clock.clearTimer }).plugin.__testing;
  let statuses = 0, finishStatus;
  const request = async action => {
    if (action === 'settings_get') return { revision: 'one', values: { roots: [], indexing: {}, runtime_policy: {}, databases: [] } };
    if (action === 'status') return ++statuses === 1 ? { service: { status: 'running' }, index: {} } : new Promise(resolve => { finishStatus = resolve; });
    if (action === 'pause') return { paused: true, user_paused: true, pause_until: null, pause_state: 'pausing' };
  };
  harness.render(Panel, { request }); await tick(); let tree = harness.render(Panel, { request });
  const controls = () => all(tree, node => node.type?.name === 'IndexControls')[0];
  for (const tab of all(tree, node => node.props.role === 'tab')) {
    tab.props.onClick(); tree = harness.render(Panel, { request });
    assert.ok(controls());
    assert.equal(all(tree, node => node.props.role === 'tabpanel').flatMap(panel => all(panel, node => node.type?.name === 'IndexControls')).length, 0);
  }
  await controls().props.run('pause', { seconds: null }); tree = harness.render(Panel, { request });
  assert.equal(controls().props.status.index.pause_state, 'pausing');
  assert.match(textOf(tree), /正在暂停/); assert.match(textOf(tree), /请求已接受/);
  finishStatus({ service: { status: 'running' }, index: { pause_state: 'paused', paused: true } });
  await tick(); tree = harness.render(Panel, { request });
  assert.equal(controls().props.status.index.pause_state, 'paused');
  harness.dispose();
});

test('connection errors show a code and advice, then recovery reloads missing settings automatically', async () => {
  const harness = hookHarness(), clock = fakeClock();
  const { Panel } = load(harness.React, { document: clock.doc, setTimeout: clock.setTimer, clearTimeout: clock.clearTimer }).plugin.__testing;
  let offline = true, reads = 0;
  const error = Object.assign(new Error('本地后台未接受连接。'), { code: 'service_connection_refused', details: { action: '检查 one_search 是否运行，随后点击立即重试。', token: 'private-token', password: 'private-password' } });
  const request = async action => {
    if (action === 'status') { if (offline) throw error; return { service: { status: 'running' }, index: {} }; }
    if (action === 'settings_get') { reads++; if (offline) throw error; return { revision: 'ready', values: { roots: ['D:/restored'], indexing: {}, runtime_policy: {}, databases: [] } }; }
  };
  harness.render(Panel, { request }); await tick(); let tree = harness.render(Panel, { request });
  assert.match(textOf(tree), /service_connection_refused/); assert.match(textOf(tree), /检查 one_search 是否运行/);
  assert.doesNotMatch(textOf(tree), /private-token|private-password/);
  assert.equal(all(tree, node => node.type?.name === 'IndexControls')[0].props.disconnected, true);
  assert.equal(reads, 1);
  offline = false; clock.fire(); await tick(); tree = harness.render(Panel, { request });
  assert.equal(reads, 2); assert.equal(all(tree, node => node.type?.name === 'IndexControls')[0].props.disconnected, false);
  assert.deepEqual(Array.from(all(tree, node => node.type?.name === 'Scope')[0].props.values.roots), ['D:/restored']);
  assert.doesNotMatch(textOf(tree), /service_connection_refused/);
  harness.dispose();
});

test('a status request started before pause cannot overwrite the acknowledged pause state', async () => {
  const harness = hookHarness(), clock = fakeClock();
  const { Panel } = load(harness.React, { document: clock.doc, setTimeout: clock.setTimer, clearTimeout: clock.clearTimer }).plugin.__testing;
  let statuses = 0, finishOldStatus;
  const request = async action => {
    if (action === 'settings_get') return { revision: 'one', values: { roots: [], indexing: {}, runtime_policy: {}, databases: [] } };
    if (action === 'status') return ++statuses === 1 ? { service: { status: 'running' }, index: { pause_state: 'running' } } : new Promise(resolve => { finishOldStatus = resolve; });
    if (action === 'pause') return { paused: true, user_paused: true, pause_state: 'pausing' };
  };
  harness.render(Panel, { request }); await tick(); let tree = harness.render(Panel, { request });
  clock.fire();
  await all(tree, node => node.type?.name === 'IndexControls')[0].props.run('pause', { seconds: null });
  finishOldStatus({ service: { status: 'running' }, index: { pause_state: 'running' } });
  await tick(); tree = harness.render(Panel, { request });
  assert.equal(all(tree, node => node.type?.name === 'IndexControls')[0].props.status.index.pause_state, 'pausing');
  assert.equal(clock.timers.values().next().value.delay, 0);
  harness.dispose();
});

test('failed pause shows its code and action without changing the last confirmed pause state', async () => {
  const harness = hookHarness(), clock = fakeClock();
  const { Panel } = load(harness.React, { document: clock.doc, setTimeout: clock.setTimer, clearTimeout: clock.clearTimer }).plugin.__testing;
  const request = async action => {
    if (action === 'settings_get') return { revision: 'one', values: { roots: [], indexing: {}, runtime_policy: {}, databases: [] } };
    if (action === 'status') return { service: { status: 'running' }, index: { pause_state: 'running' } };
    if (action === 'pause') throw Object.assign(new Error('后台正在停止。'), { code: 'service_stopping', details: { action: '等待后台停止后重新连接。' } });
  };
  harness.render(Panel, { request }); await tick(); let tree = harness.render(Panel, { request });
  await all(tree, node => node.type?.name === 'IndexControls')[0].props.run('pause', { seconds: null });
  tree = harness.render(Panel, { request });
  assert.match(textOf(tree), /service_stopping/); assert.match(textOf(tree), /等待后台停止后重新连接/);
  assert.equal(all(tree, node => node.type?.name === 'IndexControls')[0].props.status.index.pause_state, 'running');
  harness.dispose();
});

test('reconnection preserves dirty settings and does not automatically reload an existing draft', async () => {
  const harness = hookHarness(), clock = fakeClock();
  const { Panel } = load(harness.React, { document: clock.doc, setTimeout: clock.setTimer, clearTimeout: clock.clearTimer }).plugin.__testing;
  let offline = false, reads = 0;
  const request = async action => {
    if (action === 'settings_get') { reads++; return { revision: 'one', values: { roots: ['D:/before'], indexing: {}, runtime_policy: {}, databases: [] } }; }
    if (action === 'status') { if (offline) throw new Error('offline'); return { service: { status: 'running' }, index: {} }; }
  };
  harness.render(Panel, { request }); await tick(); let tree = harness.render(Panel, { request });
  all(tree, node => node.type?.name === 'Scope')[0].props.update('roots', ['D:/mine']);
  tree = harness.render(Panel, { request }); offline = true; clock.fire(); await tick(); tree = harness.render(Panel, { request });
  offline = false; clock.fire(); await tick(); tree = harness.render(Panel, { request });
  assert.deepEqual(Array.from(all(tree, node => node.type?.name === 'Scope')[0].props.values.roots), ['D:/mine']);
  assert.equal(reads, 1);
  harness.dispose();
});

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

test('service controls allow explicit start while offline and require a second click to force stop', () => {
  const harness = hookHarness();
  const { ServiceControls } = load(harness.React).plugin.__testing;
  const calls = [];
  const props = { status: { service: { status: 'offline' }, control: { desired_state: 'running' }, reconnect: { state: 'waiting', attempt: 4, next_retry_at: 2000000000 } }, run: (...args) => calls.push(args), busy: false };
  let tree = harness.render(ServiceControls, props);
  assert.match(textOf(tree), /异常断线，等待重连/); assert.match(textOf(tree), /重试次数：4/);
  all(tree, node => textOf(node) === '强制结束' && node.props.onClick)[0].props.onClick();
  assert.equal(calls.length, 0);
  tree = harness.render(ServiceControls, props);
  assert.match(textOf(tree), /已启用的定时任务仍可在到点后启动服务/);
  all(tree, node => textOf(node) === '确认强制结束' && node.props.onClick)[0].props.onClick();
  assert.equal(calls[0][0], 'service_force_stop');
  calls[0][2]();
  props.status = { service: { status: 'stopped' }, control: { desired_state: 'stopped' }, reconnect: { state: 'stopped' } };
  tree = harness.render(ServiceControls, props);
  assert.match(textOf(tree), /后台已主动停止/); assert.match(textOf(tree), /自动重连已关闭/); assert.doesNotMatch(textOf(tree), /下次重试/);
  const start = all(tree, node => textOf(node) === '启动服务' && node.props.onClick)[0];
  assert.equal(start.props.disabled, false); start.props.onClick(); assert.equal(calls[1][0], 'service_start');
  assert.equal(all(tree, node => textOf(node) === '停止服务' && node.props.onClick)[0].props.disabled, true);
  tree = harness.render(ServiceControls, { ...props, status: null, disconnected: true });
  assert.equal(all(tree, node => textOf(node) === '启动服务' && node.props.onClick)[0].props.disabled, false);
  harness.dispose();
});

test('service stop acknowledgement clears progress and cannot be undone by an earlier status sample', async () => {
  const harness = hookHarness(), clock = fakeClock();
  const { Panel } = load(harness.React, { document: clock.doc, setTimeout: clock.setTimer, clearTimeout: clock.clearTimer }).plugin.__testing;
  let statuses = 0, finishOld;
  const running = { service: { status: 'running' }, control: { desired_state: 'running' }, index: { pause_state: 'running', progress: { overall: { state: 'indexing' } } } };
  const request = async action => {
    if (action === 'settings_get') return { revision: 'one', values: { roots: [], indexing: {}, runtime_policy: {}, databases: [] } };
    if (action === 'status') return ++statuses === 1 ? running : new Promise(resolve => { finishOld = resolve; });
    if (action === 'service_stop') return { service: { status: 'stopped' }, control: { desired_state: 'stopped' }, reconnect: { state: 'stopped' } };
  };
  harness.render(Panel, { request }); await tick(); let tree = harness.render(Panel, { request });
  clock.fire();
  await all(tree, node => node.type?.name === 'ServiceControls')[0].props.run('service_stop');
  finishOld(running); await tick(); tree = harness.render(Panel, { request });
  const service = all(tree, node => node.type?.name === 'ServiceControls')[0];
  assert.equal(service.props.status.control.desired_state, 'stopped'); assert.equal(service.props.status.index, undefined);
  assert.equal(all(tree, node => node.type?.name === 'IndexControls')[0].props.busy, true);
  assert.equal(all(tree, node => node.type?.name === 'Schedules')[0].props.busy, false);
  assert.equal(all(tree, node => node.type?.name === 'Overview')[0].props.status.index, undefined);
  assert.match(textOf(tree), /后台已主动停止/);
  harness.dispose();
});

test('daemon errors keep service controls available and never present old index activity as current', async () => {
  const harness = hookHarness(), clock = fakeClock();
  const { Panel } = load(harness.React, { document: clock.doc, setTimeout: clock.setTimer, clearTimeout: clock.clearTimer }).plugin.__testing;
  const request = async action => {
    if (action === 'settings_get') throw new Error('offline');
    if (action === 'status') return { service: { status: 'offline', error: { code: 'service_connection_refused', message: '无法连接后台。' } }, control: { desired_state: 'running' }, reconnect: { state: 'waiting' } };
  };
  harness.render(Panel, { request }); await tick(); const tree = harness.render(Panel, { request });
  assert.match(textOf(tree), /service_connection_refused/);
  assert.equal(all(tree, node => node.type?.name === 'ServiceControls')[0].props.busy, false);
  assert.equal(all(tree, node => node.type?.name === 'IndexControls')[0].props.busy, true);
  assert.equal(all(tree, node => node.type?.name === 'Schedules')[0].props.busy, false);
  harness.dispose();
});

test('schedule times use the service machine timezone and validate weekly selections', () => {
  const { scheduleDraft, scheduleTask, scheduleDate } = load().plugin.__testing;
  const timezone = { name: 'Asia/Shanghai', offset: '+08:00', now: '2030-04-05T12:00:00+08:00' };
  const draft = scheduleDraft(null, timezone);
  assert.equal(draft.date, '2030-04-05');
  Object.assign(draft, { name: ' 工作日启动 ', time: '18:30' });
  const once = scheduleTask(draft, timezone);
  assert.equal(once.name, '工作日启动'); assert.equal(once.schedule.at, '2030-04-05T18:30:00');
  assert.match(scheduleDate('2030-04-05T10:30:00Z', timezone), /18:30/);
  assert.throws(() => scheduleTask({ ...draft, date: '2030-02-31' }, timezone), /有效的启动日期/);
  assert.throws(() => scheduleTask({ ...draft, kind: 'weekly', weekdays: [] }, timezone), /至少一个星期/);
  assert.throws(() => scheduleTask(draft), /服务器时区/);
  assert.deepEqual(Array.from(scheduleTask({ ...draft, kind: 'weekly', weekdays: [5, 1, 1] }, timezone).schedule.weekdays), [1, 5]);
  assert.match(scheduleDate('2030-04-05T10:30:00Z', { offset: '+08:00' }), /18:30.*UTC\+08:00/);
});

test('schedules load while stopped, preserve editing revision during polling and send complete tasks', async () => {
  const harness = hookHarness(), clock = fakeClock();
  const { Schedules } = load(harness.React, { document: clock.doc, setTimeout: clock.setTimer, clearTimeout: clock.clearTimer }).plugin.__testing;
  let revision = 'first'; const calls = [];
  const request = async action => {
    assert.equal(action, 'schedules_get');
    return { revision, timezone: { name: 'Asia/Shanghai', offset: '+08:00', now: '2030-04-05T12:00:00+08:00' }, tasks: [], scheduler: { kind: 'windows' } };
  };
  const props = { active: true, request, run: (...args) => calls.push(args), busy: false };
  harness.render(Schedules, props); await tick(); let tree = harness.render(Schedules, props);
  assert.match(textOf(tree), /尚无定时任务/); assert.match(textOf(tree), /服务所在电脑的时区/);
  assert.equal(clock.timers.values().next().value.delay, 30000);
  all(tree, node => textOf(node) === '新建启动任务' && node.props.onClick)[0].props.onClick();
  tree = harness.render(Schedules, props);
  all(tree, node => node.type?.name === 'Input' && node.props.placeholder)[0].props.onChange('按时启动');
  tree = harness.render(Schedules, props);
  revision = 'second'; clock.fire(); await tick(); tree = harness.render(Schedules, props);
  all(tree, node => textOf(node) === '保存启动任务' && node.props.onClick)[0].props.onClick();
  assert.equal(calls[0][0], 'schedule_save'); assert.equal(calls[0][1].revision, 'first');
  assert.equal(calls[0][1].task.name, '按时启动'); assert.equal(calls[0][1].task.schedule.at, '2030-04-05T09:00:00');
  // A failed operation never calls this callback, so the form remains available for correction.
  assert.ok(all(tree, node => node.props['aria-label'] === '新建启动任务').length);
  calls[0][2]({ revision: 'third', timezone: { offset: '+08:00' }, tasks: [] });
  tree = harness.render(Schedules, props); assert.equal(all(tree, node => node.props['aria-label'] === '新建启动任务').length, 0);
  harness.dispose();
});

test('schedule list shows execution evidence and requires confirmation before deletion', async () => {
  const harness = hookHarness(), clock = fakeClock();
  const { Schedules } = load(harness.React, { document: clock.doc, setTimeout: clock.setTimer, clearTimeout: clock.clearTimer }).plugin.__testing;
  const calls = [], scheduled = { id: 'task-one', name: '工作日启动', enabled: true, schedule: { kind: 'weekly', time: '09:00', weekdays: [1, 3] }, next_run_at: 1900000000, last_run_at: 1800000000, last_result: { status: 'failed', code: 'start_failed' } };
  const snapshot = { revision: 'one', tasks: [scheduled], timezone: { name: 'Asia/Shanghai', offset: '+08:00' }, scheduler: {} };
  const props = { active: true, busy: false, request: async () => snapshot, run: (...args) => calls.push(args) };
  harness.render(Schedules, props); await tick(); let tree = harness.render(Schedules, props);
  assert.match(textOf(tree), /周一、周三 09:00/); assert.match(textOf(tree), /上次结果：执行失败/); assert.match(textOf(tree), /start_failed/);
  all(tree, node => textOf(node) === '禁用' && node.props.onClick)[0].props.onClick();
  assert.equal(calls[0][1].task.enabled, false); assert.equal(calls[0][1].revision, 'one'); assert.equal(calls[0][1].task.last_result, undefined);
  all(tree, node => textOf(node) === '删除' && node.props.onClick)[0].props.onClick();
  assert.equal(calls.length, 1); tree = harness.render(Schedules, props);
  all(tree, node => textOf(node) === '确认删除任务' && node.props.onClick)[0].props.onClick();
  assert.equal(calls[1][0], 'schedule_delete'); assert.equal(calls[1][1].id, 'task-one'); assert.equal(calls[1][1].revision, 'one');
  harness.dispose();
});

test('schedule polling is inactive outside its tab and is disposed when leaving it', async () => {
  const harness = hookHarness(), clock = fakeClock();
  const { Schedules } = load(harness.React, { document: clock.doc, setTimeout: clock.setTimer, clearTimeout: clock.clearTimer }).plugin.__testing;
  let reads = 0;
  const props = { active: false, busy: false, request: async () => { reads++; return { tasks: [] }; }, run() {} };
  harness.render(Schedules, props); await tick(); assert.equal(reads, 0);
  harness.render(Schedules, { ...props, active: true }); await tick(); assert.equal(reads, 1);
  harness.render(Schedules, props); assert.equal(clock.timers.size, 0);
  harness.dispose();
});
