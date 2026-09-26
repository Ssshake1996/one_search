import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { access, mkdtemp, readFile, rmdir, unlink, writeFile } from 'node:fs/promises';
import { constants } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const moduleDir = dirname(fileURLToPath(import.meta.url));
const requiredBackendVersion = '0.5.0';
export function backendCompatible(version) {
  const parts = typeof version === 'string' && version.match(/^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$/);
  return Boolean(parts && Number(parts[1]) === 0 && Number(parts[2]) >= 5);
}
const exists = async (path) => {
  try { await access(path, constants.F_OK); return true; } catch { return false; }
};

export function settings(config = {}, environment = process.env, platform = process.platform) {
  if (!['win32', 'linux'].includes(platform)) throw new Error('one-search supports Windows and Linux');
  const userData = platform === 'win32' ? environment.LOCALAPPDATA : (environment.XDG_DATA_HOME || join(homedir(), '.local', 'share'));
  if (!userData && (!config.installDir || !config.dataDir)) throw new Error('LOCALAPPDATA is unavailable; configure installDir and dataDir explicitly');
  const installDir = resolve(config.installDir || join(userData, 'data-search', 'app'));
  const dataDir = resolve(config.dataDir || join(userData, 'data-search', 'data'));
  const configPath = resolve(config.configPath || join(dataDir, 'config.json'));
  const roots = config.roots ?? [];
  if (!Array.isArray(roots) || roots.some((p) => typeof p !== 'string' || !isAbsolute(p))) {
    throw new Error('roots must be an array of absolute directories; [] means whole machine on first installation');
  }
  const excludePaths = config.excludePaths ?? [];
  if (!Array.isArray(excludePaths) || excludePaths.some((p) => typeof p !== 'string' || !isAbsolute(p))) throw new Error('excludePaths must contain absolute paths');
  const preset = config.preset || 'balanced';
  if (!['low', 'balanced', 'fast'].includes(preset)) throw new Error('preset must be low, balanced or fast');
  if (!Array.isArray(config.commandArgs ?? []) || (config.commandArgs ?? []).some((x) => typeof x !== 'string')) {
    throw new Error('commandArgs must contain strings');
  }
  if (config.command && !isAbsolute(config.command)) throw new Error('command must be an absolute executable path');
  if (config.command && !config.configPath) throw new Error('An explicit command requires an existing configPath');
  const serverName = config.serverName || 'one_search';
  if (!/^[A-Za-z0-9_-]{1,32}$/.test(serverName)) throw new Error('Invalid MCP serverName');
  if (config.skipModel && config.modelDir) throw new Error('Choose skipModel or modelDir, not both');
  if (!Number.isInteger(config.timeoutMs ?? 900000) || (config.timeoutMs ?? 900000) < 1000 || (config.timeoutMs ?? 900000) > 3600000) {
    throw new Error('timeoutMs must be an integer between 1000 and 3600000');
  }
  const clientId = config.clientId || 'dsh-' + createHash('sha256').update(resolve(environment.DSH_HOME || join(homedir(), '.dsh')) + ':' + serverName).digest('hex').slice(0, 24);
  const clientLabel = config.clientLabel || 'DeepSeek Harness (profile unspecified)';
  for (const value of [clientId, clientLabel]) {
    if (typeof value !== 'string' || !value || value.length > 160 || /[\u0000-\u001f]/.test(value)) throw new Error('clientId and clientLabel must be bounded plain text');
  }
  return {
    platform, installDir, dataDir, configPath, roots, excludePaths, preset, serverName, clientId, clientLabel,
    command: config.command, commandArgs: config.commandArgs || [],
    releaseDir: config.releaseDir || environment.ONE_SEARCH_RELEASE_DIR,
    modelDir: config.modelDir || '', skipModel: config.skipModel === true,
    noAutostart: config.noAutostart === true,
    timeoutMs: config.timeoutMs ?? 900000,
  };
}

export async function findRelease(explicit, starts = [moduleDir, process.cwd()]) {
  const candidates = [];
  if (explicit) candidates.push(resolve(explicit));
  else for (const start of starts) {
    let current = resolve(start);
    for (let depth = 0; depth < 8; depth++) {
      candidates.push(current);
      if (dirname(current) === current) break;
      current = dirname(current);
    }
  }
  for (const directory of new Set(candidates)) {
    const manifestPath = join(directory, 'RELEASE_MANIFEST.json');
    if (!await exists(manifestPath)) continue;
    const manifest = JSON.parse(await readFile(manifestPath, 'utf8'));
    if (!['windows-native', 'python-bootstrap'].includes(manifest.kind)) {
      throw new Error('Unsupported one_search release manifest');
    }
    return { directory, manifest };
  }
  throw new Error('one_search runtime is not installed. Start DSH from the extracted release directory, or set ONE_SEARCH_RELEASE_DIR / releaseDir to that directory.');
}

export function runProcess(command, args, { timeoutMs = 900000 } = {}) {
  return new Promise((fulfill, reject) => {
    const child = spawn(command, args, { windowsHide: true, shell: false, stdio: ['ignore', 'pipe', 'pipe'] });
    let output = '';
    const collect = (chunk) => { output = (output + chunk.toString('utf8')).slice(-16384); };
    child.stdout.on('data', collect);
    child.stderr.on('data', collect);
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error('one_search setup command timed out; inspect its installation before retrying'));
    }, timeoutMs);
    child.on('error', (error) => { clearTimeout(timer); reject(error); });
    child.on('close', (code) => {
      clearTimeout(timer);
      if (code === 0) fulfill(output);
      else reject(new Error(`one_search setup command failed (${code}): ${output.slice(-2000)}`));
    });
  });
}

async function installedCommand(options) {
  const candidates = options.platform === 'win32'
    ? [join(options.installDir, 'runtime', 'data-search.exe'), join(options.installDir, 'venv', 'Scripts', 'data-search.exe')]
    : [join(options.installDir, 'venv', 'bin', 'data-search')];
  for (const path of candidates) if (await exists(path)) return path;
  return null;
}

async function install(options, run) {
  const release = await findRelease(options.releaseDir);
  if (!backendCompatible(release.manifest.version)) throw new Error('backend_update_required: use a verified one_search release >= ' + requiredBackendVersion);
  if (options.platform === 'win32') {
    const directory = await mkdtemp(join(tmpdir(), 'one-search-dsh-'));
    const requestPath = join(directory, 'install.json');
    try {
      await writeFile(requestPath, JSON.stringify({ ...options, releaseDir: release.directory }), { mode: 0o600 });
      await run(join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'),
        ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', join(moduleDir, 'install-wrapper.ps1'), '-RequestPath', requestPath], options);
    } finally {
      await unlink(requestPath).catch(() => {});
      await rmdir(directory).catch(() => {});
    }
  } else {
    const args = [join(release.directory, 'scripts', 'install.sh'), '--install-dir', options.installDir, '--data-dir', options.dataDir];
    for (const root of options.roots) args.push('--root', root);
    for (const path of options.excludePaths) args.push('--exclude', path);
    args.push('--preset', options.preset);
    if (options.skipModel) args.push('--skip-model');
    if (options.modelDir) args.push('--model-dir', options.modelDir);
    if (options.noAutostart) args.push('--no-autostart');
    await run('bash', args, options);
  }
}

export async function prepareService(config = {}, run = runProcess) {
  const options = settings(config);
  let command = options.command || await installedCommand(options);
  if (options.command) {
    if (!await exists(command) || !await exists(options.configPath)) {
      throw new Error('Explicit executable/configuration does not exist; one_search will not overwrite it');
    }
    let info;
    try { info = JSON.parse(await run(command, [...options.commandArgs, 'version', '--config', options.configPath], options)); }
    catch { throw new Error('backend_update_required: update the explicitly configured backend with the matching release installer; then retry DSH'); }
    if (!backendCompatible(info.version)) throw new Error('backend_update_required: the explicitly configured backend must be >= ' + requiredBackendVersion);
  } else if (!command || !await exists(options.configPath)) {
    await install(options, run);
    command = await installedCommand(options);
    if (!command || !await exists(options.configPath)) throw new Error('one_search installer did not produce an executable and configuration');
  } else {
    let installed = {};
    try { installed = JSON.parse(await readFile(join(options.installDir, 'install-manifest.json'), 'utf8')); } catch { /* Legacy manifest is treated as requiring upgrade. */ }
    if (!backendCompatible(installed.version)) {
      try { await findRelease(options.releaseDir); }
      catch { throw new Error('backend_update_required: extract the matching release and set ONE_SEARCH_RELEASE_DIR before starting DSH, or run its installer first'); }
      await install(options, run);
      command = await installedCommand(options);
      const upgraded = JSON.parse(await readFile(join(options.installDir, 'install-manifest.json'), 'utf8'));
      if (!command || !backendCompatible(upgraded.version)) throw new Error('backend_update_required: installer did not publish a compatible backend version');
    }
  }
  // start is idempotent. Existing configuration, search scope, and model settings
  // remain owned by the backend; profile activation never rewrites them.
  await run(command, [...options.commandArgs, 'start', '--config', options.configPath], options);
  await run(command, [...options.commandArgs, 'register-client', options.clientId, '--label', options.clientLabel,
    '--kind', 'dsh', '--config', options.configPath], options);
  return {
    transport: 'stdio', serverName: options.serverName, command,
    args: [...options.commandArgs, 'mcp', '--config', options.configPath],
    env: {}, cwd: dirname(options.configPath), toolCallTimeoutMs: 60000, failOnStartupError: true,
    reconnect: { enabled: true, initialDelayMs: 500, maxDelayMs: 30000, maxAttempts: 10 },
  };
}
