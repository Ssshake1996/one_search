import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtemp, mkdir, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { backendCompatible, findRelease, installerCompatible, prepareService, runProcess, settings } from '../bootstrap.mjs';
import { registerBundle, stageBundle } from '../register.mjs';

test('DSH manifest registers a bundle patch, without blocked install lifecycle scripts', async () => {
  const packageJson = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'));
  assert.equal(packageJson.dsh.bundle.patch, './cordis.patch.yml');
  assert.equal(packageJson.name, 'one-search-bundle');
  assert.equal(packageJson.scripts.postinstall, undefined);
  assert.match(await readFile(new URL('../cordis.patch.yml', import.meta.url), 'utf8'), /name: one-search-bundle/);
});

test('new installations default to whole machine and normal installer paths', () => {
  const config = settings({}, { LOCALAPPDATA: resolve('test-user') }, 'win32');
  assert.deepEqual(config.roots, []);
  assert.equal(config.noAutostart, false);
  assert.equal(config.skipModel, false);
  assert.equal(config.installDir, resolve('test-user', 'data-search', 'app'));
});

test('DSH staging is repeatable, colocates on home drive and refuses changed content', async () => {
  const home = await mkdtemp(join(tmpdir(), 'one-search-stage-test-'));
  const first = await stageBundle(home);
  const repeated = await stageBundle(home);
  assert.equal(first.directory, repeated.directory);
  assert.ok(first.directory.startsWith(home));
  assert.ok(JSON.parse(await readFile(join(first.directory, 'package.json'))).files.includes('register.mjs'));
  await writeFile(join(first.directory, 'index.mjs'), 'unexpected modification');
  await assert.rejects(stageBundle(home), /differs from expected/);
});

test('registration invokes DSH CLI and restores caller environment without claiming connection', async () => {
  const home = await mkdtemp(join(tmpdir(), 'one-search-register-test-'));
  const previous = process.env.DSH_HOME;
  const calls = [];
  const result = await registerBundle({ dshPackage: home, dshHome: home, profile: 'test-profile', offline: true }, async (...args) => {
    calls.push(args);
    assert.equal(process.env.DSH_HOME, home);
  });
  assert.equal(process.env.DSH_HOME, previous);
  assert.equal(calls.length, 1);
  assert.ok(calls[0][1].includes('--ignore-scripts'));
  assert.ok(calls[0][1].includes('--offline'));
  assert.equal(result.registered, true);
  assert.equal(result.connected, false);
});

test('Linux paths honor XDG_DATA_HOME', () => {
  const config = settings({}, { XDG_DATA_HOME: resolve('xdg-data') }, 'linux');
  assert.equal(config.dataDir, resolve('xdg-data', 'data-search', 'data'));
});

test('explicit Windows paths do not require LOCALAPPDATA', () => {
  assert.equal(settings({ installDir: resolve('app'), dataDir: resolve('data') }, {}, 'win32').dataDir, resolve('data'));
});

test('configuration rejects ambiguous command and scope values before spawning', () => {
  for (const config of [
    { roots: 'C:\\' }, { roots: ['relative'] }, { command: 'python' },
    { command: process.execPath }, { timeoutMs: -1 }, { timeoutMs: Infinity },
    { skipModel: true, modelDir: 'model' }, { serverName: 'bad name' },
  ]) assert.throws(() => settings(config));
});

test('existing source backend starts idempotently and preserves configuration', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'one-search-existing-test-'));
  const path = join(dir, 'config.json');
  const original = JSON.stringify({ scope: 'directories', roots: [dir], semantic: { enabled: false } });
  await writeFile(path, original);
  const calls = [];
  const request = { command: process.execPath, configPath: path, commandArgs: ['module'], roots: [resolve('other-root')] };
  const runner = async (...args) => { calls.push(args); return JSON.stringify({ version: '0.5.1' }); };
  const first = await prepareService(request, runner);
  await prepareService(request, runner);
  assert.equal(calls.length, 6);
  assert.deepEqual(calls[1][1], ['module', 'start', '--config', path]);
  assert.equal(calls[2][1][1], 'register-client');
  assert.equal(calls[2][1][2], calls[5][1][2]);
  assert.equal(await readFile(path, 'utf8'), original);
  assert.equal(first.command, process.execPath);
  assert.deepEqual(first.args, ['module', 'mcp', '--config', path]);
  assert.equal(first.cwd, dir);
  assert.equal(first.failOnStartupError, true);
});

test('structured command errors stay distinguishable from stderr and retain actual close lifetime', async () => {
  const operation = runProcess(process.execPath, ['-e', "process.stderr.write('diagnostic before result\\n'); console.log(JSON.stringify({ok:false,operation:'register-client',error:{code:'ServiceError',message:'busy'}})); process.exitCode=1;"]);
  await assert.rejects(operation, { operation: 'register-client', backendCode: 'ServiceError' });
  await operation.closed;
});

test('concurrent profile registry contention retries only registration, without replaying startup', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'one-search-register-contention-'));
  const configPath = join(dir, 'config.json');
  await writeFile(configPath, '{}');
  const operations = [];
  let registrations = 0;
  await prepareService({ command: process.execPath, configPath }, async (_command, args) => {
    operations.push(args[0]);
    if (args[0] === 'version') return JSON.stringify({ version: '0.5.0' });
    if (args[0] === 'register-client' && ++registrations < 3) {
      throw Object.assign(new Error('Busy registry'), { operation: 'register-client', backendCode: 'ServiceError' });
    }
  });
  assert.deepEqual(operations, ['version', 'start', 'register-client', 'register-client', 'register-client']);
});

test('registration retry is bounded and permanent failures are returned immediately', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'one-search-register-bound-'));
  const configPath = join(dir, 'config.json');
  await writeFile(configPath, '{}');
  for (const [backendCode, expected] of [['ServiceError', 5], ['ValueError', 1]]) {
    let registrations = 0;
    await assert.rejects(prepareService({ command: process.execPath, configPath }, async (_command, args) => {
      if (args[0] === 'version') return JSON.stringify({ version: '0.5.0' });
      if (args[0] === 'register-client') {
        registrations++;
        throw Object.assign(new Error('Registration failed'), { operation: 'register-client', backendCode });
      }
    }), { backendCode });
    assert.equal(registrations, expected);
  }
});

test('maintenance starting during registration backoff prevents the next executable launch', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'one-search-register-maintenance-'));
  const configPath = join(dir, 'config.json');
  await writeFile(configPath, '{}');
  let registrations = 0;
  let maintenance = false;
  await assert.rejects(prepareService({ command: process.execPath, configPath }, async (_command, args) => {
    if (args[0] === 'version') return JSON.stringify({ version: '0.5.0' });
    if (args[0] === 'register-client') {
      registrations++; maintenance = true;
      throw Object.assign(new Error('Busy registry'), { operation: 'register-client', backendCode: 'ServiceError' });
    }
  }, { runRuntime(callback) {
    if (maintenance) throw Object.assign(new Error('Upgrading'), { code: 'upgrade_in_progress' });
    return callback();
  } }), { code: 'upgrade_in_progress' });
  assert.equal(registrations, 1);
});

test('missing explicit backend is never replaced by an automatic install', async () => {
  let called = false;
  await assert.rejects(prepareService({ command: process.execPath, configPath: resolve('missing-config-1851873.json') }, async () => { called = true; }));
  assert.equal(called, false);
});

test('release locator supports installed package copies via explicit release directory', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'one-search-release-test-'));
  await writeFile(join(dir, 'RELEASE_MANIFEST.json'), JSON.stringify({ kind: 'windows-native', version: '0.3.0' }));
  const nested = join(dir, 'plugins', 'deepseek-harness');
  await mkdir(nested, { recursive: true });
  assert.equal((await findRelease(undefined, [nested])).directory, dir);
  assert.equal((await findRelease(dir, [])).manifest.version, '0.3.0');
  await assert.rejects(findRelease(join(dir, 'missing'), []), /runtime is not installed/);
});

test('rollback backend remains compatible while old incoming installers are rejected before execution', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'one-search-old-installer-'));
  await writeFile(join(dir, 'RELEASE_MANIFEST.json'), JSON.stringify({ kind: 'windows-native', version: '0.5.0' }));
  let called = false;
  await assert.rejects(prepareService({ releaseDir: dir, installDir: join(dir, 'app'), dataDir: join(dir, 'data') }, async () => {
    called = true;
  }), /installer release >= 0\.5\.1/);
  assert.equal(called, false);
  assert.equal(backendCompatible('0.5.0'), true);
  assert.equal(installerCompatible('0.5.0'), false);
  assert.equal(installerCompatible('0.5.1'), true);
  assert.equal(installerCompatible('0.6.0'), true);
});

test('activation provisions a missing Windows backend with JSON arguments, then starts it', { skip: process.platform !== 'win32' }, async () => {
  const dir = await mkdtemp(join(tmpdir(), 'one-search-provision-test-'));
  const releaseDir = join(dir, 'release with spaces');
  const installDir = join(dir, 'app with spaces');
  const dataDir = join(dir, 'data with spaces');
  const root = join(dir, "corpus quote ' and spaces");
  await mkdir(releaseDir);
  await mkdir(root);
  await writeFile(join(releaseDir, 'RELEASE_MANIFEST.json'), JSON.stringify({ kind: 'windows-native', version: '0.5.1' }));
  const calls = [];
  let requestPath;
  const runner = async (command, args) => {
    calls.push({ command, args });
    if (calls.length === 1) {
      assert.equal(args[0], '-NoProfile');
      assert.ok(args.includes('-NonInteractive'));
      requestPath = args.at(-1);
      const request = JSON.parse(await readFile(requestPath, 'utf8'));
      assert.equal(request.installDir, installDir);
      assert.deepEqual(request.roots, [root]);
      assert.equal(request.skipModel, true);
      assert.equal(request.noAutostart, true);
      await mkdir(join(installDir, 'runtime'), { recursive: true });
      await mkdir(dataDir);
      await writeFile(join(installDir, 'runtime/data-search.exe'), 'synthetic runner, never executed');
      await writeFile(join(dataDir, 'config.json'), JSON.stringify({ scope: 'directories', roots: [root] }));
    }
  };
  const result = await prepareService({ releaseDir, installDir, dataDir, roots: [root], skipModel: true, noAutostart: true }, runner);
  assert.equal(calls.length, 3);
  assert.deepEqual(calls[1].args, ['start', '--config', join(dataDir, 'config.json')]);
  assert.equal(result.command, join(installDir, 'runtime/data-search.exe'));
  await assert.rejects(readFile(requestPath), { code: 'ENOENT' });
});

test('explicit profile client identities remain distinct and reject control characters', () => {
  const first = settings({ clientId: 'dsh-web', clientLabel: 'DSH web' });
  const second = settings({ clientId: 'dsh-terminal', clientLabel: 'DSH terminal' });
  assert.notEqual(first.clientId, second.clientId);
  assert.throws(() => settings({ clientLabel: 'bad\nlabel' }));
  assert.equal(settings().clientId, settings().clientId);
});

test('installer failure prevents MCP activation and cleans its temporary request', { skip: process.platform !== 'win32' }, async () => {
  const dir = await mkdtemp(join(tmpdir(), 'one-search-install-fail-test-'));
  await writeFile(join(dir, 'RELEASE_MANIFEST.json'), JSON.stringify({ kind: 'windows-native', version: '0.5.1' }));
  let requestPath;
  let calls = 0;
  await assert.rejects(prepareService({ releaseDir: dir, installDir: join(dir, 'app'), dataDir: join(dir, 'data') }, async (_command, args) => {
    calls++;
    requestPath = args.at(-1);
    throw new Error('synthetic installer failure');
  }), /synthetic installer failure/);
  assert.equal(calls, 1);
  await assert.rejects(readFile(requestPath), { code: 'ENOENT' });
});

test('old explicit backend fails with an upgrade action before start/register', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'one-search-old-backend-'));
  const path = join(dir, 'config.json');
  await writeFile(path, '{}');
  const calls = [];
  await assert.rejects(prepareService({ command: process.execPath, configPath: path }, async (...args) => {
    calls.push(args); return JSON.stringify({ version: '0.3.0' });
  }), /backend_update_required/);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1][0], 'version');
  assert.equal(backendCompatible('0.5.1'), true);
  assert.equal(backendCompatible('0.5.0'), true);
  assert.equal(backendCompatible('0.4.9'), false);
  assert.equal(backendCompatible('0.3.0'), false);
});

test('old managed backend upgrades through the verified installer then preserves config', { skip: process.platform !== 'win32' }, async () => {
  const dir = await mkdtemp(join(tmpdir(), 'one-search-managed-upgrade-'));
  const app = join(dir, 'app');
  const data = join(dir, 'data');
  const release = join(dir, 'release');
  await mkdir(join(app, 'runtime'), { recursive: true });
  await mkdir(data);
  await mkdir(release);
  const path = join(data, 'config.json');
  const original = '{"scope":"directories","roots":[]}';
  await writeFile(path, original);
  await writeFile(join(app, 'runtime', 'data-search.exe'), 'mock runtime');
  await writeFile(join(app, 'install-manifest.json'), JSON.stringify({ version: '0.3.0' }));
  await writeFile(join(release, 'RELEASE_MANIFEST.json'), JSON.stringify({ kind: 'windows-native', version: '0.5.1' }));
  const calls = [];
  await prepareService({ installDir: app, dataDir: data, releaseDir: release }, async (...args) => {
    calls.push(args);
    if (calls.length === 1) await writeFile(join(app, 'install-manifest.json'), JSON.stringify({ version: '0.5.1' }));
  });
  assert.equal(calls.length, 3);
  assert.ok(calls[0][1].includes('-NonInteractive'));
  assert.equal(calls[1][1][0], 'start');
  assert.equal(await readFile(path, 'utf8'), original);
});
