/** Node-owned maintenance gate: stays alive while the replaceable runtime is stopped. */
import { randomBytes, randomUUID, timingSafeEqual } from 'node:crypto';
import { lstat, mkdir, readFile, rename, stat, unlink, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { readServiceControl, retryDelay } from './service-control.mjs';

const MAX_BODY = 4096;
export class MaintenanceError extends Error {
  constructor() { super('upgrade_in_progress: one_search is waiting for its installer'); this.code = 'upgrade_in_progress'; }
}

async function smallJson(path, limit) {
  if ((await stat(path)).size > limit) throw new Error('File exceeds size limit');
  return JSON.parse((await readFile(path, 'utf8')).replace(/^\uFEFF/, ''));
}

/** Read configuration only; resolving the registration directory never loads the runtime. */
export async function coordinatorOptions(options) {
  let dataDir = options.dataDir;
  try {
    const config = await smallJson(options.configPath, 1024 * 1024);
    if (typeof config.data_dir === 'string' && config.data_dir) {
      dataDir = resolve(config.data_dir.replace(/^~(?=$|[\\/])/, homedir()));
    }
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  return { ...options, dataDir };
}

/** Registration precedes the first marker check, closing the new-profile admission race. */
export async function createUpgradeCoordinator(options, { pollMs = 5000, onError = () => {},
  superviseService = false, probeEveryMs = 5000, initialRetryMs = 1000, maximumRetryMs = 60000,
  now = Date.now, random = Math.random } = {}) {
  const instanceId = randomUUID();
  const token = randomBytes(32).toString('hex');
  const registryDir = join(options.dataDir, 'host-clients');
  const registrationPath = join(registryDir, instanceId + '.json');
  const markerPath = join(options.dataDir, 'upgrade-state.json');
  let disposed = false;
  let blocked = false;
  let state = 'starting';
  let marker = null;
  let fiber;
  let connector;
  let starting;
  let preparing;
  let disposing;
  let draining;
  let drainingMcp;
  let resumeAfterStartup = false;
  let lastError = null;
  let control = { desired_state: 'running', revision: 'initial', reason: null, updated_at: null };
  let attempts = 0, retryAt = null, probeAt = 0, probe;
  let syncing;
  const jobs = new Set();

  async function readMarker() {
    try {
      const value = await smallJson(markerPath, 16384);
      return { transaction_id: typeof value.transaction_id === 'string' ? value.transaction_id : null };
    } catch (error) {
      if (error.code === 'ENOENT') return null;
      // A partial, inaccessible or corrupt marker cannot grant permission to restart.
      return { transaction_id: null };
    }
  }
  const snapshot = () => ({ schema_version: 1, instance_id: instanceId, state,
    maintenance: blocked || marker !== null, transaction_id: marker?.transaction_id ?? null, last_error: lastError,
    ...(superviseService ? { control, reconnect: { state: disposed ? 'stopped' : blocked || marker ? 'maintenance'
      : control.desired_state === 'stopped' ? 'stopped' : retryAt !== null ? 'waiting' : state,
      attempt: attempts, next_retry_at: retryAt === null ? null : retryAt / 1000 } } : {}) });
  function scheduleRetry() {
    if (!superviseService || disposed || blocked || marker || control.desired_state === 'stopped') return;
    attempts++;
    retryAt = now() + retryDelay(attempts, { initialMs: initialRetryMs, maximumMs: maximumRetryMs, random });
  }
  async function drainMcp() {
    if (drainingMcp) { await drainingMcp; if (fiber) return drainMcp(); return; }
    const current = fiber;
    if (current) {
      drainingMcp = (async () => {
        await current.dispose();
        if (fiber === current) fiber = undefined;
      })().finally(() => { drainingMcp = undefined; });
      await drainingMcp;
    }
  }
  async function syncService() {
    if (!superviseService || disposed) return snapshot();
    if (syncing) return syncing;
    syncing = (async () => {
      const previous = control;
      control = await readServiceControl(options.dataDir);
      if (control.desired_state === 'stopped') {
        retryAt = null; attempts = 0;
        if (!blocked && !marker) state = 'stopped';
        await drainMcp();
      } else if (previous.desired_state === 'stopped') {
        retryAt = null; attempts = 0;
        if (!blocked && !marker) {
          state = 'starting';
          if (starting) resumeAfterStartup = true;
          // A scheduled/manual launcher may still own its lifecycle lock. Do
          // not wait for it through a synchronous host notification callback.
          queueMicrotask(() => { void start().catch(() => {}); });
        }
      }
      return snapshot();
    })().finally(() => { syncing = undefined; });
    return syncing;
  }
  async function runRuntime(callback) {
    marker = await readMarker();
    if (disposed || blocked || marker) {
      if (marker) { blocked = true; state = 'maintenance'; }
      throw new MaintenanceError();
    }
    // No await between admission, launching the operation and recording its lifetime.
    const operation = callback();
    const result = Promise.resolve(operation);
    const lifetime = result.catch(() => {}).then(() => operation?.closed).finally(() => jobs.delete(lifetime));
    jobs.add(lifetime);
    return result;
  }
  function drain() {
    if (draining) return draining;
    draining = (async () => {
      await drainMcp();
      while (jobs.size) await Promise.all([...jobs]);
    })().finally(() => { draining = undefined; });
    return draining;
  }
  async function prepare(transactionId) {
    const current = await readMarker();
    if (!current || (transactionId !== undefined && transactionId !== current.transaction_id)) {
      throw new Error('maintenance_marker_mismatch');
    }
    marker = current;
    blocked = true;
    retryAt = null;
    state = 'maintenance';
    if (!preparing) {
      preparing = drain().finally(() => { preparing = undefined; });
    }
    await preparing;
    return snapshot();
  }
  async function start() {
    if (disposed) return snapshot();
    if (superviseService) {
      control = await readServiceControl(options.dataDir);
      if (control.desired_state === 'stopped') {
        retryAt = null; attempts = 0; state = 'stopped'; await drainMcp(); return snapshot();
      }
    }
    marker = await readMarker();
    if (marker || blocked) { blocked = true; state = 'maintenance'; return snapshot(); }
    if (starting || state === 'ready') return snapshot();
    state = 'starting';
    lastError = null;
    const launchRevision = control.revision;
    const task = (async () => {
      try {
        await connector?.();
        if (superviseService) control = await readServiceControl(options.dataDir);
        if (control.desired_state === 'stopped') { state = 'stopped'; await drainMcp(); }
        else if (superviseService && launchRevision !== control.revision) {
          await drainMcp(); state = 'starting'; resumeAfterStartup = true;
        }
        else if (!disposed && !blocked) { state = 'ready'; attempts = 0; retryAt = null; probeAt = now() + probeEveryMs; }
      } catch (error) {
        if (error instanceof MaintenanceError || blocked) state = 'maintenance';
        else {
          state = 'failed';
          lastError = { code: 'host_startup_failed' };
          if (['version', 'start', 'register-client'].includes(error.operation)) lastError.operation = error.operation;
          if (typeof error.backendCode === 'string' && /^[A-Za-z0-9_]{1,80}$/.test(error.backendCode)) {
            lastError.backend_code = error.backendCode;
          }
          // Keep raw subprocess output, paths and credentials out of status/logs.
          try { onError({ ...lastError }); } catch { /* Diagnostics cannot prevent disposal. */ }
          await drain();
          if (superviseService) control = await readServiceControl(options.dataDir);
          if (control.desired_state === 'stopped') state = 'stopped';
          scheduleRetry();
          throw error;
        }
      } finally {
        if (starting === task) starting = undefined;
        const retry = resumeAfterStartup && !disposed && !blocked && state !== 'ready';
        resumeAfterStartup = false;
        if (retry) queueMicrotask(() => { void start().catch(() => {}); });
      }
    })();
    starting = task;
    await task;
    return snapshot();
  }
  async function resume() {
    marker = await readMarker();
    if (marker || disposed) return snapshot();
    if (preparing) await preparing;
    // Recheck after draining: a second installer may have acquired maintenance meanwhile.
    marker = await readMarker();
    if (marker || disposed) return snapshot();
    blocked = false;
    if (state === 'maintenance' || state === 'failed') state = 'starting';
    if (starting) resumeAfterStartup = true;
    // Do not await the installer-owning startup promise: an automatic first install
    // can notify this endpoint before its shell process exits.
    void start().catch(() => {});
    return snapshot();
  }
  const server = createServer(async (request, response) => {
    const send = (statusCode, value) => {
      if (!response.destroyed) {
        response.writeHead(statusCode, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
        response.end(JSON.stringify(value));
      }
    };
    const auth = Buffer.from(request.headers.authorization || '');
    const expected = Buffer.from('Bearer ' + token);
    if (auth.length !== expected.length || !timingSafeEqual(auth, expected) || request.headers.origin) {
      request.resume(); send(401, { ok: false, error: { code: 'unauthorized' } }); return;
    }
    if (request.method !== 'POST' || request.url !== '/control') {
      request.resume(); send(404, { ok: false, error: { code: 'not_found' } }); return;
    }
    let timer = setTimeout(() => request.destroy(), 5000);
    try {
      if (Number(request.headers['content-length']) > MAX_BODY) throw new Error('invalid_request');
      let size = 0;
      const chunks = [];
      for await (const chunk of request) {
        size += chunk.length;
        if (size > MAX_BODY) throw new Error('invalid_request');
        chunks.push(chunk);
      }
      clearTimeout(timer); timer = undefined;
      const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      if (!body || typeof body !== 'object' || Array.isArray(body) ||
          Object.keys(body).some((key) => !['action', 'transaction_id'].includes(key)) ||
          !['status', 'prepare', 'resume', 'service_sync'].includes(body.action) ||
          (body.transaction_id !== undefined && (typeof body.transaction_id !== 'string' || body.transaction_id.length > 160))) {
        throw new Error('invalid_request');
      }
      if (body.action === 'prepare') send(200, { ok: true, result: await prepare(body.transaction_id) });
      else if (body.action === 'resume') send(200, { ok: true, result: await resume() });
      else if (body.action === 'service_sync') send(200, { ok: true, result: await syncService() });
      else { marker = await readMarker(); send(200, { ok: true, result: snapshot() }); }
    } catch (error) {
      const code = error.message === 'maintenance_marker_mismatch' ? error.message : 'control_failed';
      send(409, { ok: false, error: { code } });
    } finally { clearTimeout(timer); }
  });
  server.requestTimeout = 10000;
  server.headersTimeout = 10000;
  await new Promise((fulfill, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', fulfill); });
  server.unref();
  try {
    await mkdir(registryDir, { recursive: true, mode: 0o700 });
    if ((await lstat(registryDir)).isSymbolicLink()) throw new Error('host-clients must not be a symbolic link');
    const registration = { schema_version: 1, instance_id: instanceId, pid: process.pid,
      port: server.address().port, token, config_path: options.configPath, data_dir: options.dataDir,
      command: options.command || null, command_args: options.commandArgs || [], client_id: options.clientId,
      plugin_version: '0.7.0', created_at: new Date().toISOString() };
    await writeFile(registrationPath + '.tmp', JSON.stringify(registration), { mode: 0o600, flag: 'wx' });
    await rename(registrationPath + '.tmp', registrationPath);
  } catch (error) {
    server.close();
    await unlink(registrationPath + '.tmp').catch(() => {});
    throw error;
  }
  let polling = false;
  const poll = setInterval(async () => {
    if (polling || disposed) return;
    polling = true;
    try {
      const current = await readMarker();
      if (current) await prepare();
      else if (blocked) await resume();
      if (superviseService && !current && !blocked) {
        await syncService();
        if (control.desired_state === 'running') {
          if (retryAt !== null && now() >= retryAt) { retryAt = null; await start(); }
          else if (state === 'ready' && probe && now() >= probeAt) {
            probeAt = now() + probeEveryMs;
            try { await runRuntime(probe); }
            catch (error) {
              if (error instanceof MaintenanceError) return;
              state = 'failed'; lastError = { code: 'service_connection_lost' };
              await drainMcp(); scheduleRetry();
            }
          }
        }
      }
    } catch { /* Remain blocked; the installer owns recovery and marker removal. */ }
    finally { polling = false; }
  }, pollMs);
  poll.unref();
  return {
    registrationPath, runRuntime, prepare, resume, start, syncService,
    status: snapshot,
    setResumeHandler(callback) { connector = callback; },
    setProbeHandler(callback) { probe = callback; },
    setMcpFiber(value) { fiber = value; },
    async dispose() {
      if (disposing) return disposing;
      disposed = true; blocked = true; state = 'disposed'; clearInterval(poll);
      disposing = (async () => {
        await drain();
        if (starting) await starting.catch(() => {});
        await unlink(registrationPath).catch((error) => { if (error.code !== 'ENOENT') throw error; });
        server.closeAllConnections();
        await new Promise((fulfill) => server.close(fulfill));
      })();
      return disposing;
    },
  };
}
