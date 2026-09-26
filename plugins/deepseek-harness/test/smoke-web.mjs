/** Real installed DSH Web composition using only a fresh isolated home/config.
 * node test/smoke-web.mjs REQUEST.json
 * {dshPackage,home,command,commandArgs?,configPath,report?,keepAlive?}
 * With keepAlive, write "stop" to stdin after browser validation.
 */
import assert from 'node:assert/strict';
import { access, mkdir, readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';
import { registerBundle } from '../register.mjs';
import { runProcess } from '../bootstrap.mjs';
import { verifyWebBoundaries } from './web-boundaries.mjs';

const request = JSON.parse(await readFile(process.argv[2], 'utf8'));
for (const key of ['dshPackage', 'home', 'command', 'configPath']) assert.equal(typeof request[key], 'string');
const home = resolve(request.home);
const host = resolve(request.dshPackage);
const command = resolve(request.command);
const configPath = resolve(request.configPath);
const commandArgs = request.commandArgs || [];
const reportPath = resolve(request.report || join(home, 'web-smoke-report.json'));
const profile = 'web';
try { await access(join(home, 'profiles')); throw new Error('Use a fresh isolated DSH home'); }
catch (error) { if (error.code !== 'ENOENT') throw error; }
await mkdir(home, { recursive: true });
process.env.DSH_HOME = home;
process.env.DSH_TELEMETRY_DISABLED = '1';
const report = { schema_version: 1, tested_at_utc: new Date().toISOString(),
  host_cli_version: JSON.parse(await readFile(join(host, 'package.json'), 'utf8')).version,
  default_user_profile_modified: false, browser_opened_automatically: false, model_requests: 0 };
let application;
try {
  await registerBundle({ dshPackage: host, dshHome: home, profile, offline: true });
  await writeFile(join(home, 'profiles', profile, 'cordis.patch.yml'), JSON.stringify([
    { id: 'one-search', config: { command, commandArgs, configPath, clientId: 'dsh-web-isolated-smoke', clientLabel: 'Isolated DSH Web validation' } },
    { id: 'web-runtime', config: { openBrowser: false, printUrl: false, surfaceContext: true, trustedHosts: [] } },
  ], null, 2));
  const bootName = (await readFile(join(host, 'lib', 'bin.js'), 'utf8')).match(/import\("\.\/(profile-boot-[^"]+)"\)/)?.[1];
  if (!bootName) throw new Error('Installed DSH boot adapter changed');
  const { runProfile } = await import(pathToFileURL(join(host, 'lib', bootName)));
  const { createLaunchEnvironmentSnapshot } = await import(pathToFileURL(join(host, 'node_modules/@deepseek-ai/dsh-launch-environment/lib/index.js')));
  application = await runProfile({ environment: createLaunchEnvironmentSnapshot([{ source: 'process', values: { ...process.env } }]),
    profile, patchFiles: [], args: ['--no-open', '--port', '0'] });
  const origin = `http://127.0.0.1:${application.ctx.webServer.port}`;
  const authenticatedUrl = application.ctx.connection.authenticatedUrl(origin);
  const login = await fetch(authenticatedUrl, { redirect: 'manual' });
  assert.equal(login.status, 303);
  const cookie = login.headers.get('set-cookie')?.split(';')[0];
  assert.ok(cookie);
  const rpcBody = (action, params = {}) => JSON.stringify({ type: 'client-request', rpcId: 'one-search-web-smoke', method: 'request', payload: { action, params } });
  async function call(action, params = {}, extra = {}) {
    return fetch(origin + '/one-search/request', { method: 'POST', headers: {
      'Content-Type': 'application/json', Cookie: cookie, Origin: origin, ...extra }, body: rpcBody(action, params) });
  }
  // Optional service injection can settle after the root application is ready.
  let unauthenticated;
  for (let attempt = 0; attempt < 40; attempt++) {
    unauthenticated = await fetch(origin + '/one-search/request', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: rpcBody('status') });
    if (unauthenticated.status !== 405 && unauthenticated.status !== 404) break;
    await delay(250);
  }
  report.plugin_state = [...application.ctx.loader.entries()].find((entry) => entry.options.id === 'one-search')?.fiber.state;
  report.bridge_fibers = [...application.ctx.registry.values()].flatMap((runtime) => [...runtime.fibers])
    .filter((fiber) => Object.keys(fiber.inject).includes('connection') && Object.keys(fiber.inject).includes('webServer'))
    .map((fiber) => ({ state: fiber.state, inject: Object.keys(fiber.inject), error: fiber._error?.message,
      effects: fiber.getEffects().map((effect) => effect.label) }));
  assert.equal(unauthenticated.status, 401);
  assert.equal((await call('status', {}, { Origin: 'https://other.example' })).status, 403);
  report.unauthenticated_rejected = true;
  report.cross_origin_rejected = true;
  report.http_boundaries = await verifyWebBoundaries(origin, cookie);
  const status = await (await call('status')).json();
  assert.equal(status.result.ok, true);
  assert.equal(status.result.value.ok, true);
  assert.equal(status.result.value.result.service.status, 'running');
  assert.equal(JSON.stringify(status).includes('"token"'), false);
  report.status_succeeded = true;
  for (const [action, params] of [['pause', { seconds: 60 }], ['resume', {}], ['scan', {}], ['settings_get', {}]]) {
    const response = await (await call(action, params)).json();
    assert.equal(response.result.value.ok, true, action + ' failed');
    report[action + '_succeeded'] = true;
  }
  const graph = application.ctx.clientModules.graph();
  const client = graph.entries.find((entry) => entry.id === 'one-search-bundle');
  assert.ok(client);
  const bundle = await (await fetch(new URL(client.url, origin))).text();
  assert.match(bundle, /one-search-bundle/);
  assert.match(bundle, /sidebar\.panellist/);
  report.client_manifest_served = true;
  report.mcp_tools = application.ctx.tools.wireSchemas().schemas.map((item) => item.name)
    .filter((name) => name.startsWith('mcp__one_search__'));
  assert.equal(report.mcp_tools.length, 11);
  report.success = true;
  await writeFile(reportPath, JSON.stringify(report, null, 2) + '\n');
  if (request.keepAlive) {
    // Browser login token is test-only and kept out of reports and normal output.
    await writeFile(join(home, 'browser-url-private.json'), JSON.stringify({ url: authenticatedUrl, origin }), { mode: 0o600 });
    console.log('Isolated DSH Web ready: ' + origin + '/');
    console.log('Write stop to stdin after browser validation.');
    await new Promise((finish) => {
      process.stdin.setEncoding('utf8'); process.stdin.resume();
      const onData = (data) => { if (data.includes('stop')) { process.stdin.off('data', onData); process.stdin.pause(); finish(); } };
      process.stdin.on('data', onData);
    });
  }
} catch (error) {
  report.success = false;
  report.error_type = error.name;
  report.error_message = error.message.replace(/token=[^\s&"']+/g, 'token=[redacted]');
  process.exitCode = 1;
} finally {
  if (application) await application.shutdown.shutdown(0);
  try {
    const stopped = JSON.parse(await runProcess(command, [...commandArgs, 'stop', '--config', configPath], { timeoutMs: 30000 }));
    report.stopped_after_validation = Boolean(stopped.stopped || stopped.status === 'not_running');
  } catch { report.stopped_after_validation = false; }
  await runProcess(command, [...commandArgs, 'remove-client', 'dsh-web-isolated-smoke', '--config', configPath], { timeoutMs: 15000 }).catch(() => {});
  await writeFile(reportPath, JSON.stringify(report, null, 2) + '\n');
}
console.log(JSON.stringify(report));
