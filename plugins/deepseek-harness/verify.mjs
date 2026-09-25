/** Verify DSH tool registration and read-only backend calls, without a chat model. */
import { mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { runProcess } from './bootstrap.mjs';
import { registerBundle } from './register.mjs';

const requestPath = process.argv[2];
if (!requestPath) throw new Error('Usage: node verify.mjs REQUEST.json; README documents its fields');
const request = JSON.parse(await readFile(requestPath, 'utf8'));
for (const field of ['dshPackage', 'command', 'configPath']) {
  if (typeof request[field] !== 'string') throw new Error('Required request field: ' + field);
}
const host = resolve(request.dshPackage);
const home = await mkdtemp(join(tmpdir(), 'one-search-dsh-check-'));
const profile = 'one-search-connection-check';
const clientId = 'dsh-check-' + createHash('sha256').update(home).digest('hex').slice(0, 24);
process.env.DSH_HOME = home;
process.env.DSH_TELEMETRY_DISABLED = '1';
const report = { schema_version: 1, host_cli_version: JSON.parse(await readFile(join(host, 'package.json'), 'utf8')).version,
  checked_at: new Date().toISOString(), dsh_home: home, default_user_profile_modified: false,
  chat_model_called: false, answer_quality_tested: false, connected: false, tools: [], calls: {} };
let application;
let stage = 'registration';
try {
  await registerBundle({ dshPackage: host, dshHome: home, profile, offline: true });
  await writeFile(join(home, 'profiles', profile, 'cordis.patch.yml'), JSON.stringify([{ id: 'one-search', config: {
    command: resolve(request.command), commandArgs: request.commandArgs || [], configPath: resolve(request.configPath),
    clientId, clientLabel: 'Temporary DSH connection check',
  } }], null, 2));
  stage = 'host_boot';
  const bootName = (await readFile(join(host, 'lib', 'bin.js'), 'utf8')).match(/import\("\.\/(profile-boot-[^"]+)"\)/)?.[1];
  if (!bootName) throw new Error('Installed DSH boot entry changed; revalidate the connection checker');
  const { runProfile } = await import(pathToFileURL(join(host, 'lib', bootName)));
  const { createLaunchEnvironmentSnapshot } = await import(pathToFileURL(join(host, 'node_modules/@deepseek-ai/dsh-launch-environment/lib/index.js')));
  application = await runProfile({ environment: createLaunchEnvironmentSnapshot([{ source: 'process', values: { ...process.env } }]),
    profile, patchFiles: [], args: [] });
  stage = 'tool_discovery';
  report.tools = application.ctx.tools.wireSchemas().schemas.map((item) => item.name).filter((name) => name.startsWith('mcp__one_search__'));
  const required = ['search', 'fetch', 'inspect_source', 'query_database', 'index_status', 'diagnose_path',
    'read_context', 'refresh_path', 'prioritize_path', 'pause_indexing', 'resume_indexing'];
  if (!required.every((name) => report.tools.includes('mcp__one_search__' + name))) throw new Error('DSH did not register every required search tool');
  for (const [name, args] of [['index_status', {}], ['search', { query: 'one_search_installation_probe_93c2f7', mode: 'files', limit: 1 }]]) {
    stage = name;
    const result = await application.ctx.tools.execute({ name: 'mcp__one_search__' + name, arguments: args,
      callId: 'installation-check-' + name, signal: new AbortController().signal });
    report.calls[name] = { ok: result.isError !== true };
    if (result.isError) throw new Error('DSH backend call failed: ' + name);
  }
  report.connected = true;
  report.ok = true;
} catch (error) {
  report.ok = false;
  report.error = { code: 'dsh_connection_check_failed', type: error.name, stage,
    message: 'Inspect DSH version, backend command/configuration and plugin dependencies; registration requires a populated package cache' };
  process.exitCode = 1;
} finally {
  if (application) await application.shutdown.shutdown(0);
  try {
    await runProcess(resolve(request.command), [...(request.commandArgs || []), 'remove-client', clientId, '--config', resolve(request.configPath)], { timeoutMs: 15000 });
    report.diagnostic_registration_removed = true;
  } catch { report.diagnostic_registration_removed = false; }
  if (request.report) await writeFile(resolve(request.report), JSON.stringify(report, null, 2) + '\n');
}
console.log(JSON.stringify(report));
