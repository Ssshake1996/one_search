/** Read the backend's durable user intent without loading its executable. */
import { readFile, stat } from 'node:fs/promises';
import { join } from 'node:path';

export async function readServiceControl(dataDir) {
  try {
    const path = join(dataDir, 'service-control.json');
    if ((await stat(path)).size > 8192) throw new Error('control too large');
    const value = JSON.parse((await readFile(path, 'utf8')).replace(/^\uFEFF/, ''));
    if (value?.schema_version !== 1 || !['running', 'stopped'].includes(value.desired_state) ||
        typeof value.revision !== 'string' || !value.revision || value.revision.length > 160 ||
        !['manual', 'forced', 'scheduled', 'automatic'].includes(value.reason) || !Number.isFinite(value.updated_at)) throw new Error('invalid control');
    return { schema_version: 1, desired_state: value.desired_state, revision: value.revision,
      reason: ['manual', 'forced', 'scheduled', 'automatic'].includes(value.reason) ? value.reason : null,
      updated_at: Number.isFinite(value.updated_at) ? value.updated_at : null };
  } catch (error) {
    if (error.code === 'ENOENT') return { schema_version: 1, desired_state: 'running', revision: 'initial', reason: 'automatic', updated_at: 0 };
    // Corruption and access errors are never permission to launch a daemon.
    return { schema_version: 1, desired_state: 'stopped', revision: 'invalid', reason: null, updated_at: null,
      error: { code: 'service_control_invalid', message: '无法读取服务启停记录，请检查数据目录权限与文件完整性。' } };
  }
}

export function retryDelay(attempt, { initialMs = 1000, maximumMs = 60000, random = Math.random } = {}) {
  const base = Math.min(maximumMs, initialMs * 2 ** Math.min(Math.max(0, attempt - 1), 30));
  return Math.min(maximumMs, Math.round(base * (1 + Math.max(0, Math.min(1, random())) * .2)));
}
