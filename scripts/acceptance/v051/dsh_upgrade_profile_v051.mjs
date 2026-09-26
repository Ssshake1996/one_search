/** Isolated real DSH profile controlled by newline JSON on stdin.
 * node dsh_upgrade_profile_v051.mjs REQUEST.json
 * Request: {fixtureRoot,dshPackage,bundleDir,home,profile,command,commandArgs?,configPath}
 * This worker never stops the shared backend. The owning acceptance harness
 * disposes every profile before cleaning up that exact synthetic installation.
 */
import assert from 'node:assert/strict';
import childProcess from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
import { access, mkdir, readFile, realpath, writeFile } from 'node:fs/promises';
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath, pathToFileURL } from 'node:url';

const prefix = '@@ONE_SEARCH_ACCEPTANCE@@';
const emit = (message) => process.stdout.write(prefix + JSON.stringify(message) + '\n');
const sanitize = (error) => ({ name: error.name || 'Error', message: String(error.message || error)
  .replace(/token=[^\s&"']+/g, 'token=[redacted]').replace(/Bearer\s+[^\s"']+/g, 'Bearer [redacted]').slice(-2500) });
const request = JSON.parse(await readFile(process.argv[2], 'utf8'));
const helperDirectory = dirname(fileURLToPath(import.meta.url));
const repoDirectory = resolve(helperDirectory, '..', '..', '..');
const acceptanceDirectory = join(repoDirectory, '.packaging-smoke');
function inside(path, parent, label) {
  const part = relative(resolve(parent), resolve(path));
  assert.ok(part && !isAbsolute(part) && part !== '..' && !part.startsWith('..' + sep), label);
}
for (const name of ['fixtureRoot', 'dshPackage', 'bundleDir', 'home', 'profile', 'command', 'configPath']) {
  assert.equal(typeof request[name], 'string', name);
  assert.ok(request[name], name);
}
const fixtureRoot = resolve(request.fixtureRoot);
inside(fixtureRoot, acceptanceDirectory, 'Only a new .packaging-smoke fixture is accepted');
assert.ok(relative(acceptanceDirectory, fixtureRoot).startsWith('upgrade-v051-'));
for (const name of ['home', 'configPath']) inside(request[name], fixtureRoot, name);
const home = resolve(request.home);
const command = await realpath(request.command);
const configPath = await realpath(request.configPath);
const commandArgs = request.commandArgs || [];
const sourcePython = await realpath(join(repoDirectory, '.venv', 'Scripts', 'python.exe'));
if (command === sourcePython) assert.deepEqual(commandArgs, ['-m', 'data_search']);
else {
  inside(command, await realpath(fixtureRoot), 'The runtime cannot redirect outside the fixture');
  assert.deepEqual(commandArgs, []);
}
inside(configPath, await realpath(fixtureRoot), 'The configuration cannot redirect outside the fixture');
const config = JSON.parse(await readFile(configPath, 'utf8'));
assert.equal(config.scope, 'directories');
assert.equal(config.semantic.enabled, false);
assert.ok(config.roots.length > 0);
for (const root of config.roots) inside(await realpath(root), await realpath(fixtureRoot), 'Synthetic roots only');
assert.match(request.profile, /^[a-zA-Z0-9_-]{1,80}$/);
try {
  await access(home);
  assert.equal(request.preRegistered, true, 'Each DSH worker requires a fresh isolated home');
  await access(join(home, 'profiles', request.profile, 'package.json'));
} catch (error) {
  if (error.code !== 'ENOENT' || request.preRegistered) throw error;
}
await mkdir(home, { recursive: true });
process.env.DSH_HOME = home;
process.env.DSH_TELEMETRY_DISABLED = '1';
const host = resolve(request.dshPackage);
const source = resolve(request.bundleDir);
assert.match(request.instanceName || request.profile, /^[a-zA-Z0-9_-]{1,80}$/);
const clientId = 'upgrade-acceptance-' + (request.instanceName || request.profile);
let application, reader, shutdownRequested = false;

// Observe only our exact fixture runtime's short bootstrap commands. Keeping
// this in the helper captures async resume failures without changing the plugin.
const originalSpawn = childProcess.spawn;
childProcess.spawn = function (file, args, options) {
  const child = originalSpawn.call(this, file, args, options);
  if (resolve(file) === command && args?.some((part) => ['version', 'start', 'register-client'].includes(part))) {
    let output = '';
    const collect = (chunk) => { output = (output + chunk.toString('utf8')).slice(-4000); };
    child.stdout?.on('data', collect);
    child.stderr?.on('data', collect);
    child.on('close', (code) => {
      if (code !== 0) emit({ event: 'diagnostic', operation: args.find((part) => ['version', 'start', 'register-client'].includes(part)),
        returncode: code, error: sanitize({ message: output }) });
    });
  }
  return child;
};
syncBuiltinESMExports();

try {
  const { registerBundle } = await import(pathToFileURL(join(source, 'register.mjs')));
  if (!request.preRegistered) await registerBundle({ dshPackage: host, dshHome: home, profile: request.profile, offline: request.offline === true });
  await writeFile(join(home, 'profiles', request.profile, 'cordis.patch.yml'), JSON.stringify([
    { id: 'one-search', config: { command, commandArgs, configPath, clientId, clientLabel: clientId } },
    { id: 'web-runtime', config: { openBrowser: false, printUrl: false, surfaceContext: true, trustedHosts: [] } },
  ], null, 2));
  const bootName = (await readFile(join(host, 'lib', 'bin.js'), 'utf8')).match(/import\("\.\/(profile-boot-[^"]+)"\)/)?.[1];
  assert.ok(bootName, 'Installed DSH boot adapter changed');
  const { runProfile } = await import(pathToFileURL(join(host, 'lib', bootName)));
  const { createLaunchEnvironmentSnapshot } = await import(pathToFileURL(join(host, 'node_modules/@deepseek-ai/dsh-launch-environment/lib/index.js')));
  application = await runProfile({
    environment: createLaunchEnvironmentSnapshot([{ source: 'process', values: { ...process.env } }]),
    profile: request.profile, patchFiles: [], args: ['--no-open', '--port', '0'],
  });
  const origin = `http://127.0.0.1:${application.ctx.webServer.port}`;
  const login = await fetch(application.ctx.connection.authenticatedUrl(origin), { redirect: 'manual' });
  assert.equal(login.status, 303);
  const cookie = login.headers.get('set-cookie')?.split(';')[0];
  assert.ok(cookie);
  const tools = () => application.ctx.tools.wireSchemas().schemas.map((tool) => tool.name)
    .filter((name) => name.startsWith('mcp__one_search__')).sort();

  async function dispatch(message) {
    assert.equal(typeof message.id, 'string');
    if (message.action === 'state') return { profile: request.profile, mcp_tools: tools() };
    if (message.action === 'web_action') {
      assert.equal(typeof message.webAction, 'string');
      const response = await fetch(origin + '/one-search/request', { method: 'POST',
        headers: { 'Content-Type': 'application/json', Cookie: cookie, Origin: origin },
        body: JSON.stringify({ type: 'client-request', rpcId: 'acceptance-' + message.id,
          method: 'request', payload: { action: message.webAction, params: message.params || {} } }),
        signal: AbortSignal.timeout(30000) });
      return { status: response.status, envelope: await response.json() };
    }
    if (message.action === 'mcp_status' || message.action === 'mcp_search') {
      const name = message.action === 'mcp_status' ? 'index_status' : 'search';
      const args = name === 'search' ? { query: message.query || 'upgradefixture051', mode: 'keyword', limit: 5 } : {};
      const result = await application.ctx.tools.execute({ name: 'mcp__one_search__' + name,
        arguments: args, callId: 'acceptance-' + message.id, signal: AbortSignal.timeout(25000) });
      const serialized = JSON.stringify(result);
      assert.ok(serialized.length < 256 * 1024, 'Unexpected oversized synthetic tool result');
      return { isError: Boolean(result.isError), contains_query: name === 'search' && serialized.includes(args.query), result };
    }
    if (message.action === 'stop') {
      shutdownRequested = true;
      await application.shutdown.shutdown(0);
      application = undefined;
      return { profile_disposed: true, shared_daemon_stop_requested: false };
    }
    throw new Error('Unknown acceptance control action');
  }

  emit({ event: 'ready', profile: request.profile, origin, mcp_tools: tools(),
    default_profile_modified: false, model_requests: 0 });
  reader = createInterface({ input: process.stdin, crlfDelay: Infinity });
  let queue = Promise.resolve();
  reader.on('line', (line) => {
    queue = queue.then(async () => {
      if (shutdownRequested) return;
      let message;
      try {
        assert.ok(Buffer.byteLength(line) <= 128 * 1024);
        message = JSON.parse(line);
        const result = await dispatch(message);
        emit({ id: message.id, ok: true, result });
      } catch (error) { emit({ id: message?.id || null, ok: false, error: sanitize(error) }); }
      if (shutdownRequested) reader.close();
    });
  });
  await new Promise((finish) => reader.once('close', finish));
  await queue;
} catch (error) {
  emit({ event: 'failed', error: sanitize(error) });
  process.exitCode = 1;
} finally {
  if (application) await application.shutdown.shutdown(0);
  reader?.close();
  // Keep the backend alive; other isolated profiles may still use it. Cleanup
  // belongs to the parent harness after every profile has been disposed.
}
