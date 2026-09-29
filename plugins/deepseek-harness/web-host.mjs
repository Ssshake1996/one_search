/** Authenticated DSH Web adapter. Backend credentials never leave this process. */
import { spawn } from 'node:child_process';
import { open, realpath } from 'node:fs/promises';
import { request as httpRequest } from 'node:http';
import { homedir } from 'node:os';
import { isAbsolute, join, resolve } from 'node:path';

const MAX_REQUEST = 128 * 1024;
const MAX_RESPONSE = 2 * 1024 * 1024;
const SERVICE_ACTIONS = new Set(['service_start', 'service_stop', 'service_force_stop']);
const SHIELDED_ACTIONS = new Set(['settings_save', ...SERVICE_ACTIONS, 'schedule_save', 'schedule_delete']);
const MANAGE_ACTIONS = new Set(['settings_get', 'settings_preview', 'settings_save',
  'db_discover', 'db_propose', 'db_preflight', 'credential_store',
  'model_start', 'model_import', 'model_cancel', ...SERVICE_ACTIONS,
  'schedules_get', 'schedule_save', 'schedule_delete']);
const READ_ACTIONS = new Set(['status', 'diagnose_path', 'settings_get', 'settings_preview',
  'db_discover', 'db_propose', 'db_preflight', 'schedules_get']);
const DIRECT_ACTIONS = new Set(['status', 'pause', 'resume', 'scan', 'refresh_path', 'diagnose_path']);
const STATUS_KEYS = new Set(['schema_version', 'version', 'instance_id', 'node_id', 'paused', 'last_error',
  'runtime_policy', 'capabilities', 'file_scope', 'coverage', 'resources', 'indexing', 'vector_index',
  'worker_controls', 'database_sync', 'scheduler', 'journal', 'vector_error', 'semantic', 'remote_nodes', 'progress',
  'pause_state', 'background_activity']);
const plain = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
class BridgeError extends Error {
  constructor(code, message, details) { super(message); this.code = code; this.details = details; }
}
const failed = (code, message, details) => ({ ok: false, error: { code, message, ...(details ? { details } : {}) } });
// Only fixed public text crosses the bridge. OS errors, state tokens, database
// diagnostics and raw subprocess output may contain private information.
const SERVICE_ERRORS = {
  configuration_missing: ['找不到后台配置文件。', '在运行 DSH 的电脑上确认安装目录及 configPath，按 README 完成安装。', false],
  configuration_unreadable: ['无法读取后台配置文件。', '检查运行 DSH 的账户是否有配置目录的读取权限。', false],
  configuration_invalid: ['后台配置文件格式无效。', '检查配置 JSON，或使用保留数据的安装方式修复；不要删除索引。', false],
  service_state_missing: ['未找到正在运行的后台服务记录。', '在服务所在电脑检查 installation-status 和后台日志，启动服务后重试。', true],
  service_state_unreadable: ['无法读取后台服务状态。', '检查 DSH 与后台是否使用同一账户，以及数据目录权限。', false],
  service_state_invalid: ['后台服务状态记录无效。', '查看后台日志并正常重启服务，不要手工修改状态文件。', false],
  service_identity_mismatch: ['后台服务身份与当前配置不一致。', '核对 DSH 的 configPath 和数据目录；服务刚重启时可稍后重试。', true],
  service_connection_refused: ['后台记录存在，但本地服务未接受连接。', '服务可能已退出；在服务所在电脑检查后台日志和服务状态。', true],
  service_connection_timeout: ['等待后台响应超时。', '检查服务器负载和后台日志；服务恢复响应后页面会自动重连。', true],
  service_disconnected: ['与后台的连接在响应完成前断开。', '服务可能正在重启；稍后重试，反复出现时检查后台日志。', true],
  service_auth_rejected: ['后台拒绝了当前连接凭据。', '服务可能已重启；重试读取最新状态，持续失败时核对数据目录。', true],
  service_endpoint_rejected: ['后台拒绝了本地 RPC 请求。', '核对后台与插件版本，确认连接的是 one_search 本地服务。', false],
  service_busy: ['后台当前请求过多。', '等待正在执行的查询完成后重试。', true],
  service_stopping: ['后台正在停止或重启。', '等待服务恢复；页面会自动重新读取状态。', true],
  service_response_invalid: ['后台返回了无法识别的响应。', '核对后台和插件版本，并检查后台日志。', false],
  service_response_too_large: ['后台状态响应超过大小限制。', '减少数据源范围或联系维护人员，附上此错误码。', false],
  backend_rejected: ['后台未能执行此操作。', '检查操作参数与后台日志后重试。', false],
  backend_operation_failed: ['后台执行操作失败。', '检查数据源是否可访问，并查看后台日志。', true],
  host_startup_failed: ['DSH 尚未成功连接后台。', '检查 DSH 的 one_search 启动日志，修复安装或服务问题后重新加载此 profile。', true],
  host_starting: ['DSH 正在准备后台连接。', '等待安装或服务启动完成，页面会自动重试。', true],
};
function serviceError(code, stage) {
  const [message, action, retryable] = SERVICE_ERRORS[code];
  return new BridgeError(code, message, { stage, reason: code, action, retryable });
}

async function readServiceFile(path, limit, kind) {
  try { return await readJson(path, limit); }
  catch (error) {
    const suffix = error.code === 'ENOENT' ? 'missing' : error instanceof SyntaxError || !error.code ? 'invalid' : 'unreadable';
    throw serviceError(kind + '_' + suffix, kind);
  }
}

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
    const config = await readServiceFile(backend.configPath, 1024 * 1024, 'configuration');
    if (!plain(config) || typeof config.data_dir !== 'string' || !config.data_dir) throw serviceError('configuration_invalid', 'configuration');
    const dataDir = config.data_dir.replace(/^~(?=$|[\\/])/, homedir());
    const state = await readServiceFile(join(resolve(dataDir), 'service.json'), 16384, 'service_state');
    if (!plain(state) || !Number.isInteger(state.port) || state.port < 1024 || state.port > 65535 ||
        !Number.isInteger(state.pid) || state.pid < 1 || typeof state.token !== 'string' || state.token.length < 32 ||
        typeof state.service_id !== 'string' || !state.service_id || typeof state.config_path !== 'string' ||
        !isAbsolute(state.config_path)) throw serviceError('service_state_invalid', 'service_state');
    // Python canonicalizes config_path on startup. Match the same real file even
    // when DSH used a directory symlink or different Windows path casing.
    let actual, expected;
    try { [actual, expected] = await Promise.all([realpath(state.config_path), realpath(backend.configPath)]); }
    catch { throw serviceError('service_identity_mismatch', 'service_state'); }
    if (process.platform === 'win32') { actual = actual.toLowerCase(); expected = expected.toLowerCase(); }
    if (actual !== expected) throw serviceError('service_identity_mismatch', 'service_state');
    return state;
}

function daemonCall(state, method, params, signal, timeoutMs = 12000) {
  const stage = method === '_health' ? 'health' : method === 'index_status' ? 'status' : 'operation';
  const transportError = (error) => error instanceof BridgeError ? error : signal?.aborted
    ? new BridgeError('cancelled', '操作已取消。')
    : serviceError(error?.code === 'ECONNREFUSED' ? 'service_connection_refused'
      : error?.code === 'ETIMEDOUT' ? 'service_connection_timeout' : 'service_disconnected', stage);
  return new Promise((fulfill, reject) => {
    const body = Buffer.from(JSON.stringify({ method, params }));
    const req = httpRequest({ hostname: '127.0.0.1', port: state.port, path: '/rpc', method: 'POST',
      signal, headers: { 'Content-Type': 'application/json', 'Content-Length': body.length,
        Authorization: 'Bearer ' + state.token } }, (response) => {
      const chunks = [];
      let length = 0;
      response.on('data', (chunk) => {
        length += chunk.length;
        if (length > MAX_RESPONSE) { const error = serviceError('service_response_too_large', stage); reject(error); response.destroy(); req.destroy(error); }
        else chunks.push(chunk);
      });
      response.on('error', (error) => reject(transportError(error)));
      response.on('end', () => {
        try {
          if (response.statusCode !== 200) throw serviceError(response.statusCode === 401 ? 'service_auth_rejected'
            : response.statusCode === 503 || response.statusCode === 429 ? 'service_busy' : 'service_endpoint_rejected', stage);
          const result = JSON.parse(Buffer.concat(chunks).toString('utf8'));
          if (!plain(result) || typeof result.ok !== 'boolean' || (result.ok && !Object.hasOwn(result, 'result'))) throw serviceError('service_response_invalid', stage);
          if (!result.ok) {
            // v0.5.0/1 return fixed strings; newer runtimes also supply a code.
            const legacy = { 'Service is busy; retry later': 'service_busy', 'Service is shutting down': 'service_stopping' };
            const allowed = new Set(['service_busy', 'service_stopping', 'backend_operation_failed']);
            const code = allowed.has(result.error_code) ? result.error_code
              : typeof result.error === 'string' && Object.hasOwn(legacy, result.error) ? legacy[result.error] : 'backend_rejected';
            throw serviceError(code, stage);
          }
          fulfill(result.result);
        } catch (error) { reject(error instanceof BridgeError ? error : serviceError('service_response_invalid', stage)); }
      });
    });
    const timer = setTimeout(() => req.destroy(serviceError('service_connection_timeout', stage)), timeoutMs);
    req.on('close', () => clearTimeout(timer));
    req.on('error', (error) => reject(transportError(error)));
    req.end(body);
  });
}

/** A bounded identity check for the host supervisor; no runtime subprocess. */
export async function probeBackend(connection) {
  const state = await readState(preparedBackend(connection));
  const health = await daemonCall(state, '_health', {}, undefined, 3000);
  if (!plain(health) || health.service_id !== state.service_id || health.pid !== state.pid) throw serviceError('service_identity_mismatch', 'health');
  if (health.status !== 'running') throw serviceError('service_stopping', 'health');
}

/** Only occasional administration uses a CLI child; status never spawns Python. */
export function runManagement(backend, request, { signal, timeoutMs = 75000, spawnProcess = spawn } = {}) {
  let markClosed;
  const closed = new Promise((resolve) => { markClosed = resolve; });
  const promise = new Promise((fulfill, reject) => {
    if (signal?.aborted) { markClosed(); return reject(new BridgeError('cancelled', '操作已取消。')); }
    const shieldSave = SHIELDED_ACTIONS.has(request.action);
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
  if (SERVICE_ACTIONS.has(action) && Object.keys(params).length) throw new BridgeError('invalid_request', '服务控制操作不接受额外参数。');
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

export function createWebBridge(connection, { manage = runManagement, now = Date.now,
  runRuntime = (callback) => callback(), maintenanceStatus = () => null,
  syncService = async () => {},
  healthTimeoutMs = 3000, statusTimeoutMs = 12000 } = {}) {
  const active = new Set();
  let disposed = false;
  let cached;
  let inflight;
  let generation = 0;
  let queue = Promise.resolve();
  let queued = 0;
  let previousConnection;
  let wasMaintenance = false;
  let controlRevision;
  function invalidate() { generation++; cached = undefined; inflight = undefined; }
  function maintenance() {
    const snapshot = maintenanceStatus();
    const blocked = snapshot?.maintenance === true;
    if (blocked !== wasMaintenance) { invalidate(); wasMaintenance = blocked; }
    if (!blocked) return null;
    return { service: { status: 'maintenance' }, upgrade: { maintenance: true,
      state: ['starting', 'ready', 'maintenance', 'failed', 'disposed'].includes(snapshot.state) ? snapshot.state : 'maintenance' } };
  }
  function backend() {
    const current = typeof connection === 'function' ? connection() : connection;
    if (!current) throw serviceError(maintenanceStatus()?.state === 'failed' ? 'host_startup_failed' : 'host_starting', 'connection');
    if (current !== previousConnection) { invalidate(); previousConnection = current; }
    return preparedBackend(current);
  }
  function requireAvailable() {
    if (maintenance()) throw new BridgeError('upgrade_in_progress', '正在升级 one_search，请等待升级完成后重试。');
  }
  function withControl(value) {
    const snapshot = maintenanceStatus();
    return snapshot?.control ? { ...value, control: snapshot.control, reconnect: snapshot.reconnect } : value;
  }
  async function publicStatus() {
    await syncService();
    const revision = maintenanceStatus()?.control?.revision;
    if (revision !== controlRevision) { invalidate(); controlRevision = revision; }
    const upgrading = maintenance();
    if (upgrading) return withControl(upgrading);
    try { return withControl(await runRuntime(() => readStatus(backend()))); }
    catch (error) {
      if (error?.code === 'upgrade_in_progress') return withControl(maintenance() || { service: { status: 'maintenance' } });
      const snapshot = maintenanceStatus();
      if (!snapshot?.control || !(error instanceof BridgeError) || error.code.startsWith('configuration_')) throw error;
      const stopped = snapshot.control.desired_state === 'stopped';
      const expectedStop = stopped && ['service_state_missing', 'service_connection_refused', 'service_disconnected'].includes(error.code);
      const detail = snapshot.control.error || (!expectedStop ? { code: error.code, message: error.message, details: error.details } : null);
      return withControl({ service: { status: stopped ? 'stopped' : 'offline', ...(detail ? { error: detail } : {}) } });
    }
  }
  async function readStatus(prepared) {
    if (cached && now() - cached.at < 1000) return cached.value;
    if (inflight) return inflight;
    const controller = new AbortController();
    const startedGeneration = generation;
    active.add(controller);
    const task = (async () => {
      let state = await readState(prepared);
      const sample = async () => {
        const health = await daemonCall(state, '_health', {}, controller.signal, healthTimeoutMs);
        if (!plain(health)) throw serviceError('service_response_invalid', 'health');
        if (health.service_id !== state.service_id || health.pid !== state.pid) throw serviceError('service_identity_mismatch', 'health');
        if (health.status === 'stopping') throw serviceError('service_stopping', 'health');
        if (health.status !== 'running') throw serviceError('service_response_invalid', 'health');
        const index = await daemonCall(state, 'index_status', {}, controller.signal, statusTimeoutMs);
        if (!plain(index)) throw serviceError('service_response_invalid', 'status');
        return index;
      };
      let index;
      try { index = await sample(); }
      catch (error) {
        if (!['service_identity_mismatch', 'service_auth_rejected', 'service_connection_refused', 'service_disconnected'].includes(error.code)) throw error;
        // A restart may atomically replace service.json after our first read.
        // Retry this read-only sample once, only when the state really changed.
        const replacement = await readState(prepared);
        if (replacement.service_id === state.service_id && replacement.port === state.port &&
            replacement.token === state.token && replacement.pid === state.pid) throw error;
        state = replacement;
        index = await sample();
      }
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
    const upgrading = maintenance();
    if (request.action === 'status') {
      if (upgrading) return { ok: true, result: withControl(upgrading) };
      return { ok: true, result: await publicStatus() };
    }
    requireAvailable();
    const controller = new AbortController();
    const cancel = () => controller.abort();
    // Once accepted, saving may be between stopping the old runtime and
    // activating its replacement. Page cancellation must not interrupt it.
    const shieldSave = SHIELDED_ACTIONS.has(request.action);
    if (!shieldSave) { externalSignal?.addEventListener('abort', cancel, { once: true }); active.add(controller); }
    try {
      if (MANAGE_ACTIONS.has(request.action)) {
        const result = await runRuntime(() => {
          requireAvailable();
          const operation = manage(backend(), request, {
            signal: controller.signal,
            timeoutMs: request.action === 'settings_save' ? 120000 : SERVICE_ACTIONS.has(request.action) || request.action === 'settings_preview' ? 60000 : 30000,
          });
          if (shieldSave && operation.closed) trackCompletion(operation.closed);
          // Preserve the actual process lifetime for coordinator.prepare(), even
          // when the response times out before an accepted save has finished.
          return operation;
        });
        if (result.ok && SERVICE_ACTIONS.has(request.action)) {
          invalidate();
          return { ok: true, result: await publicStatus() };
        }
        return result;
      }
      const result = await runRuntime(async () => {
        requireAvailable();
        const state = await readState(backend());
        return daemonCall(state, request.action, request.params, controller.signal, request.action === 'refresh_path' ? 30000 : 12000);
      });
      return { ok: true, result };
    } finally {
      active.delete(controller);
      externalSignal?.removeEventListener('abort', cancel);
      if (!READ_ACTIONS.has(request.action)) invalidate();
    }
  }
  return {
    async request(input, signal) {
      try {
        const request = validateRequest(input);
        if (!disposed && !signal?.aborted && request.action !== 'status') requireAvailable();
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
        if (error?.code === 'upgrade_in_progress') {
          invalidate();
          return failed('upgrade_in_progress', '正在升级 one_search，请等待升级完成后重试。');
        }
        return error instanceof BridgeError ? failed(error.code, error.message, error.details) : failed('operation_failed', '操作未完成，请刷新状态后重试。');
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
export function registerWebHost(ctx, connection, options = {}) {
  ctx.inject(['connection', 'webServer'], (webCtx) => {
    const bridge = createWebBridge(connection, options);
    webCtx.effect(() => {
      const unregister = webCtx.webServer.register({ kind: 'prefix', path: '/one-search', handler: webRpcHandler(webCtx, bridge) });
      return async () => { bridge.dispose(); await unregister(); };
    }, 'one-search: authenticated Web management');
  });
}
