import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtemp, readFile, readdir, unlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { coordinatorOptions, createUpgradeCoordinator } from '../upgrade-coordinator.mjs';
import { prepareService } from '../bootstrap.mjs';

const deferred = () => { let resolve; const promise = new Promise((r) => { resolve = r; }); return { promise, resolve }; };
async function fixture(t, options = {}) {
  const dataDir = await mkdtemp(join(tmpdir(), 'one-search-upgrade-host-'));
  const configPath = join(dataDir, 'config.json');
  await writeFile(configPath, JSON.stringify({ data_dir: dataDir }));
  const host = await createUpgradeCoordinator({ dataDir, configPath, command: process.execPath,
    commandArgs: [], clientId: 'dsh-test' }, options);
  t.after(() => host.dispose());
  const registration = JSON.parse(await readFile(host.registrationPath, 'utf8'));
  const markerPath = join(dataDir, 'upgrade-state.json');
  return { host, dataDir, configPath, registration, markerPath,
    mark: (id = 'test-upgrade') => writeFile(markerPath, JSON.stringify({ schema_version: 1, transaction_id: id })),
    async call(action, extra = {}, headers = {}) {
      const response = await fetch(`http://127.0.0.1:${registration.port}/control`, {
        method: 'POST', headers: { Authorization: 'Bearer ' + registration.token, ...headers },
        body: JSON.stringify({ action, ...extra }),
      });
      return { status: response.status, body: await response.json() };
    },
  };
}
async function eventually(check) {
  for (let attempt = 0; attempt < 100; attempt++) { if (await check()) return; await delay(10); }
  assert.fail('State did not settle');
}

test('host registration exists before first runtime admission, with protected authenticated control', async (t) => {
  const f = await fixture(t);
  let connected = 0;
  f.host.setResumeHandler(() => f.host.runRuntime(async () => {
    assert.equal((await readdir(join(f.dataDir, 'host-clients'))).length, 1); connected++;
  }));
  await f.host.start();
  assert.equal(connected, 1);
  const status = await f.call('status');
  assert.equal(status.body.result.state, 'ready');
  assert.equal(JSON.stringify(status.body).includes(f.registration.token), false);
  assert.equal((await f.call('status', {}, { Authorization: 'Bearer wrong' })).status, 401);
  assert.equal((await f.call('status', {}, { Origin: 'https://untrusted.example' })).status, 401);
  assert.equal((await f.call('prepare', { transaction_id: 'missing' })).status, 409);
});

test('new profile with marker completes startup without loading runtime and later resumes', async (t) => {
  const f = await fixture(t);
  await f.mark();
  let calls = 0;
  f.host.setResumeHandler(() => f.host.runRuntime(() => { calls++; }));
  assert.equal((await f.host.start()).state, 'maintenance');
  assert.equal(calls, 0);
  assert.equal((await f.call('resume')).body.result.maintenance, true);
  await unlink(f.markerPath);
  await f.call('resume');
  await eventually(() => f.host.status().state === 'ready');
  assert.equal(calls, 1);
});

test('prepare gates new work immediately and waits for MCP disposal and actual child closure', async (t) => {
  const f = await fixture(t);
  const closeMcp = deferred(); const operationDone = deferred(); const childClosed = deferred();
  let disposed = 0;
  f.host.setMcpFiber({ async dispose() { disposed++; await closeMcp.promise; } });
  const response = operationDone.promise;
  response.closed = childClosed.promise;
  const running = f.host.runRuntime(() => response);
  await delay(10);
  await f.mark();
  let prepared = false;
  const request = f.call('prepare', { transaction_id: 'test-upgrade' }).then((value) => { prepared = true; return value; });
  await eventually(() => disposed === 1);
  await assert.rejects(f.host.runRuntime(() => assert.fail('Loaded during maintenance')), { code: 'upgrade_in_progress' });
  closeMcp.resolve(); operationDone.resolve('timed-out response');
  assert.equal(await running, 'timed-out response');
  await delay(10); assert.equal(prepared, false);
  childClosed.resolve();
  assert.equal((await request).body.result.state, 'maintenance');
  assert.equal(disposed, 1);
});

test('prepare during bootstrap prevents subsequent version/start/register operations', async (t) => {
  const f = await fixture(t);
  const version = deferred(); let calls = 0;
  f.host.setResumeHandler(() => prepareService({ command: process.execPath, configPath: f.configPath }, async () => {
    calls++; return version.promise;
  }, { runRuntime: f.host.runRuntime }));
  const startup = f.host.start();
  await eventually(() => calls === 1);
  await f.mark();
  const quiesce = f.call('prepare');
  await eventually(() => f.host.status().maintenance);
  version.resolve(JSON.stringify({ version: '0.5.1' }));
  await quiesce; await startup;
  assert.equal(calls, 1);
  assert.equal(f.host.status().state, 'maintenance');
});

test('multiple profile registrations quiesce independently and disposal cleans only its own record', async (t) => {
  const first = await fixture(t);
  const second = await createUpgradeCoordinator({ dataDir: first.dataDir, configPath: first.configPath,
    command: process.execPath, clientId: 'second-profile' });
  t.after(() => second.dispose());
  assert.equal((await readdir(join(first.dataDir, 'host-clients'))).length, 2);
  let stopped = 0;
  first.host.setMcpFiber({ async dispose() { stopped++; } });
  second.setMcpFiber({ async dispose() { stopped++; } });
  await first.mark();
  await Promise.all([first.host.prepare(), second.prepare()]);
  assert.equal(stopped, 2);
  await first.host.dispose();
  assert.deepEqual(await readdir(join(first.dataDir, 'host-clients')), [second.registrationPath.split(/[\\/]/).at(-1)]);
});

test('lost finish notification recovers through low frequency polling, corrupt marker never resumes', async (t) => {
  const f = await fixture(t, { pollMs: 20 });
  let calls = 0;
  f.host.setResumeHandler(() => f.host.runRuntime(() => { calls++; }));
  await writeFile(f.markerPath, '{');
  await f.host.start();
  await delay(60);
  assert.equal(calls, 0);
  assert.equal(f.host.status().maintenance, true);
  await unlink(f.markerPath);
  await eventually(() => f.host.status().state === 'ready');
  assert.equal(calls, 1);
});

test('automatic installer may prepare and resume its own host without waiting for its own process', async (t) => {
  const f = await fixture(t);
  let starts = 0;
  f.host.setResumeHandler(async () => {
    starts++;
    await f.mark();
    assert.equal((await f.call('prepare')).body.result.maintenance, true);
    await unlink(f.markerPath);
    assert.equal((await f.call('resume')).body.ok, true);
    await f.host.runRuntime(() => {});
  });
  await f.host.start();
  assert.equal(f.host.status().state, 'ready');
  assert.equal(starts, 1);
});

test('late resume notification racing a finishing startup retries exactly once', async (t) => {
  const f = await fixture(t);
  const release = deferred();
  const rejected = deferred();
  const finishStartup = deferred();
  let starts = 0;
  f.host.setResumeHandler(async () => {
    starts++;
    if (starts === 1) {
      await release.promise;
      // Use the actual rejected admission rather than simulating a runtime spawn.
      try { await f.host.runRuntime(() => {}); }
      catch (error) { rejected.resolve(); await finishStartup.promise; throw error; }
    }
    await f.host.runRuntime(() => {});
  });
  const startup = f.host.start();
  await eventually(() => starts === 1);
  await f.mark();
  await f.host.prepare();
  release.resolve();
  await rejected.promise;
  await unlink(f.markerPath);
  await f.host.resume();
  finishStartup.resolve();
  await startup;
  await eventually(() => f.host.status().state === 'ready');
  assert.equal(starts, 2);
});

test('concurrent prepare requests share disposal and a mismatched transaction cannot reopen admission', async (t) => {
  const f = await fixture(t);
  let disposals = 0;
  const close = deferred();
  f.host.setMcpFiber({ async dispose() { disposals++; await close.promise; } });
  await f.mark();
  const calls = [f.call('prepare'), f.call('prepare')];
  await eventually(() => disposals === 1);
  assert.equal((await f.call('prepare', { transaction_id: 'other' })).status, 409);
  close.resolve();
  for (const result of await Promise.all(calls)) assert.equal(result.body.result.maintenance, true);
  assert.equal(disposals, 1);
  assert.equal((await f.call('resume')).body.result.maintenance, true);
});

test('source config selects its actual data directory without starting its executable', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'one-search-actual-data-'));
  const path = join(dir, 'custom.json');
  await writeFile(path, JSON.stringify({ data_dir: join(dir, 'actual') }));
  assert.equal((await coordinatorOptions({ configPath: path, dataDir: join(dir, 'unused') })).dataDir, join(dir, 'actual'));
});

test('asynchronous resume failures expose only safe diagnostic fields and can be retried', async (t) => {
  const diagnostics = [];
  const f = await fixture(t, { onError: (value) => diagnostics.push(value) });
  let attempts = 0;
  f.host.setResumeHandler(async () => {
    if (++attempts === 1) throw Object.assign(new Error('Sensitive connection detail MUST_STAY_PRIVATE'), {
      operation: 'register-client', backendCode: 'ServiceError' });
    await f.host.runRuntime(() => {});
  });
  await f.mark();
  await f.host.start();
  await unlink(f.markerPath);
  await f.host.resume();
  await eventually(() => f.host.status().state === 'failed');
  const status = (await f.call('status')).body;
  assert.deepEqual(status.result.last_error, { code: 'host_startup_failed', operation: 'register-client', backend_code: 'ServiceError' });
  assert.equal(JSON.stringify([status, diagnostics]).includes('MUST_STAY_PRIVATE'), false);
  await f.host.resume();
  await eventually(() => f.host.status().state === 'ready');
  assert.equal(f.host.status().last_error, null);
});
