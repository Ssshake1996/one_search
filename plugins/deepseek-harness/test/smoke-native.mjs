/**
 * Live smoke against a freshly built Windows release and an isolated DSH home.
 * Usage: node test/smoke-native.mjs REQUEST.json
 * Request: {dshPackage, releaseDir, home, profile?, report?}.
 * No model requests, default profiles, login startup, or whole-machine scans.
 */
import assert from 'node:assert/strict';
import { access, mkdir, readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';
import { runProcess } from '../bootstrap.mjs';

if (process.platform !== 'win32') throw new Error('This smoke requires the Windows native release');
const request = JSON.parse(await readFile(process.argv[2], 'utf8'));
for (const name of ['dshPackage', 'releaseDir', 'home']) {
  if (!request[name]) throw new Error('Missing request field: ' + name);
}
const host = resolve(request.dshPackage);
const release = resolve(request.releaseDir);
const home = resolve(request.home);
const profile = request.profile || 'one-search-native-validation';
const reportPath = resolve(request.report || join(home, 'native-smoke-report.json'));
const app = join(home, 'app');
const data = join(home, 'data');
const corpus = join(home, 'synthetic-corpus');
const alternative = join(home, 'alternative-synthetic-corpus');
const nativeCli = join(app, 'runtime', 'data-search.exe');
const configPath = join(data, 'config.json');
const exists = async (path) => { try { await access(path); return true; } catch { return false; } };
if (await exists(app) || await exists(data)) throw new Error('Fresh isolated app/data directories are required');
const releaseManifest = JSON.parse(await readFile(join(release, 'RELEASE_MANIFEST.json'), 'utf8'));
assert.equal(releaseManifest.kind, 'windows-native');
await mkdir(corpus, { recursive: true });
await mkdir(alternative, { recursive: true });
await writeFile(join(corpus, 'native-dsh-fixture.txt'), 'Native DSH synthetic token nativedshvalidation731.', 'utf8');
process.env.DSH_HOME = home;
process.env.DSH_TELEMETRY_DISABLED = '1';
const options = { releaseDir: release, installDir: app, dataDir: data, roots: [corpus], skipModel: true, noAutostart: true };
const report = {
  schema_version: 1, tested_at_utc: new Date().toISOString(), release_version: releaseManifest.version,
  host_cli_version: JSON.parse(await readFile(join(host, 'package.json'), 'utf8')).version,
  scope: 'one explicitly selected synthetic directory',
  default_user_profile_modified: false, login_autostart_requested: false,
  model_download_requested: false, model_requests_in_test: 0,
};
let application;
try {
  await runProcess(process.execPath, [join(host, 'lib', 'bin.js'), 'plugin', '--profile', profile,
    'add', 'file:' + join(release, 'plugins', 'deepseek-harness'), '--ignore-scripts', '--offline',
    '--registry=https://registry.npmjs.org'], { timeoutMs: 120000 });
  report.registration_with_scripts_disabled = true;
  const profileDir = join(home, 'profiles', profile);
  const patch = join(profileDir, 'cordis.patch.yml');
  await writeFile(patch, JSON.stringify([{ id: 'one-search', config: options }], null, 2));
  const bootName = (await readFile(join(host, 'lib', 'bin.js'), 'utf8'))
    .match(/import\("\.\/(profile-boot-[^"]+)"\)/)?.[1];
  if (!bootName) throw new Error('Installed DSH boot entry changed; revalidate the host test adapter');
  const { runProfile } = await import(pathToFileURL(join(host, 'lib', bootName)));
  const { createLaunchEnvironmentSnapshot } = await import(pathToFileURL(
    join(host, 'node_modules/@deepseek-ai/dsh-launch-environment/lib/index.js')));
  const boot = () => runProfile({
    environment: createLaunchEnvironmentSnapshot([{ source: 'process', values: { ...process.env } }]),
    profile, patchFiles: [], args: [],
  });
  application = await boot();
  assert.ok(await exists(nativeCli));
  const configBytes = await readFile(configPath, 'utf8');
  const config = JSON.parse(configBytes);
  assert.equal(config.scope, 'directories');
  assert.deepEqual(config.roots.map((path) => resolve(path)), [corpus]);
  assert.equal(config.semantic.enabled, false);
  report.first_activation_provisioned_native_backend = true;
  report.scope_preserved = true;
  report.tools = application.ctx.tools.wireSchemas().schemas.map((s) => s.name)
    .filter((name) => name.startsWith('mcp__one_search__')).sort();
  assert.equal(report.tools.length, 5);
  async function call(name, args) {
    const result = await application.ctx.tools.execute({
      name: 'mcp__one_search__' + name, arguments: args, callId: 'native-smoke-' + name,
      signal: new AbortController().signal,
    });
    assert.equal(result.isError, false, JSON.stringify(result));
    return result;
  }
  report.index_status_succeeded = Boolean(await call('index_status', {}));
  let search;
  for (let attempt = 0; attempt < 45; attempt++) {
    search = await call('search', { query: 'nativedshvalidation731', mode: 'keyword', limit: 5 });
    if (JSON.stringify(search).includes('native-dsh-fixture.txt')) break;
    await delay(1000);
  }
  assert.ok(JSON.stringify(search).includes('native-dsh-fixture.txt'));
  report.synthetic_keyword_search_succeeded = true;
  const entry = [...application.ctx.loader.entries()].find((e) => e.options.id === 'one-search');
  await entry.fiber.dispose();
  report.tools_after_dispose = application.ctx.tools.wireSchemas().schemas.map((s) => s.name)
    .filter((name) => name.startsWith('mcp__one_search__'));
  assert.deepEqual(report.tools_after_dispose, []);
  const retained = JSON.parse(await runProcess(nativeCli, ['start', '--config', configPath]));
  assert.equal(retained.started, false);
  report.daemon_survived_plugin_dispose = true;
  await application.shutdown.shutdown(0);
  application = undefined;
  // Re-activation with different install-time choices must preserve saved scope.
  await writeFile(patch, JSON.stringify([{ id: 'one-search', config: {
    ...options, roots: [alternative], skipModel: false,
  } }], null, 2));
  application = await boot();
  assert.equal(await readFile(configPath, 'utf8'), configBytes);
  report.repeat_activation_preserved_config = true;
  report.success = true;
} catch (error) {
  report.success = false;
  report.error_type = error.name;
  throw error;
} finally {
  if (application) await application.shutdown.shutdown(0);
  if (await exists(nativeCli) && await exists(configPath)) {
    const stopped = JSON.parse(await runProcess(nativeCli, ['stop', '--config', configPath], { timeoutMs: 30000 }));
    report.daemon_stopped = Boolean(stopped.stopped || stopped.status === 'not_running');
  }
  await writeFile(reportPath, JSON.stringify(report, null, 2) + '\n');
}
console.log(JSON.stringify(report));
