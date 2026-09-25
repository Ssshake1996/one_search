import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtemp, mkdir, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { findRelease, prepareService, settings } from '../bootstrap.mjs';

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
  const first = await prepareService(request, async (...args) => calls.push(args));
  await prepareService(request, async (...args) => calls.push(args));
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[0][1], ['module', 'start', '--config', path]);
  assert.equal(await readFile(path, 'utf8'), original);
  assert.equal(first.command, process.execPath);
  assert.deepEqual(first.args, ['module', 'mcp', '--config', path]);
  assert.equal(first.cwd, dir);
  assert.equal(first.failOnStartupError, true);
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

test('activation provisions a missing Windows backend with JSON arguments, then starts it', { skip: process.platform !== 'win32' }, async () => {
  const dir = await mkdtemp(join(tmpdir(), 'one-search-provision-test-'));
  const releaseDir = join(dir, 'release with spaces');
  const installDir = join(dir, 'app with spaces');
  const dataDir = join(dir, 'data with spaces');
  const root = join(dir, "corpus quote ' and spaces");
  await mkdir(releaseDir);
  await mkdir(root);
  await writeFile(join(releaseDir, 'RELEASE_MANIFEST.json'), JSON.stringify({ kind: 'windows-native', version: '0.3.0' }));
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
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[1].args, ['start', '--config', join(dataDir, 'config.json')]);
  assert.equal(result.command, join(installDir, 'runtime/data-search.exe'));
  await assert.rejects(readFile(requestPath), { code: 'ENOENT' });
});

test('installer failure prevents MCP activation and cleans its temporary request', { skip: process.platform !== 'win32' }, async () => {
  const dir = await mkdtemp(join(tmpdir(), 'one-search-install-fail-test-'));
  await writeFile(join(dir, 'RELEASE_MANIFEST.json'), JSON.stringify({ kind: 'windows-native' }));
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
