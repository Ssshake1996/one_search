import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { createWebBridge, preparedBackend, registerWebHost, runManagement, webRpcHandler } from '../web-host.mjs';
import { createUpgradeCoordinator } from '../upgrade-coordinator.mjs';

async function fixture(t, override) {
  const directory = await mkdtemp(join(tmpdir(), 'one-search-web-host-'));
  const configPath = join(directory, 'config.json');
  const calls = [];
  const state = { token: 'private-daemon-token-'.repeat(4), pid: process.pid, service_id: 'test-service', config_path: configPath };
  const server = createServer(async (request, response) => {
    assert.equal(request.headers.authorization, 'Bearer ' + state.token);
    assert.equal(request.headers.origin, undefined);
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks));
    calls.push(body);
    const result = override ? await override(body, state) : body.method === '_health'
      ? { status: 'running', pid: state.pid, service_id: state.service_id }
      : { method: body.method, token: state.token, coverage: { documents: { token: 12 } } };
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ ok: true, result }));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  state.port = server.address().port;
  await writeFile(configPath, JSON.stringify({ data_dir: directory }));
  await writeFile(join(directory, 'service.json'), JSON.stringify(state));
  const connection = { command: process.execPath, args: ['-m', 'fixture', 'mcp', '--config', configPath] };
  t.after(async () => { server.closeAllConnections(); await new Promise((resolve) => server.close(resolve)); await rm(directory, { recursive: true, force: true }); });
  return { directory, configPath, calls, state, connection, server };
}

test('prepared backend uses trusted MCP arguments only', () => {
  assert.throws(() => preparedBackend({ command: process.execPath, args: ['untrusted'] }));
  const configPath = join(tmpdir(), 'one-search-config.json');
  assert.deepEqual(preparedBackend({ command: process.execPath, args: ['module', 'mcp', '--config', configPath] }),
    { command: process.execPath, commandArgs: ['module'], configPath });
});

test('status is direct local RPC, deduped, cached and excludes backend secrets', async (t) => {
  const f = await fixture(t);
  let time = 1000;
  const bridge = createWebBridge(f.connection, { now: () => time, manage: () => { throw new Error('Status must not spawn'); } });
  t.after(() => bridge.dispose());
  const [a, b] = await Promise.all([bridge.request({ action: 'status', params: {} }), bridge.request({ action: 'status', params: {} })]);
  assert.deepEqual(a, b);
  assert.equal(a.result.service.status, 'running');
  assert.equal(a.result.index.coverage.documents.token, 12);
  assert.equal(JSON.stringify(a).includes('private-daemon-token'), false);
  assert.equal(JSON.stringify(a).includes('database-password'), false);
  assert.deepEqual(f.calls.map((c) => c.method), ['_health', 'index_status']);
  await bridge.request({ action: 'status', params: {} });
  assert.equal(f.calls.length, 2);
  time += 1001;
  await bridge.request({ action: 'status', params: {} });
  assert.equal(f.calls.length, 4);
});

test('missing or wrong daemon identity is structured offline and never returns state', async (t) => {
  const f = await fixture(t, () => ({ status: 'running', pid: process.pid, service_id: 'other-service' }));
  const bridge = createWebBridge(f.connection);
  t.after(() => bridge.dispose());
  assert.equal((await bridge.request({ action: 'status', params: {} })).error.code, 'service_offline');
  f.state.config_path = join(f.directory, 'another-config.json');
  await writeFile(join(f.directory, 'service.json'), JSON.stringify(f.state));
  assert.equal((await bridge.request({ action: 'status', params: {} })).error.code, 'service_offline');
  assert.equal(f.calls.length, 1);
});

test('browser cannot select arbitrary RPCs, executables, config paths or unbounded pause', async (t) => {
  const f = await fixture(t);
  const bridge = createWebBridge(f.connection);
  for (const input of [null, {}, { action: '_stop', params: {} }, { action: 'status', params: { configPath: 'other' } },
    { action: 'pause', params: { seconds: 604801 } }, { action: 'scan', params: {}, command: 'evil' },
    { action: 'refresh_path', params: { path: 'relative' } }, { action: 'settings_save', params: { text: 'x'.repeat(140000) } }]) {
    assert.equal((await bridge.request(input)).error.code, 'invalid_request');
  }
  assert.equal(f.calls.length, 0);
});

test('mutations serialize, bound queue, and invalidate cached status', async (t) => {
  const f = await fixture(t);
  const seen = [];
  let release;
  const bridge = createWebBridge(f.connection, { manage: async (_backend, request) => {
    seen.push(request.params.id);
    if (request.params.id === 1) await new Promise((resolve) => { release = resolve; });
    return { ok: true, result: { saved: request.params.id } };
  } });
  await bridge.request({ action: 'status', params: {} });
  const writes = [1, 2, 3, 4].map((id) => bridge.request({ action: 'settings_save', params: { id } }));
  await delay(0);
  assert.deepEqual(seen, [1]);
  assert.equal((await bridge.request({ action: 'settings_get', params: {} })).error.code, 'busy');
  release();
  assert.deepEqual((await Promise.all(writes)).map((r) => r.result.saved), [1, 2, 3, 4]);
  await bridge.request({ action: 'status', params: {} });
  assert.equal(f.calls.length, 4);
  bridge.dispose();
  assert.equal((await bridge.request({ action: 'status', params: {} })).error.code, 'cancelled');
});

test('pause/resume/scan use bounded direct methods without management children', async (t) => {
  const f = await fixture(t);
  const bridge = createWebBridge(f.connection, { manage: () => { throw new Error('Not expected'); } });
  for (const action of ['pause', 'resume', 'scan']) assert.equal((await bridge.request({ action, params: action === 'pause' ? { seconds: 300 } : {} })).ok, true);
  assert.deepEqual(f.calls.map((c) => c.method), ['pause', 'resume', 'scan']);
});

test('management child receives secrets on stdin, not argv, and preserves valid column names', async (t) => {
  const f = await fixture(t);
  const script = join(f.directory, 'management.mjs');
  await writeFile(script, `let raw=''; for await (const c of process.stdin) raw+=c;
    const request=JSON.parse(raw); console.error('private diagnostic');
    console.log(JSON.stringify({ok:true,result:{argv:process.argv.slice(2),action:request.action,stored:Boolean(request.params.password),allowed_columns:{token:['password']},ref:'vault:fixture'}}));`);
  const result = await runManagement({ command: process.execPath, commandArgs: [script], configPath: f.configPath },
    { action: 'credential_store', params: { password: 'never-argv-secret' } });
  assert.equal(result.ok, true);
  assert.deepEqual(result.result.argv, ['web-manage', '--config', f.configPath]);
  assert.equal(result.result.ref, 'vault:fixture');
  assert.equal(result.result.stored, true);
  assert.deepEqual(result.result.allowed_columns, { token: ['password'] });
  assert.equal(JSON.stringify(result).includes('never-argv-secret'), false);
});

test('accepted save survives browser cancellation and plugin disposal', async (t) => {
  const f = await fixture(t);
  let release;
  let saveSignal;
  const bridge = createWebBridge(f.connection, { manage: async (_backend, _request, options) => {
    saveSignal = options.signal;
    await new Promise((resolve) => { release = resolve; });
    return { ok: true, result: { saved: true } };
  } });
  const browser = new AbortController();
  const result = bridge.request({ action: 'settings_save', params: {} }, browser.signal);
  await delay(0);
  browser.abort();
  bridge.dispose();
  assert.equal(saveSignal.aborted, false);
  release();
  assert.equal((await result).result.saved, true);
});

test('save response timeout leaves child alive and blocks later mutations until close', async (t) => {
  const f = await fixture(t);
  const script = join(f.directory, 'slow-save.mjs');
  const marker = join(f.directory, 'saved.txt');
  await writeFile(script, `import {writeFile} from 'node:fs/promises';
    let raw=''; for await (const c of process.stdin) raw+=c;
    const request=JSON.parse(raw); await new Promise(r=>setTimeout(r,180));
    await writeFile(request.params.marker,'committed'); console.log(JSON.stringify({ok:true,result:{saved:true}}));`);
  const bridge = createWebBridge(f.connection, { manage: (_backend, request, options) => runManagement(
    { command: process.execPath, commandArgs: [script], configPath: f.configPath }, request, { ...options, timeoutMs: 60 }) });
  const save = await bridge.request({ action: 'settings_save', params: { marker } });
  assert.equal(save.error.code, 'operation_timeout');
  assert.match(save.error.message, /保存结果尚未确认/);
  const pause = bridge.request({ action: 'pause', params: {} });
  await delay(30);
  assert.equal(f.calls.length, 0);
  assert.equal((await pause).ok, true);
  assert.equal(await readFile(marker, 'utf8'), 'committed');
  assert.equal(f.calls[0].method, 'pause');
});

test('management timeout and malformed stdout are sanitized', async (t) => {
  const f = await fixture(t);
  const script = join(f.directory, 'management.mjs');
  await writeFile(script, `process.stdin.resume(); setInterval(()=>{},1000);`);
  const backend = { command: process.execPath, commandArgs: [script], configPath: f.configPath };
  await assert.rejects(runManagement(backend, { action: 'settings_get', params: {} }, { timeoutMs: 75 }), (error) => error.code === 'operation_timeout');
  await writeFile(script, `console.log('private invalid diagnostic');`);
  await assert.rejects(runManagement(backend, { action: 'settings_get', params: {} }), (error) => error.code === 'management_unavailable' && !error.message.includes('private'));
});

test('optional host registration uses authenticated connection and disposes only its route', async (t) => {
  const f = await fixture(t);
  let effect;
  let route;
  let removed = false;
  registerWebHost({ inject(services, callback) {
    assert.deepEqual(services, ['connection', 'webServer']);
    callback({ effect(fn) { effect = fn; }, connection: { requestRejection() { return 401; } }, webServer: { register(value) {
      route = value; return async () => { removed = true; };
    } } });
  } }, f.connection);
  const dispose = effect();
  assert.equal(route.path, '/one-search');
  assert.equal(route.kind, 'prefix');
  await dispose();
  assert.equal(removed, true);
});

test('HTTP adapter checks host authentication before accepting bounded DSH RPC envelopes', async (t) => {
  const calls = [];
  const handler = webRpcHandler({ connection: { requestRejection(req) { return req.headers.cookie === 'test-session' ? undefined : 401; } } },
    { async request(payload) { calls.push(payload); return { ok: true, result: { scanned: 12 } }; } });
  const server = createServer(handler);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => { server.closeAllConnections(); await new Promise((resolve) => server.close(resolve)); });
  const url = `http://127.0.0.1:${server.address().port}/one-search/request`;
  const body = JSON.stringify({ type: 'client-request', rpcId: 'test-1', method: 'request', payload: { action: 'status', params: {} } });
  assert.equal((await fetch(url, { method: 'POST', body })).status, 401);
  const success = await (await fetch(url, { method: 'POST', body, headers: { Cookie: 'test-session' } })).json();
  assert.equal(success.rpcId, 'test-1');
  assert.equal(success.result.value.result.scanned, 12);
  assert.equal(calls.length, 1);
  assert.equal((await fetch(url, { method: 'POST', body: 'x'.repeat(140000), headers: { Cookie: 'test-session' } })).status, 413);
  assert.equal((await fetch(url, { method: 'POST', body: '{}', headers: { Cookie: 'test-session' } })).status, 400);
  assert.equal(calls.length, 1);
});

test('maintenance status is public and does not resolve the backend, read files or spawn', async () => {
  const bridge = createWebBridge(() => { throw new Error('Backend must not be resolved during maintenance'); }, {
    maintenanceStatus: () => ({ maintenance: true, state: 'private-state-secret', token: 'private-token', config: 'private-path' }),
    runRuntime: () => { throw new Error('Runtime must not be admitted'); },
    manage: () => { throw new Error('No child is allowed'); },
  });
  assert.deepEqual(await bridge.request({ action: 'status', params: {} }), {
    ok: true, result: { service: { status: 'maintenance' }, upgrade: { maintenance: true, state: 'maintenance' } },
  });
  for (const action of ['settings_get', 'settings_save', 'db_discover', 'pause', 'scan', 'diagnose_path']) {
    const params = action === 'diagnose_path' ? { path: join(tmpdir(), 'fixture') } : {};
    assert.equal((await bridge.request({ action, params })).error.code, 'upgrade_in_progress');
  }
  bridge.dispose();
});

test('runtime gate closes a new-marker race before any backend access', async () => {
  let attempts = 0;
  const bridge = createWebBridge(() => { throw new Error('Provider was accessed after gate refusal'); }, {
    runRuntime: () => { attempts++; throw Object.assign(new Error('private rejection details'), { code: 'upgrade_in_progress' }); },
  });
  assert.equal((await bridge.request({ action: 'status', params: {} })).result.service.status, 'maintenance');
  for (const action of ['settings_get', 'pause', 'diagnose_path']) {
    const result = await bridge.request({ action, params: action === 'diagnose_path' ? { path: join(tmpdir(), 'fixture') } : {} });
    assert.equal(result.error.code, 'upgrade_in_progress');
    assert.equal(JSON.stringify(result).includes('private'), false);
  }
  assert.equal(attempts, 4);
});

test('maintenance transitions and replacement connection invalidate a fresh cached status', async (t) => {
  const a = await fixture(t);
  const b = await fixture(t);
  let current = a.connection;
  let maintenance = false;
  const bridge = createWebBridge(() => current, { now: () => 1000,
    maintenanceStatus: () => ({ maintenance, state: maintenance ? 'maintenance' : 'ready' }) });
  t.after(() => bridge.dispose());
  await bridge.request({ action: 'status', params: {} });
  assert.equal(a.calls.length, 2);
  maintenance = true;
  assert.equal((await bridge.request({ action: 'status', params: {} })).result.service.status, 'maintenance');
  assert.equal(a.calls.length, 2);
  maintenance = false;
  await bridge.request({ action: 'status', params: {} });
  assert.equal(a.calls.length, 4);
  current = b.connection;
  await bridge.request({ action: 'status', params: {} });
  assert.equal(b.calls.length, 2);
});

test('queued management rechecks maintenance at execution and never launches its child', async (t) => {
  const f = await fixture(t);
  let maintenance = false;
  let release;
  let launched = 0;
  const bridge = createWebBridge(f.connection, {
    maintenanceStatus: () => ({ maintenance, state: 'maintenance' }),
    manage: () => { launched++; return new Promise((resolve) => { release = () => resolve({ ok: true, result: {} }); }); },
  });
  const first = bridge.request({ action: 'settings_save', params: {} });
  const second = bridge.request({ action: 'settings_get', params: {} });
  await delay(0);
  maintenance = true;
  release();
  assert.equal((await first).ok, true);
  assert.equal((await second).error.code, 'upgrade_in_progress');
  assert.equal(launched, 1);
});

test('real coordinator drains a timed-out save until its actual child closes', async (t) => {
  const f = await fixture(t);
  const script = join(f.directory, 'coordinated-save.mjs');
  const marker = join(f.directory, 'saved.txt');
  await writeFile(script, `import {writeFile} from 'node:fs/promises';
    let raw=''; for await (const c of process.stdin) raw+=c;
    const request=JSON.parse(raw); await new Promise(r=>setTimeout(r,350));
    await writeFile(request.params.marker,'committed'); console.log(JSON.stringify({ok:true,result:{saved:true}}));`);
  const coordinator = await createUpgradeCoordinator({ dataDir: f.directory, configPath: f.configPath, clientId: 'web-test' }, { pollMs: 60000 });
  t.after(() => coordinator.dispose());
  let launched = 0;
  const bridge = createWebBridge(f.connection, { runRuntime: coordinator.runRuntime, maintenanceStatus: coordinator.status,
    manage: (_backend, request, options) => { launched++; return runManagement(
      { command: process.execPath, commandArgs: [script], configPath: f.configPath }, request, { ...options, timeoutMs: 35 }); } });
  t.after(() => bridge.dispose());
  const save = await bridge.request({ action: 'settings_save', params: { marker } });
  assert.equal(save.error.code, 'operation_timeout');
  const queued = bridge.request({ action: 'settings_get', params: {} });
  await writeFile(join(f.directory, 'upgrade-state.json'), JSON.stringify({ transaction_id: 'fixture-upgrade' }));
  let drained = false;
  const preparation = coordinator.prepare('fixture-upgrade').then(() => { drained = true; });
  await delay(25);
  assert.equal(drained, false);
  assert.equal((await bridge.request({ action: 'status', params: {} })).result.service.status, 'maintenance');
  await preparation;
  assert.equal(await readFile(marker, 'utf8'), 'committed');
  assert.equal((await queued).error.code, 'upgrade_in_progress');
  assert.equal(launched, 1);
  assert.equal(f.calls.length, 0);
});

test('coordinator waits for an accepted direct RPC before allowing runtime replacement', async (t) => {
  let release;
  let entered;
  const started = new Promise((resolve) => { entered = resolve; });
  const f = await fixture(t, async () => {
    entered();
    await new Promise((resolve) => { release = resolve; });
    return { paused: true };
  });
  const coordinator = await createUpgradeCoordinator({ dataDir: f.directory, configPath: f.configPath, clientId: 'web-rpc-test' }, { pollMs: 60000 });
  t.after(() => coordinator.dispose());
  const bridge = createWebBridge(f.connection, { runRuntime: coordinator.runRuntime, maintenanceStatus: coordinator.status });
  t.after(() => bridge.dispose());
  const pause = bridge.request({ action: 'pause', params: {} });
  await started;
  await writeFile(join(f.directory, 'upgrade-state.json'), JSON.stringify({ transaction_id: 'fixture-rpc' }));
  let drained = false;
  const preparation = coordinator.prepare('fixture-rpc').then(() => { drained = true; });
  await delay(10);
  assert.equal(drained, false);
  release();
  assert.equal((await pause).ok, true);
  await preparation;
  assert.equal(drained, true);
});
