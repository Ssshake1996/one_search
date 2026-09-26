/** Authenticated DSH Web adapter. Backend credentials never leave this process. */
import { spawn } from 'node:child_process';
import { open } from 'node:fs/promises';
import { request as httpRequest } from 'node:http';
import { homedir } from 'node:os';
import { isAbsolute, join, resolve } from 'node:path';

const MAX_REQUEST = 128 * 1024;
const MAX_RESPONSE = 2 * 1024 * 1024;
const MANAGE_ACTIONS = new Set(['settings_get', 'settings_preview', 'settings_save',
  'db_discover', 'db_propose', 'db_preflight', 'credential_store',
  'model_start', 'model_import', 'model_cancel']);
const READ_ACTIONS = new Set(['status', 'diagnose_path', 'settings_get', 'settings_preview',
  'db_discover', 'db_propose', 'db_preflight']);
const DIRECT_ACTIONS = new Set(['status', 'pause', 'resume', 'scan', 'refresh_path', 'diagnose_path']);
const STATUS_KEYS = new Set(['schema_version', 'version', 'instance_id', 'node_id', 'paused', 'last_error',
  'runtime_policy', 'capabilities', 'file_scope', 'coverage', 'resources', 'indexing', 'vector_index',
  'worker_controls', 'database_sync', 'scheduler', 'journal', 'vector_error', 'semantic', 'remote_nodes', 'progress']);
const plain = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
class BridgeError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}
const failed = (code, message) => ({ ok: false, error: { code, message } });
const unavailable = () => new BridgeError('service_offline', '后台服务暂不可用，请检查安装与服务状态后重试。');

async function readJson(path, limit) {
  const file = await open(path, 'r');
  try {
    const size = (await file.stat()).size;
    if (size > limit) throw new Error('Size limit exceeded');
    const bytes = Buffer.alloc(size + 1);
    let length = 0;
    while (length < bytes.length) {
      const { bytesRead } = await file.read(bytes, length, bytes.length - length, length);
      if (!bytesRead) break;
      length += bytesRead;
    }
    if (length > size) throw new Error('File changed while reading');
    return JSON.parse(bytes.subarray(0, length).toString('utf8').replace(/^\uFEFF/, ''));
  } finally { await file.close(); }
}

/** Derive trusted executable facts only from the prepared MCP connection. */
export function preparedBackend(connection) {
  const args = connection.args;
  if (!isAbsolute(connection.command) || !Array.isArray(args) || args.length < 3 ||
      args.at(-3) !== 'mcp' || args.at(-2) !== '--config' || !isAbsolute(args.at(-1))) {
    throw new Error('Expected a prepared one_search MCP connection');
  }
  return { command: connection.command, commandArgs: args.slice(0, -3), configPath: resolve(args.at(-1)) };
}

async function readState(backend) {
  try {
    const config = await readJson(backend.configPath, 1024 * 1024);
    if (typeof config.data_dir !== 'string' || !config.data_dir) throw new Error('Invalid data directory');
    const dataDir = config.data_dir.replace(/^~(?=$|[\\/])/, homedir());
    const state = await readJson(join(resolve(dataDir), 'service.json'), 16384);
    if (!plain(state) || !Number.isInteger(state.port) || state.port < 1024 || state.port > 65535 ||
        !Number.isInteger(state.pid) || state.pid < 1 || typeof state.token !== 'string' || state.token.length < 32 ||
        typeof state.service_id !== 'string' || !state.service_id || state.config_path !== backend.configPath) throw new Error('Invalid state');
    return state;
  } catch { throw unavailable(); }
}

function daemonCall(state, method, params, signal, timeoutMs = 12000) {
  return new Promise((fulfill, reject) => {
    const body = Buffer.from(JSON.stringify({ method, params }));
    const req = httpRequest({ hostname: '127.0.0.1', port: state.port, path: '/rpc', method: 'POST',
      signal, headers: { 'Content-Type': 'application/json', 'Content-Length': body.length,
        Authorization: 'Bearer ' + state.token } }, (response) => {
      const chunks = [];
      let length = 0;
      response.on('data', (chunk) => {
        length += chunk.length;
        if (length > MAX_RESPONSE) { response.destroy(); req.destroy(unavailable()); }
        else chunks.push(chunk);
      });
      response.on('error', () => reject(unavailable()));
      response.on('end', () => {
        try {
          if (response.statusCode !== 200) throw unavailable();
          const result = JSON.parse(Buffer.concat(chunks).toString('utf8'));
          if (!plain(result) || result.ok !== true) throw new BridgeError('backend_rejected', '后台未能执行此操作，请检查参数或稍后重试。');
          fulfill(result.result);
        } catch (error) { reject(error instanceof BridgeError ? error : unavailable()); }
      });
    });
    const timer = setTimeout(() => req.destroy(unavailable()), timeoutMs);
    req.on('close', () => clearTimeout(timer));
    req.on('error', () => reject(unavailable()));
    req.end(body);
  });
}

/** Only occasional administration uses a CLI child; status never spawns Python. */
export function runManagement(backend, request, { signal, timeoutMs = 75000, spawnProcess = spawn } = {}) {
  let markClosed;
  const closed = new Promise((resolve) => { markClosed = resolve; });
  const promise = new Promise((fulfill, reject) => {
    if (signal?.aborted) { markClosed(); return reject(new BridgeError('cancelled', '操作已取消。')); }
    const shieldSave = request.action === 'settings_save';
    let child;
    try {
      child = spawnProcess(backend.command, [...backend.commandArgs, 'web-manage', '--config', backend.configPath],
        { shell: false, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    } catch {
      markClosed();
      return reject(new BridgeError('management_unavailable', '管理命令不可用，请检查后台版本。'));
    }
    const chunks = [];
    let length = 0;
    let settled = false;
    let timer;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener('abort', cancel);
      if (error) { if (!shieldSave) child.kill(); reject(error); } else fulfill(value);
    };
    const cancel = () => finish(new BridgeError('cancelled', '操作已取消。'));
    if (!shieldSave) signal?.addEventListener('abort', cancel, { once: true });
    timer = setTimeout(() => finish(new BridgeError('operation_timeout', request.action === 'settings_save'
      ? '保存结果尚未确认，请重新读取设置和服务状态，确认实际结果后再重试。'
      : '操作超时，请检查后台状态后重试。')), timeoutMs);
    child.stdout.on('data', (chunk) => {
      length += chunk.length;
      if (length > MAX_RESPONSE) finish(new BridgeError('response_too_large', '后台响应过大，请缩小操作范围。'));
      else chunks.push(chunk);
    });
    // Diagnostics may contain connection details. Never mirror stderr into Web responses.
    child.stderr.resume();
    child.stdin.on('error', () => finish(new BridgeError('management_unavailable', '管理命令不可用，请检查后台版本。')));
    child.on('error', () => { markClosed(); finish(new BridgeError('management_unavailable', '管理命令不可用，请检查后台版本。')); });
    child.on('close', (code) => {
      markClosed();
      try {
        const result = JSON.parse(Buffer.concat(chunks).toString('utf8').replace(/^\uFEFF/, ''));
        if (!plain(result) || typeof result.ok !== 'boolean' || (code !== 0 && result.ok)) throw new Error();
        if (!result.ok && (!plain(result.error) || !/^[a-z0-9_]{1,80}$/.test(result.error.code) ||
            typeof result.error.message !== 'string' || result.error.message.length > 1000)) throw new Error();
        // web-manage owns typed public result projection; table and column names
        // such as "token" or "password" remain valid inside those public mappings.
        finish(null, result);
      } catch { finish(new BridgeError('management_unavailable', '管理命令未返回有效结果，请检查后台版本。')); }
    });
    child.stdin.end(JSON.stringify(request));
  });
  // The response deadline and transaction lifetime are different: a save may
  // finish after the browser receives an uncertain-result response.
  promise.closed = closed;
  return promise;
}

function validateRequest(request) {
  if (!plain(request) || Object.keys(request).some((key) => !['action', 'params'].includes(key)) ||
      !plain(request.params) || (!DIRECT_ACTIONS.has(request.action) && !MANAGE_ACTIONS.has(request.action)) ||
      Buffer.byteLength(JSON.stringify(request)) > MAX_REQUEST) throw new BridgeError('invalid_request', '请求格式或操作名称无效。');
  const { action, params } = request;
  const allowed = { status: [], pause: ['seconds'], resume: [], scan: [], refresh_path: ['path'], diagnose_path: ['path'] }[action];
  if (allowed && Object.keys(params).some((key) => !allowed.includes(key))) throw new BridgeError('invalid_request', '此操作包含不支持的参数。');
  if (action === 'pause' && params.seconds !== undefined && params.seconds !== null &&
      (!Number.isInteger(params.seconds) || params.seconds < 1 || params.seconds > 604800)) throw new BridgeError('invalid_request', '暂停时长必须为 1 秒到 7 天。');
  if (['refresh_path', 'diagnose_path'].includes(action) &&
      (typeof params.path !== 'string' || !isAbsolute(params.path) || params.path.length > 32768 || params.path.includes('\0'))) {
    throw new BridgeError('invalid_request', '请选择服务所在电脑上的绝对路径。');
  }
  return request;
}

export function createWebBridge(connection, { manage = runManagement, now = Date.now } = {}) {
  const backend = preparedBackend(connection);
  const active = new Set();
  let disposed = false;
  let cached;
  let inflight;
  let generation = 0;
  let queue = Promise.resolve();
  let queued = 0;
  async function status() {
    if (cached && now() - cached.at < 1000) return cached.value;
    if (inflight) return inflight;
    const controller = new AbortController();
    const startedGeneration = generation;
    active.add(controller);
    const task = (async () => {
      const state = await readState(backend);
      const health = await daemonCall(state, '_health', {}, controller.signal, 3000);
      if (health.service_id !== state.service_id || health.pid !== state.pid || health.status !== 'running') throw unavailable();
      const index = await daemonCall(state, 'index_status', {}, controller.signal);
      if (!plain(index)) throw unavailable();
      const value = { service: { status: 'running' }, index: Object.fromEntries(
        Object.entries(index).filter(([key]) => STATUS_KEYS.has(key))) };
      if (generation === startedGeneration) cached = { at: now(), value };
      return value;
    })();
    inflight = task;
    try { return await task; }
    finally { active.delete(controller); if (inflight === task) inflight = undefined; }
  }
  async function perform(request, externalSignal, trackCompletion = () => {}) {
    if (disposed || externalSignal?.aborted) throw new BridgeError('cancelled', '操作已取消。');
    if (request.action === 'status') return { ok: true, result: await status() };
    const controller = new AbortController();
    const cancel = () => controller.abort();
    // Once accepted, saving may be between stopping the old runtime and
    // activating its replacement. Page cancellation must not interrupt it.
    const shieldSave = request.action === 'settings_save';
    if (!shieldSave) { externalSignal?.addEventListener('abort', cancel, { once: true }); active.add(controller); }
    try {
      if (MANAGE_ACTIONS.has(request.action)) {
        const operation = manage(backend, request, {
          signal: controller.signal,
          timeoutMs: request.action === 'settings_save' ? 120000 : request.action === 'settings_preview' ? 60000 : 30000,
        });
        if (shieldSave && operation.closed) trackCompletion(operation.closed);
        return await operation;
      }
      const state = await readState(backend);
      const result = await daemonCall(state, request.action, request.params, controller.signal, request.action === 'refresh_path' ? 30000 : 12000);
      return { ok: true, result };
    } finally {
      active.delete(controller);
      externalSignal?.removeEventListener('abort', cancel);
      if (!READ_ACTIONS.has(request.action)) { generation++; cached = undefined; inflight = undefined; }
    }
  }
  return {
    async request(input, signal) {
      try {
        const request = validateRequest(input);
        // Management calls share a bounded queue even for reads, avoiding CLI storms.
        if (MANAGE_ACTIONS.has(request.action) || !READ_ACTIONS.has(request.action)) {
          if (queued >= 4) return failed('busy', '已有管理操作正在执行，请稍后重试。');
          queued++;
          let completion;
          const task = queue.then(() => perform(request, signal, (closed) => { completion = closed; }));
          queue = task.catch(() => {}).then(() => completion).finally(() => { queued--; });
          return await task;
        }
        return await perform(request, signal);
      } catch (error) {
        return error instanceof BridgeError ? failed(error.code, error.message) : failed('operation_failed', '操作未完成，请刷新状态后重试。');
      }
    },
    dispose() { disposed = true; for (const controller of active) controller.abort(); cached = undefined; },
  };
}

/** DSH Connection RPC wire carried by an authenticated, bounded host route.
 * Installed DSH 0.1.5-rc.2 rpc.handle attempts to access webServer from its own
 * undeclared service scope. The public route/trust APIs avoid that host defect.
 */
export function webRpcHandler(webCtx, bridge) {
  return async (req, res) => {
    const reject = webCtx.connection.requestRejection(req);
    if (reject !== undefined) { res.writeHead(reject); res.end(); req.resume(); return; }
    if (req.method !== 'POST') { res.writeHead(405); res.end(); req.resume(); return; }
    if ((req.url || '').split('?')[0] !== '/one-search/request') { res.writeHead(404); res.end(); req.resume(); return; }
    const limit = MAX_REQUEST + 1024;
    if (Number(req.headers['content-length']) > limit) { res.writeHead(413); res.end(); req.resume(); return; }
    const controller = new AbortController();
    const abort = () => { if (!res.writableEnded) controller.abort(); };
    res.once('close', abort);
    let rpcId = 'invalid-request';
    let timer = setTimeout(() => { req.destroy(); controller.abort(); }, 10000);
    const send = (status, value) => {
      if (res.destroyed || res.writableEnded) return;
      const response = { type: 'server-response', rpcId, result: { ok: true, value } };
      res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
      res.end(JSON.stringify(response));
    };
    try {
      const chunks = [];
      let size = 0;
      for await (const chunk of req) {
        size += chunk.length;
        if (size > limit) { send(413, failed('invalid_request', '请求过大。')); return; }
        chunks.push(chunk);
      }
      clearTimeout(timer); timer = undefined;
      const message = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      if (!plain(message) || message.type !== 'client-request' || message.method !== 'request' ||
          typeof message.rpcId !== 'string' || !message.rpcId || message.rpcId.length > 200 ||
          Object.keys(message).some((key) => !['type', 'rpcId', 'method', 'payload'].includes(key))) throw new Error('Invalid envelope');
      rpcId = message.rpcId;
      send(200, await bridge.request(message.payload, controller.signal));
    } catch {
      send(400, failed('invalid_request', '请求格式无效。'));
    } finally {
      clearTimeout(timer);
      res.off('close', abort);
    }
  };
}

/** Optional connection injection keeps terminal/headless MCP activation unchanged. */
export function registerWebHost(ctx, connection) {
  ctx.inject(['connection', 'webServer'], (webCtx) => {
    const bridge = createWebBridge(connection);
    webCtx.effect(() => {
      const unregister = webCtx.webServer.register({ kind: 'prefix', path: '/one-search', handler: webRpcHandler(webCtx, bridge) });
      return async () => { bridge.dispose(); await unregister(); };
    }, 'one-search: authenticated Web management');
  });
}
