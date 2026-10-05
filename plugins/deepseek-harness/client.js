/* Ready-to-load DSH client module. This file is its source: no separate build output. */
window.__ModuleLoader__.load({
  id: 'one-search-bundle',
  factory(require) {
    'use strict';
    const React = require('react');
    const h = React.createElement;
    const { useState, useEffect, useRef } = React;
    const clone = value => JSON.parse(JSON.stringify(value));
    const lines = value => String(value || '').split(/\r?\n/).map(x => x.trim()).filter(Boolean);
    const number = value => Number.isFinite(value) ? value.toLocaleString('zh-CN') : '—';
    const date = value => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('zh-CN') : '—';
    const mb = value => Number.isFinite(value) ? `${number(Math.round(value))} MiB` : '—';
    const duration = value => {
      if (!Number.isFinite(value) || value < 0) return '—';
      if (value < 60) return `${value.toLocaleString('zh-CN', { maximumFractionDigits: 1 })} 秒`;
      if (value < 3600) return `${Math.floor(value / 60)} 分 ${Math.floor(value % 60)} 秒`;
      return `${Math.floor(value / 3600)} 小时 ${Math.floor(value % 3600 / 60)} 分`;
    };
    const rate = value => Number.isFinite(value) && value >= 0 ? `${value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })} 文件/秒` : '—';
    const labels = {
      maintenance: '升级维护中', pausing: '正在暂停', paused: '已暂停', waiting: '等待资源', indexing: '正在扫描与建索引', needs_attention: '需要关注',
      up_to_date: '当前已知任务已处理', user_pause: '手动暂停', system_busy: '电脑繁忙，稍后继续',
      memory_budget_exceeded: '达到内存预算', system_memory_pressure: '可用内存不足',
      disk_budget_exceeded: '达到索引空间预算', disk_free_space_low: '磁盘可用空间不足',
      idle_only: '等待电脑空闲', on_ac_only: '等待接通电源', battery_low: '电量偏低',
      foreground_query: '优先响应检索', foreground_grace: '优先响应检索', battery_throttle: '电池模式下减速',
      ready: '就绪', missing: '尚未准备', running: '准备中', queued: '排队中', cancelling: '正在取消',
      cancelled: '已取消', interrupted: '已中断', failed: '失败', disabled: '已关闭',
      scanning: '扫描中', reconciling: '核对变动', idle: '本轮已处理', pending: '待处理', error: '失败',
      discover: '发现文件中', cleanup: '核对旧记录', done: '本轮已扫描', waiting_for_ac_power: '等待接通电源',
      waiting_for_idle: '等待电脑空闲', battery_saving: '电池模式下减速', retry_backoff: '等待失败任务重试',
      model_not_ready: '等待语义模型准备', incomplete_sources: '部分来源需要处理', known_tasks_pending: '正在处理已知任务',
      low: '节省资源', balanced: '均衡', fast: '优先速度', stopped: '服务未启动', running_service: '后台运行中',
      configured_limit: '已达到配置的并发上限', memory_capacity: '可用内存限制了并发', cpu_capacity: '可用 CPU 限制了并发',
      system_cpu_pressure: '电脑繁忙，暂时减少并发', backlog: '当前可处理任务较少',
      adaptive: '自适应预算', fixed: '固定预算',
    };
    const label = value => labels[value] || value || '—';

    function unwrap(transport) {
      if (!transport || transport.ok !== true) {
        const error = new Error(transport?.error?.message || '与 DSH 的连接中断，请稍后重试。');
        error.code = transport?.error?.code || 'connection_error';
        error.details = transport?.error?.details;
        throw error;
      }
      const response = transport.value;
      if (!response || response.ok !== true) {
        const error = new Error(response?.error?.message || '操作未完成，请重试。');
        error.code = response?.error?.code || 'invalid_response';
        error.details = response?.error?.details;
        throw error;
      }
      return response.result;
    }

    // One request at a time, including manual refreshes. Hidden tabs stop scheduling;
    // in-flight replies may settle but never update an unmounted panel.
    function createPoller({ request, onValue, onError, document: doc, intervalMs = 2000, maxDelay = 30000, setTimer = setTimeout, clearTimer = clearTimeout }) {
      let stopped = false, timer = null, running = false, errors = 0, again = false;
      const clear = () => { if (timer !== null) clearTimer(timer); timer = null; };
      const visible = () => doc.visibilityState !== 'hidden';
      const schedule = delay => { clear(); if (!stopped && visible()) timer = setTimer(tick, delay); };
      async function tick() {
        clear();
        if (stopped || !visible()) return;
        if (running) { again = true; return; }
        running = true;
        try { const value = await request(); if (!stopped) { errors = 0; onValue(value); } }
        catch (error) { if (!stopped) { errors += 1; onError(error, Math.min(maxDelay, intervalMs * 2 ** errors)); } }
        finally {
          running = false;
          if (!stopped) { const delay = again ? 0 : Math.min(maxDelay, intervalMs * 2 ** errors); again = false; schedule(delay); }
        }
      }
      const visibility = () => { clear(); if (visible()) tick(); };
      doc.addEventListener('visibilitychange', visibility);
      tick();
      return { refresh: tick, dispose() { stopped = true; clear(); doc.removeEventListener('visibilitychange', visibility); } };
    }

    function freshDraft(snapshot) { return { revision: snapshot.revision, values: clone(snapshot.values), original: clone(snapshot.values) }; }
    function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
    function errorDescription(error) {
      const publicText = value => typeof value === 'string' ? value.slice(0, 1000) : '';
      return { message: publicText(error?.message) || '操作未完成，请重试。',
        code: typeof error?.code === 'string' && /^[a-z0-9_]{1,80}$/.test(error.code) ? error.code : 'operation_failed',
        action: publicText(error?.details?.action) };
    }
    function errorContent(error) {
      const info = errorDescription(error);
      return h(React.Fragment, null, h('p', null, info.message), h('p', { className: 'os-note' }, '错误码：', h('code', null, info.code)),
        info.action && h('p', null, info.action));
    }
    function pauseStatus(status) {
      const index = status?.index || {};
      const policy = index.runtime_policy || index.progress?.runtime_policy || {};
      const state = ['running', 'pausing', 'paused'].includes(index.pause_state) ? index.pause_state :
        policy.user_paused === true || index.paused === true ? 'paused' : 'running';
      return { state, until: policy.pause_until || index.pause_until };
    }
    function serviceStatus(status, disconnected) {
      const state = status?.service?.status;
      if (disconnected) return { state: 'unknown', title: '服务状态待确认' };
      if (state === 'maintenance') return { state, title: '升级维护中' };
      if (status?.control?.desired_state === 'stopped') return { state: 'stopped', title: state === 'running' ? '正在停止后台服务' : state === 'stopped' ? '后台已主动停止' : '已禁止自动启动，服务状态待确认' };
      if (state === 'running') return { state, title: '后台服务运行中' };
      if (status?.reconnect?.state === 'waiting') return { state: 'waiting', title: '异常断线，等待重连' };
      if (['connecting', 'starting'].includes(status?.reconnect?.state)) return { state: 'waiting', title: '正在恢复后台连接' };
      return { state: state || 'unknown', title: status ? '后台服务未连接' : '正在读取服务状态' };
    }
    function scheduleDraft(task, timezone) {
      const schedule = task?.schedule || {};
      return { id: task?.id, name: task?.name || '', enabled: task?.enabled ?? true,
        kind: schedule.kind || 'once', date: (schedule.at || timezone?.now || '').slice(0, 10),
        time: schedule.time || (schedule.at ? schedule.at.slice(11, 16) : '09:00'), weekdays: [...(schedule.weekdays || [1])] };
    }
    function scheduleTask(draft, timezone) {
      if (!draft.name.trim()) throw new Error('请填写任务名称。');
      if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(draft.time)) throw new Error('请选择有效的启动时间。');
      let schedule;
      if (draft.kind === 'once') {
        const day = new Date(`${draft.date}T00:00:00Z`);
        if (!/^\d{4}-\d{2}-\d{2}$/.test(draft.date) || !Number.isFinite(day.getTime()) || day.toISOString().slice(0, 10) !== draft.date) throw new Error('请选择有效的启动日期。');
        if (!timezone) throw new Error('尚未读取到服务器时区，请刷新任务后再保存。');
        // The server resolves local calendar time, including the offset at a future DST date.
        schedule = { kind: 'once', at: `${draft.date}T${draft.time}:00` };
      } else if (draft.kind === 'daily') schedule = { kind: 'daily', time: draft.time };
      else if (draft.kind === 'weekly') {
        const weekdays = [...new Set(draft.weekdays)].filter(day => Number.isInteger(day) && day >= 1 && day <= 7).sort();
        if (!weekdays.length) throw new Error('请选择至少一个星期。');
        schedule = { kind: 'weekly', time: draft.time, weekdays };
      } else throw new Error('请选择有效的重复方式。');
      return { ...(draft.id ? { id: draft.id } : {}), name: draft.name.trim(), enabled: draft.enabled, schedule };
    }
    function scheduleDate(value, timezone) {
      if (!value) return '—';
      const instant = new Date(typeof value === 'number' ? value * 1000 : value);
      if (!Number.isFinite(instant.getTime())) return '—';
      if (timezone?.name) {
        try { return instant.toLocaleString('zh-CN', { timeZone: timezone.name, hour12: false }); } catch {}
      }
      const offset = (typeof value === 'string' ? value.match(/([+-]\d{2}:\d{2})$/)?.[1] : null) || timezone?.offset;
      const match = /^([+-])(\d{2}):(\d{2})$/.exec(offset || '');
      if (!match) return instant.toISOString().replace('T', ' ').replace('.000Z', ' UTC');
      const minutes = (Number(match[2]) * 60 + Number(match[3])) * (match[1] === '+' ? 1 : -1);
      return `${new Date(instant.getTime() + minutes * 60000).toISOString().slice(0, 16).replace('T', ' ')} (UTC${offset})`;
    }
    function diagnosticMessage(result) {
      return (result.diagnostics || result.checks || []).filter(x => x.ok !== true).map(x => [x.message, x.action].filter(Boolean).join(' ')).join('；') || '请检查连接信息与允许读取的字段。';
    }
    function selectionFor(table, source) {
      const entry = (source.index || []).find(x => x.table === table.table);
      return { table: table.table, enabled: (source.allowed_tables || []).includes(table.table),
        columns: [...(source.allowed_columns?.[table.table] || [])], index_text_columns: [...(entry?.text_columns || [])],
        id_column: entry?.id_column || table.index_recommendation?.id_column || '', updated_column: entry?.updated_column || '',
        watermark_confirmed: Boolean(entry?.updated_column) };
    }
    function packSelections(states) {
      return Object.values(states).filter(x => x.enabled).map(state => {
        if (!state.columns.length) throw new Error(`请为 ${state.table} 选择至少一个允许读取的字段。`);
        const result = { table: state.table, columns: state.columns };
        if (state.index_text_columns.length) {
          if (!state.id_column || !state.columns.includes(state.id_column)) throw new Error(`请为 ${state.table} 选择并允许读取唯一键。`);
          result.index_text_columns = state.index_text_columns;
          result.id_column = state.id_column;
          if (state.updated_column) {
            if (!state.watermark_confirmed) throw new Error(`请确认 ${state.table} 的水位字段会随每次新增或更新维护。`);
            result.updated_column = state.updated_column;
          }
        }
        return result;
      });
    }
    function connectionSource(source) {
      const result = clone(source);
      result.id = (result.id || '').trim();
      if (!result.id) throw new Error('请填写来源名称。');
      if (result.kind === 'sqlite') {
        if (!result.path?.trim()) throw new Error('请填写 DSH 所在电脑上的 SQLite 文件路径。');
        for (const key of ['host', 'port', 'database', 'user', 'credential_ref', 'password_env', 'ssl']) delete result[key];
      } else {
        if (!result.host?.trim() || !result.database?.trim() || !result.user?.trim()) throw new Error('请填写数据库地址、库名与只读账号。');
        result.port = Number(result.port || (result.kind === 'mysql' ? 3306 : 5432));
        if (!Number.isInteger(result.port) || result.port < 1 || result.port > 65535) throw new Error('端口必须为 1–65535 的整数。');
        delete result.path;
        if (result.ssl) {
          result.ssl = Object.fromEntries(Object.entries(result.ssl).filter(([, value]) => value !== ''));
          if (!Object.keys(result.ssl).length) delete result.ssl;
        }
      }
      return result;
    }

    const css = `
.os-panel{height:100%;overflow:auto;box-sizing:border-box;color:var(--dsw-alias-label-primary,#202329);background:var(--dsw-alias-bg-base,#fff);font-family:inherit;font-size:14px;line-height:1.55;--os-line:var(--dsw-alias-border-l3,#e2e5e9);--os-muted:var(--dsw-alias-label-secondary,#69717e);--os-soft:var(--dsw-alias-interactive-bg-hover,#f4f5f7);--os-blue:var(--dsw-alias-state-business-primary,#4164d6)}
.os-panel *{box-sizing:border-box}.os-body{max-width:1120px;padding:32px 36px 24px;margin:0 auto}.os-head{display:flex;align-items:flex-start;justify-content:space-between;gap:24px}.os-head h1{font-size:26px;font-weight:650;letter-spacing:-.8px;margin:0 0 3px}.os-subtitle,.os-muted{color:var(--os-muted)}.os-subtitle{margin:0}.os-state{display:inline-flex;align-items:center;gap:8px;white-space:nowrap;font-size:12px;padding:6px 10px;border:1px solid var(--os-line);border-radius:20px}.os-dot{width:7px;height:7px;border-radius:50%;background:#638d73}.os-state[data-state=needs_attention] .os-dot,.os-state[data-state=waiting] .os-dot{background:#b68a34}.os-state[data-state=paused] .os-dot{background:#8a92a0}
.os-index-controls{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;margin-top:22px;padding:16px 0;border-top:1px solid var(--os-line);border-bottom:1px solid var(--os-line)}.os-index-controls>div{min-width:0}.os-index-controls p{margin:3px 0 0;font-size:12px;color:var(--os-muted)}.os-index-controls .os-select{width:auto;max-width:100%}.os-index-controls .os-actions{flex-shrink:0;max-width:100%}.os-index-controls+.os-tabs{margin-top:18px}
.os-service-controls{margin-top:22px;padding:18px 0 0;border-top:1px solid var(--os-line)}.os-service-row{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}.os-service-row>div{min-width:0}.os-service-row>.os-actions{flex-shrink:0}.os-service-controls+.os-index-controls{margin-top:14px}.os-schedule{border-bottom:1px solid var(--os-line);padding:17px 0}.os-schedule .os-section-head{margin-bottom:8px}.os-schedule-state{font-size:12px;color:var(--os-muted);margin-left:10px}.os-schedule-editor{margin-top:20px;padding:18px;border:1px solid var(--os-line);border-radius:8px}.os-schedule-editor h3{margin:0 0 16px;font-size:15px}.os-state[data-state=stopped] .os-dot,.os-state[data-state=unknown] .os-dot{background:#8a92a0}.os-state[data-state=offline] .os-dot{background:#b84949}
.os-tabs{display:flex;gap:24px;border-bottom:1px solid var(--os-line);margin:26px 0 24px;overflow-x:auto}.os-tab{font:inherit;color:var(--os-muted);border:0;border-bottom:2px solid transparent;background:transparent;padding:0 0 12px;white-space:nowrap;cursor:pointer}.os-tab[aria-selected=true]{border-color:var(--os-blue);color:var(--os-blue);font-weight:600}.os-panel button:focus-visible,.os-panel input:focus-visible,.os-panel select:focus-visible,.os-panel textarea:focus-visible,.os-panel summary:focus-visible{outline:2px solid var(--os-blue);outline-offset:3px}.os-panel button:disabled{opacity:.48;cursor:not-allowed}.os-btn{font:inherit;font-size:13px;color:inherit;background:transparent;border:1px solid var(--os-line);border-radius:8px;min-height:34px;padding:6px 12px;cursor:pointer;white-space:normal}.os-btn:hover:enabled{background:var(--os-soft)}.os-btn.os-primary{background:var(--os-blue);border-color:var(--os-blue);color:#fff}.os-btn.os-primary:hover:enabled{filter:brightness(.94)}.os-btn.os-danger{color:#b84949}.os-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.os-section{border-top:1px solid var(--os-line);padding:22px 0}.os-section:first-child{border-top:0;padding-top:0}.os-section h2{margin:0 0 4px;font-size:16px;font-weight:600}.os-section>p{margin:0 0 17px;color:var(--os-muted);font-size:13px}.os-section-head{display:flex;justify-content:space-between;gap:14px;margin-bottom:15px;align-items:center}.os-section-head h2{margin:0}.os-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));padding:4px 0 24px;gap:18px}.os-metric small{display:block;color:var(--os-muted);font-size:12px}.os-metric strong{display:block;font-weight:550;font-size:26px;letter-spacing:-.6px;margin:5px 0}.os-metric span{font-size:12px;color:var(--os-muted)}.os-stage{display:grid;grid-template-columns:150px 1fr;gap:20px;padding:15px 0;border-bottom:1px solid var(--os-line)}.os-stage:last-child{border-bottom:0}.os-stage-title{font-weight:550}.os-stage p{margin:0;color:var(--os-muted);font-size:13px}.os-stage strong{font-weight:500}.os-root{display:grid;grid-template-columns:minmax(100px,1fr) auto;gap:8px;padding:10px 0;border-bottom:1px solid var(--os-line);font-size:13px}.os-root:last-child{border:0}.os-path{overflow-wrap:anywhere;font-family:var(--ds-font-family-code,monospace);font-size:12px}.os-note{color:var(--os-muted);font-size:12px;margin:8px 0}.os-alert{border:1px solid var(--os-line);background:var(--os-soft);border-left:3px solid var(--os-blue);padding:10px 13px;border-radius:5px;margin:12px 0;overflow-wrap:anywhere}.os-alert.os-error{border-left-color:#b84949}.os-alert p{margin:3px 0}.os-form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:15px 22px}.os-field{display:flex;flex-direction:column;gap:6px;min-width:0;font-size:13px}.os-field>span{font-weight:500}.os-field input,.os-field textarea,.os-field select,.os-select{font:inherit;color:inherit;background:var(--dsw-alias-bg-base,#fff);border:1px solid var(--os-line);border-radius:7px;padding:8px 10px;min-height:36px;width:100%}.os-field textarea{resize:vertical;min-height:82px;line-height:1.65}.os-field small{font-weight:400;color:var(--os-muted)}.os-wide{grid-column:1/-1}.os-check{display:flex;align-items:flex-start;gap:8px;cursor:pointer;font-size:13px;margin:10px 0}.os-check input{accent-color:var(--os-blue);margin-top:4px}.os-fields{border:0;margin:0;padding:0;min-width:0}.os-radio-group{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px}.os-radio{display:flex;align-items:center;gap:8px;padding:11px 15px;border:1px solid var(--os-line);border-radius:8px;cursor:pointer}.os-radio:has(input:checked){border-color:var(--os-blue);background:var(--os-soft)}.os-radio input{accent-color:var(--os-blue)}.os-save{position:sticky;bottom:0;background:var(--dsw-alias-bg-base,#fff);border-top:1px solid var(--os-line);padding:14px 0 6px;display:flex;gap:16px;justify-content:space-between;align-items:center;margin-top:18px;z-index:1}.os-save p{margin:0;font-size:12px;color:var(--os-muted)}.os-pre{font:12px/1.6 var(--ds-font-family-code,monospace);white-space:pre-wrap;overflow-wrap:anywhere;max-height:290px;overflow:auto;background:var(--os-soft);padding:12px;border-radius:6px}.os-details summary{cursor:pointer;font-size:13px;padding:8px 0}.os-db-list{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0 20px}.os-db-item{display:flex;gap:5px;align-items:center}.os-table-wrap{overflow:auto;max-height:350px;margin:10px 0}.os-table{border-collapse:collapse;width:100%;font-size:12px;text-align:left}.os-table th,.os-table td{padding:8px 10px;border-bottom:1px solid var(--os-line);vertical-align:top}.os-table th{color:var(--os-muted);font-weight:500}.os-table input{accent-color:var(--os-blue)}.os-table-picker{border:1px solid var(--os-line);border-radius:8px;padding:12px 16px;margin:10px 0}.os-table-picker>summary{cursor:pointer;font-weight:500;overflow-wrap:anywhere}.os-loading{padding:36px 0;color:var(--os-muted)}.os-preset small{display:block;color:var(--os-muted);font-size:11px}.os-preset .os-radio{flex:1;min-width:160px;align-items:flex-start}.os-empty{padding:14px 0;color:var(--os-muted);font-size:13px}.os-panel [hidden]{display:none!important}
.os-performance-metrics{grid-template-columns:repeat(2,minmax(0,1fr));padding-bottom:12px}.os-performance-metrics strong{font-size:22px;overflow-wrap:anywhere}.os-timing-summary{display:flex;flex-wrap:wrap;gap:6px 22px;font-size:13px;padding-bottom:8px}.os-timing-summary strong{font-weight:500;color:var(--os-blue)}.os-budget-state{border-left:3px solid var(--os-blue);padding:3px 0 3px 14px;margin:0 0 18px}.os-budget-state p{margin:4px 0}.os-budget-form{margin-top:14px}
@media(max-width:760px){.os-body{padding:22px 18px}.os-head{flex-wrap:wrap;gap:14px}.os-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.os-form-grid{grid-template-columns:1fr}.os-stage{grid-template-columns:1fr;gap:5px}.os-save{align-items:flex-start;flex-direction:column}.os-root{grid-template-columns:1fr}.os-tabs{gap:23px}.os-section-head{align-items:flex-start;flex-wrap:wrap}}
`;

    function Button({ children, primary, danger, ...props }) { return h('button', { type: 'button', className: `os-btn${primary ? ' os-primary' : ''}${danger ? ' os-danger' : ''}`, ...props }, children); }
    function Field({ title, hint, wide, children }) { return h('label', { className: `os-field${wide ? ' os-wide' : ''}` }, h('span', null, title), children, hint && h('small', null, hint)); }
    function Input({ value, onChange, ...props }) { return h('input', { value: value ?? '', onChange: event => onChange(event.target.value), ...props }); }
    function TextList({ title, value, onChange, hint }) {
      // Keep the raw textarea value locally so a trailing newline does not disappear while typing.
      const [raw, setRaw] = useState((value || []).join('\n'));
      const last = useRef(value);
      useEffect(() => { if (!same(value, last.current)) { setRaw((value || []).join('\n')); last.current = value; } }, [value]);
      return h(Field, { title, hint }, h('textarea', { value: raw, onChange: event => { setRaw(event.target.value); const parsed = lines(event.target.value); last.current = parsed; onChange(parsed); } }));
    }
    function Check({ checked, onChange, children, disabled }) { return h('label', { className: 'os-check' }, h('input', { type: 'checkbox', checked: Boolean(checked), disabled, onChange: e => onChange(e.target.checked) }), h('span', null, children)); }
    function Select({ value, onChange, options, ...props }) { return h('select', { value: value ?? '', onChange: event => onChange(event.target.value), ...props }, options.map(([key, text]) => h('option', { key, value: key }, text))); }
    function Details({ title = '查看详细结果', value }) { return h('details', { className: 'os-details' }, h('summary', null, title), h('pre', { className: 'os-pre' }, JSON.stringify(value, null, 2))); }
    function Metric({ title, value, note }) { return h('div', { className: 'os-metric' }, h('small', null, title), h('strong', null, value), h('span', null, note)); }
    function Stage({ title, children }) { return h('div', { className: 'os-stage' }, h('div', { className: 'os-stage-title' }, title), h('div', null, children)); }

    function Performance({ performance }) {
      if (!performance || performance.schema_version !== 1) return h('section', { className: 'os-section' },
        h('h2', null, '处理速度'), h('p', { className: 'os-note' }, '当前后台版本尚未提供性能指标。已有索引进度与服务控制仍可使用。'));
      const stages = performance.stages || {}, queue = performance.queue || {}, batch = performance.last_batch;
      const names = [['discovery', '发现文件'], ['parse', '正文解析'], ['write', '索引写入'], ['embedding', '语义计算'], ['vectors', '向量发布'], ['wait', '调度等待']];
      return h('section', { className: 'os-section', 'aria-label': '索引处理速度' },
        h('h2', null, '处理速度'), h('p', null, '最近的处理速率与队列等待，随文件类型和大小变化。'),
        h('div', { className: 'os-metrics os-performance-metrics' },
          h(Metric, { title: '正文处理速度', value: rate(performance.throughput?.files_per_second), note: `最近 ${duration(performance.throughput?.window_seconds)}，包含实际等待` }),
          h(Metric, { title: '最老任务已等待', value: duration(queue.oldest_seconds), note: `待处理 ${number(queue.pending_files)} · 新增或修改 ${number(queue.recent_files)}` })),
        h('div', { className: 'os-timing-summary' }, [['parse', '解析'], ['write', '写入'], ['embedding', '语义']].map(([key, title]) =>
          h('span', { key }, `${title} ${duration(stages[key]?.seconds)}`, stages[key]?.active > 0 && h('strong', null, ` · ${number(stages[key].active)} 项进行中`)))),
        h('details', { className: 'os-details' }, h('summary', null, '查看本次运行计时'),
          h('p', { className: 'os-note' }, `后台本次已运行 ${duration(performance.uptime_seconds)}。阶段耗时为累计计时，并行工作可能重叠，不能相加为总用时或完成百分比。`),
          h('div', { className: 'os-table-wrap' }, h('table', { className: 'os-table' },
            h('thead', null, h('tr', null, ['阶段', '累计耗时', '调用次数', '进行中'].map(title => h('th', { key: title }, title)))),
            h('tbody', null, names.map(([key, title]) => h('tr', { key }, h('td', null, title), h('td', null, duration(stages[key]?.seconds)), h('td', null, number(stages[key]?.calls)), h('td', null, number(stages[key]?.active))))))),
          batch && h('p', { className: 'os-note' }, `最近一批：${number(batch.files)} 个文件 · ${duration(batch.seconds)} · ${number(batch.workers)} 个解析进程`)));
    }

    function ServiceControls({ status, run, busy, disconnected }) {
      const [confirmForce, setConfirmForce] = useState(false);
      const service = serviceStatus(status, disconnected);
      const disabled = Boolean(busy) || service.state === 'maintenance';
      const stopped = status?.control?.desired_state === 'stopped';
      const running = status?.service?.status === 'running';
      const retry = status?.reconnect;
      const operate = action => run(action, {}, () => setConfirmForce(false));
      return h('section', { className: 'os-service-controls', 'aria-label': '后台服务控制' },
        h('div', { className: 'os-service-row' }, h('div', null,
          h('strong', { role: 'status', 'aria-live': 'polite' }, service.title),
          h('p', { className: 'os-note' }, disconnected ? '当前无法确认服务状态；可尝试手动启动，并检查下方错误信息。' : stopped ?
            '自动重连已关闭。只有手动启动或已启用的定时任务到点，才会再次启动。' :
            '异常断线会按指数退避恢复连接；主动停止会关闭自动重连。'),
          !disconnected && !stopped && retry?.state === 'waiting' && h('p', { className: 'os-note' }, `重试次数：${number(retry.attempt || 0)} · 下次重试：${date(retry.next_retry_at)}`)),
          h('div', { className: 'os-actions' },
            h(Button, { primary: !running || disconnected, disabled: disabled || running && !stopped && !disconnected, onClick: () => operate('service_start') }, '启动服务'),
            h(Button, { disabled: disabled || stopped && !running, onClick: () => operate('service_stop') }, '停止服务'),
            h(Button, { danger: true, disabled: disabled || stopped && !running, onClick: () => setConfirmForce(true), 'aria-expanded': confirmForce }, '强制结束'))),
        confirmForce && h('div', { className: 'os-alert os-error', role: 'alert' },
          h('strong', null, '强制结束这个 one_search 后台？'),
          h('p', null, '当前扫描与索引工作会立即中断，并关闭自动重连。已启用的定时任务仍可在到点后启动服务。'),
          h('div', { className: 'os-actions', style: { marginTop: 10 } },
            h(Button, { danger: true, disabled, onClick: () => operate('service_force_stop') }, '确认强制结束'),
            h(Button, { disabled: Boolean(busy), onClick: () => setConfirmForce(false) }, '取消'))));
    }

    function IndexControls({ status, run, busy, disconnected }) {
      const [minutes, setMinutes] = useState('0');
      const pause = pauseStatus(status);
      const maintenance = status?.service?.status === 'maintenance';
      const available = Boolean(status?.index) && !maintenance && !disconnected && (!status.service?.status || status.service.status === 'running');
      const disabled = Boolean(busy) || !available;
      const paused = pause.state !== 'running';
      const stopped = !disconnected && status?.service?.status === 'stopped';
      const stateText = maintenance ? '升级维护中' : stopped ? '服务已停止，索引操作不可用' : !available ? '暂停状态待确认' :
        pause.state === 'pausing' ? '正在暂停，等待当前任务收尾' : paused ? '后台索引已暂停' : '后台索引可运行';
      return h('section', { className: 'os-index-controls', 'aria-label': '后台索引控制' },
        h('div', null, h('strong', { role: 'status', 'aria-live': 'polite' }, stateText),
          h('p', null, stopped ? '启动服务后，可单独暂停索引并保留检索能力。' : !available ? '连接恢复后自动更新状态与操作入口。' : '暂停文件扫描、正文解析、语义索引与数据库同步；已有索引仍可检索。'),
          available && paused && h('p', null, pause.until ? `${date(pause.until)} 自动恢复` : '直到手动恢复')),
        h('div', { className: 'os-actions' }, !paused && h(Select, { className: 'os-select', 'aria-label': '暂停时长', value: minutes, disabled,
          options: [['0','直到手动恢复'],['15','15 分钟'],['30','30 分钟'],['60','1 小时']], onChange: setMinutes }),
          h(Button, { onClick: () => paused ? run('resume') : run('pause', { seconds: Number(minutes) ? Number(minutes) * 60 : null }), disabled }, paused ? '恢复索引' : '暂停索引')));
    }

    function Overview({ status, run, busy }) {
      const [path, setPath] = useState('');
      const [pathResult, setPathResult] = useState(null);
      const [modelPath, setModelPath] = useState('');
      const index = status?.index || {};
      const progress = index.progress;
      const model = index.semantic?.lifecycle || {};
      const policy = progress?.runtime_policy || index.runtime_policy || {};
      if (status?.service?.status === 'maintenance') return h('p', { className: 'os-muted' }, '扫描与索引进度将在升级完成、后台重新连接后继续显示。');
      if (status?.service?.status === 'stopped') return h('p', { className: 'os-muted' }, '后台服务已停止。可在上方手动启动，或在“定时启动”中安排任务。');
      if (status?.service?.status === 'offline') return h('p', { className: 'os-muted' }, '后台尚未连接，当前扫描进度不可用。连接恢复后会自动更新。');
      if (!progress) return h('div', { className: 'os-loading' }, status ? '进度尚不可用。请检查后台服务状态或升级 one_search。' : '正在读取后台状态…');
      const content = progress.content || {}, semantic = progress.semantic || {}, discovery = progress.discovery || {};
      const errors = progress.error_summary || {};
      const failed = ['error','budget','encrypted','partial'].reduce((sum, key) => sum + (content.counts?.[key] || 0), 0);
      const hasErrors = failed > 0 || errors.last_error || errors.vector_error || semantic.enabled && errors.semantic_error || Object.keys(errors.source_errors || {}).length > 0 || (errors.scan_errors?.count || 0) > 0 || (errors.unavailable_roots || []).length > 0 || errors.discovery_error;
      const sources = Object.entries(progress.databases?.sources || {});
      const pathAction = action => run(action, { path: path.trim() }, result => setPathResult({ action, result }));
      return h(React.Fragment, null,
        h('div', { className: 'os-metrics' },
          h(Metric, { title: '已发现文件', value: number(progress.known_unique_files), note: '当前已知的唯一文件' }),
          h(Metric, { title: '正文待处理', value: number(content.pending), note: `${number(content.retry_waiting)} 项等待重试` }),
          h(Metric, { title: '语义片段', value: `${number(semantic.embedded)} / ${number(semantic.eligible)}`, note: '已计算 / 当前可计算' }),
          h(Metric, { title: '内存占用', value: mb(progress.resources?.rss_mb), note: '后台及其工作进程' })),
        h(Performance, { performance: progress.performance || index.performance }),
        h('section', { className: 'os-section' },
          h('div', { className: 'os-section-head' }, h('h2', null, '扫描与建立索引'),
            h(Button, { onClick: () => run('scan'), disabled: busy }, '重新扫描')),
          (policy.reason || progress.overall?.reason && progress.overall.reason !== 'known_tasks_pending') && h('div', { className: 'os-alert' }, label(policy.reason || progress.overall.reason), policy.pause_until && ` · ${date(policy.pause_until)} 自动恢复`),
          h(Stage, { title: '文件发现' }, h('strong', null, discovery.active ? '正在发现文件' : discovery.complete ? '本轮配置范围已扫描' : '等待扫描'),
            h('p', null, `${number(discovery.queued_directories)} 个目录等待扫描。首次扫描总量未知，不显示整机百分比。`)),
          h(Stage, { title: '正文解析' }, h('strong', null, `已解析 ${number(content.counts?.ready || 0)} · 待处理 ${number(content.pending)} · 失败或不完整 ${number(failed)}`),
            h('p', null, `仅文件名 ${number((content.counts?.metadata || 0) + (content.counts?.unsupported || 0))} · 文件变动队列 ${number(content.queued_events)} 项${content.next_retry_at ? ` · 下次重试 ${date(content.next_retry_at)}` : ''}`)),
          h(Stage, { title: '语义索引' }, h('strong', null, !semantic.enabled ? '已关闭' : `模型${label(semantic.model_state)} · ${errors.semantic_error ? '语义计算需要关注' : !model.ready ? '等待模型就绪' : semantic.active ? '正在计算语义片段' : semantic.vector_building ? '正在构建检索索引' : semantic.vector_pending ? '等待更新检索索引' : '当前向量批次已处理'}`),
            semantic.enabled && errors.semantic_error && h('p', null, `错误码：${errors.semantic_error}。正文检索仍可使用，详情见“需要关注”。`),
            h('p', null, '文件发现与正文解析会增加待计算片段。文件名与已解析正文可先使用。')),
          h(Stage, { title: '数据库同步' }, sources.length ? sources.map(([name, source]) => h('div', { key: name },
            h('strong', null, name), Object.entries(source.tables || {}).map(([table, data]) => h('p', { key: table }, `${table} · ${label(data.phase)} · 本轮读取 ${number(data.scanned_rows)} 行 / ${number(data.pages)} 页`, data.last_error && ` · ${data.last_error}`)),
            source.last_error && h('p', null, source.last_error))) : h('p', null, '未配置正文同步任务。可在“数据库”中连接来源并选择表字段。')),
          h('p', { className: 'os-note' }, '后台会持续核对新增与修改；“当前已知任务已处理”不代表电脑上的全部内容均已索引。')),
        h('section', { className: 'os-section' }, h('h2', null, '磁盘与目录'), (discovery.roots || []).length ? discovery.roots.map(root => h('div', { className: 'os-root', key: root.path },
          h('span', { className: 'os-path' }, root.path), h('span', { className: 'os-muted' }, `${label(root.phase)} · 本轮遍历记录 ${number(root.observed_files)} · 错误 ${number(root.errors)}`))) : h('p', { className: 'os-empty' }, '等待发现配置范围内的磁盘或目录。')),
        hasErrors && h('section', { className: 'os-section' }, h('h2', null, '需要关注'),
          h('p', null, '查看失败来源，或用下面的路径诊断定位某个文件。'), h(Details, { title: '查看失败与跳过详情', value: { ...errors, content_statuses: content.counts } })),
        h('section', { className: 'os-section' }, h('h2', null, '找不到某个文件？'), h('p', null, '输入 DSH 所在电脑上的完整路径，检查范围与解析状态，或优先刷新该目录。'),
          h(Field, { title: '文件或目录路径' }, h(Input, { value: path, onChange: value => { setPath(value); setPathResult(null); }, placeholder: '例如 D:\\资料\\项目', disabled: busy })),
          h('div', { className: 'os-actions', style: { marginTop: 12 } }, h(Button, { onClick: () => pathAction('diagnose_path'), disabled: busy || !path.trim() }, '诊断路径'), h(Button, { onClick: () => pathAction('refresh_path'), disabled: busy || !path.trim() }, '刷新此路径')),
          pathResult && h(Details, { title: `${pathResult.action === 'diagnose_path' ? '诊断' : '刷新'}结果`, value: pathResult.result })),
        semantic.enabled && h('section', { className: 'os-section' }, h('h2', null, '本地语义模型'), h('p', null, `状态：${label(model.state || semantic.model_state)}。模型准备期间仍可检索文件名和已解析正文。`),
          model.error && h('div', { className: 'os-alert os-error' }, model.error.message || model.error.code),
          model.progress && Object.keys(model.progress).length > 0 && h(Details, { title: '模型准备进度', value: model.progress }),
          h('div', { className: 'os-actions' }, h(Button, { onClick: () => run('model_start'), disabled: busy || ['running','queued','cancelling','ready'].includes(model.state) }, ['failed','interrupted','cancelled'].includes(model.state) ? '重试准备模型' : '下载并准备模型'),
            ['running','queued','cancelling'].includes(model.state) && h(Button, { onClick: () => run('model_cancel'), disabled: busy || model.state === 'cancelling' }, '取消准备')),
          h('div', { style: { marginTop: 14 } }, h(Field, { title: '已有离线模型目录', hint: '填写服务器本地目录；浏览器上的文件不会自动上传。' }, h(Input, { value: modelPath, onChange: setModelPath, disabled: busy }))),
          h(Button, { onClick: () => run('model_import', { source: modelPath.trim() }), disabled: busy || !modelPath.trim() || ['running','queued','cancelling'].includes(model.state), style: { marginTop: 10 } }, '导入离线模型')));
    }

    function Scope({ values, update }) {
      const indexing = values.indexing;
      const layer = (name, title) => h('section', { className: 'os-section', key: name }, h('h2', null, title),
        h('p', null, name === 'content' ? '文件名检索保留在上面的基础范围内；正文只处理支持的文件类型。' : '语义范围受正文范围约束。范围越小，建立索引的计算量越少。'),
        name === 'semantic' && h(Check, { checked: values.semantic_enabled, onChange: value => update('semantic_enabled', value) }, '启用本地语义检索'),
        h('div', { className: 'os-form-grid' }, h(Field, { title: `${title}范围` }, h(Select, { value: indexing[`${name}_scope`], onChange: value => update('indexing', { ...indexing, [`${name}_scope`]: value }), options: [['all','跟随检索范围'],['directories','指定目录'],['none','不建立此类索引']] })),
          indexing[`${name}_scope`] === 'directories' && h(TextList, { title: `${title}目录`, hint: '每行一个服务器本地绝对路径', value: indexing[`${name}_roots`], onChange: value => update('indexing', { ...indexing, [`${name}_roots`]: value }) }),
          h(TextList, { title: '仅处理这些扩展名', hint: '留空表示支持的全部类型；每行一个，如 .pdf', value: indexing[`${name}_extensions`], onChange: value => update('indexing', { ...indexing, [`${name}_extensions`]: value }) }),
          h(TextList, { title: '排除这些目录或文件', hint: '每行一个绝对路径', value: indexing[`${name}_exclude_paths`], onChange: value => update('indexing', { ...indexing, [`${name}_exclude_paths`]: value }) })));
      return h(React.Fragment, null, h('section', { className: 'os-section' }, h('h2', null, '在哪里检索'), h('p', null, '“整机”自动发现服务账号可访问的本地固定磁盘。无权限的目录会在进度中反馈。'),
        h('div', { className: 'os-radio-group' }, [['machine','整个电脑 / 服务器'],['directories','指定目录']].map(([key,text]) => h('label', { className: 'os-radio', key }, h('input', { type: 'radio', name: 'one-search-scope', checked: values.scope === key, onChange: () => { update('scope', key); if (key === 'machine') update('roots', []); } }), text))),
        h('div', { className: 'os-form-grid' }, values.scope === 'directories' && h(TextList, { title: '检索目录', hint: '每行一个服务器本地绝对路径', value: values.roots, onChange: value => update('roots', value) }),
          h(TextList, { title: '排除路径', value: values.exclude_paths, onChange: value => update('exclude_paths', value), hint: '每行一个目录或文件的绝对路径' }),
          h(TextList, { title: '排除目录名称', value: values.exclude_names, onChange: value => update('exclude_names', value), hint: '按名称跳过目录，如 node_modules；每行一个' }))),
        layer('content', '正文索引'), layer('semantic', '语义索引'),
        h('section', { className: 'os-section' }, h('h2', null, '敏感内容'), h(Check, { checked: indexing.sensitive_content_excluded, onChange: value => update('indexing', { ...indexing, sensitive_content_excluded: value }) }, '使用内置敏感内容排除模板'), h('p', { className: 'os-note' }, '保存前可预览已知索引的受影响数量；这不是尚未扫描内容的完整统计。')));
    }

    function Resources({ values, update, status, settings }) {
      const policy = values.runtime_policy;
      const change = (key, value) => update('runtime_policy', { ...policy, [key]: value });
      const resources = status?.index?.progress?.resources || status?.index?.resources || {};
      const configured = resources.configured_budget || {}, effective = resources.effective_budget;
      const resource = values.resource;
      const changeResource = (key, value) => update('resource', { ...resource, [key]: value });
      const choosePreset = key => {
        update('preset', key); change('preset', key);
        if (resource) {
          const [memory_mb, workers, memory_fraction, reserve_fraction] = { low: [768, 1, .1, .15], balanced: [4096, 4, .2, .125], fast: [8192, 8, .25, .125] }[key];
          update('resource', { ...resource, budget_mode: 'adaptive', memory_mb, workers, memory_fraction, reserve_fraction });
        }
      };
      return h(React.Fragment, null, h('section', { className: 'os-section' }, h('h2', null, '选择资源档位'), h('p', null, '降低后台索引开销会延长首次建立索引的时间，已建立的索引仍可检索。'),
        h('div', { className: 'os-radio-group os-preset' }, [['low','节省资源','较小批次，优先减少干扰'],['balanced','均衡','按可用资源调节，默认档位'],['fast','优先速度','允许更高并发与更大批次']].map(([key,title,note]) => h('label', { className: 'os-radio', key }, h('input', { type: 'radio', name: 'one-search-preset', checked: values.preset === key, onChange: () => choosePreset(key) }), h('span', null, title, h('small', null, note))))),
        resource && h('div', { className: 'os-actions' }, h(Button, { onClick: () => choosePreset(values.preset) }, '应用此档位默认值'), h('span', { className: 'os-note' }, '更新本页草稿，保存后生效。')),
        resource && h('details', { className: 'os-details' }, h('summary', null, '预算与并发上限'),
          h('p', { className: 'os-note' }, '自适应模式在这些上限内按空闲资源调整。固定模式保留指定预算；两种模式都不会为占满内存而分配无用缓存。'),
          h('div', { className: 'os-form-grid os-budget-form' },
            h(Field, { title: '预算方式' }, h(Select, { value: resource.budget_mode, options: [['adaptive', '自适应'], ['fixed', '固定']], onChange: value => changeResource('budget_mode', value) })),
            h(Field, { title: '内存硬上限（MiB）', hint: '采样预算，不是预先占用或绝对峰值保证。' }, h(Input, { type: 'number', min: 64, step: 64, value: resource.memory_mb, onChange: value => changeResource('memory_mb', Number(value)) })),
            h(Field, { title: '解析进程上限', hint: '实际并发还受 CPU、内存与待处理任务限制。' }, h(Input, { type: 'number', min: 1, max: 8, step: 1, value: resource.workers, onChange: value => changeResource('workers', Number(value)) })),
            h(Field, { title: '最多使用总内存（%）', hint: '自适应模式使用，并同时遵守内存硬上限。' }, h(Input, { type: 'number', min: 1, max: 50, step: .1, disabled: resource.budget_mode !== 'adaptive', value: Number.isFinite(resource.memory_fraction) ? Number((resource.memory_fraction * 100).toFixed(2)) : '', onChange: value => changeResource('memory_fraction', Number(value) / 100) })),
            h(Field, { title: '为系统保留总内存（%）', hint: '自适应模式使用，另保留配置的最低可用内存。' }, h(Input, { type: 'number', min: 0, max: 50, step: .1, disabled: resource.budget_mode !== 'adaptive', value: Number.isFinite(resource.reserve_fraction) ? Number((resource.reserve_fraction * 100).toFixed(2)) : '', onChange: value => changeResource('reserve_fraction', Number(value) / 100) }))))),
        h('section', { className: 'os-section' }, h('h2', null, '后台运行策略'),
          h(Check, { checked: policy.enabled, onChange: value => change('enabled', value) }, '电脑繁忙或电量偏低时自动退让'),
          h(Check, { checked: policy.idle_only, onChange: value => change('idle_only', value) }, '仅在电脑空闲时建立索引'),
          h(Check, { checked: policy.on_ac_only, onChange: value => change('on_ac_only', value) }, '仅在接通电源时建立索引'),
          h('div', { className: 'os-form-grid', style: { marginTop: 15 } }, h(Field, { title: '空闲等待（秒）' }, h(Input, { type: 'number', min: 15, max: 86400, value: policy.idle_seconds, onChange: value => change('idle_seconds', Number(value)) })),
            h(Field, { title: '繁忙阈值（整机 CPU %）' }, h(Input, { type: 'number', min: 1, max: 100, value: policy.busy_cpu_percent, onChange: value => change('busy_cpu_percent', Number(value)) })))),
        h('section', { className: 'os-section' }, h('h2', null, '实际使用与预算'),
          effective && h('div', { className: 'os-budget-state' },
            h('strong', null, `${label(effective.budget_mode)} · 当前允许 ${number(effective.parser_workers)} 个解析进程`),
            h('p', null, effective.reason === 'idle' ? '当前没有待处理任务，无需增加并发。' : label(effective.reason)),
            h('p', { className: 'os-note' }, `配置上限 ${number(configured.workers)} 个进程 · 本批最多 ${number(effective.batch_files)} 个文件 · 为系统保留 ${mb(effective.system_reserve_mb)}`)),
          h('div', { className: 'os-metrics' },
            h(Metric, { title: '实际内存 RSS', value: mb(resources.rss_mb), note: '后台及其工作进程' }),
            h(Metric, { title: '当前有效内存预算', value: effective ? mb(effective.memory_limit_mb) : '—', note: effective ? `配置上限 ${mb(configured.memory_mb)}` : `旧版配置预算 ${mb(settings.resource?.memory_mb)}` }),
            h(Metric, { title: '系统可用内存', value: mb(resources.available_mb), note: Number.isFinite(resources.system_cpu_percent) ? `整机 CPU ${number(Math.round(resources.system_cpu_percent))}%` : '整机剩余资源' }),
            h(Metric, { title: '索引空间', value: mb(resources.disk_mb), note: `磁盘剩余 ${mb(resources.free_disk_mb)}` })),
          h('p', { className: 'os-note' }, '有空余内存时可以提高允许的并发和批次；实际吞吐还取决于 CPU、磁盘和文件类型。预算不是占用目标，内存越满不代表越快。RSS 不包含浏览器与 DSH 模型；磁盘占用定期校准。')));
    }

    function Databases({ values, update, request, task, busy, onDirty }) {
      const [editor, setEditor] = useState(null);
      const [originalId, setOriginalId] = useState(null);
      const [catalog, setCatalog] = useState(null);
      const [selections, setSelections] = useState({});
      const [secret, setSecret] = useState('');
      const [auth, setAuth] = useState('none');
      const [report, setReport] = useState(null);
      const [removeId, setRemoveId] = useState(null);
      const open = source => {
        setEditor(clone(source || { id: '', kind: 'sqlite', path: '', allowed_tables: [], allowed_columns: {}, index: [] }));
        setOriginalId(source?.id || null); setCatalog(null); setSelections({}); setSecret(''); setReport(null);
        setAuth(source?.credential_ref ? 'vault' : source?.password_env ? 'environment' : 'none'); onDirty(true);
      };
      const edit = (key, value) => { setEditor(current => ({ ...current, [key]: value })); setCatalog(null); setReport(null); };
      const close = () => { setEditor(null); setCatalog(null); setSecret(''); onDirty(false); };
      async function prepared() {
        let source = connectionSource(editor);
        if (source.kind !== 'sqlite') {
          if (auth === 'vault') {
            delete source.password_env;
            if (secret) {
              // A fresh credential avoids changing an existing live source before settings save.
              const stored = await request('credential_store', { secret });
              source.credential_ref = stored.credential_ref; setSecret('');
              setEditor(current => ({ ...current, credential_ref: stored.credential_ref }));
            }
            if (!source.credential_ref) throw new Error('请填写新密码，或使用已保存的凭据引用。');
          } else if (auth === 'environment') { delete source.credential_ref; if (!source.password_env?.trim()) throw new Error('请填写密码环境变量名称。'); }
          else { delete source.credential_ref; delete source.password_env; }
        }
        return source;
      }
      const discover = () => task('发现表与字段', async () => {
        const source = await prepared(); const result = await request('db_discover', { source });
        if (!result.ok) throw new Error(diagnosticMessage(result));
        setEditor(source); setCatalog(result); setSelections(Object.fromEntries(result.tables.map(table => [table.table, selectionFor(table, source)]))); setReport(null);
      });
      const stage = () => task('检查数据库配置', async () => {
        let source = await prepared();
        if (catalog) { const proposal = await request('db_propose', { source, selections: packSelections(selections) }); if (!proposal.ok) throw new Error(diagnosticMessage(proposal)); source = proposal.source; }
        else if (!originalId) throw new Error('请先发现结构，明确选择允许读取的表与字段。');
        if (!source.allowed_tables?.length) throw new Error('请明确选择至少一张表和允许读取的字段。');
        const result = await request('db_preflight', { source }); setReport(result);
        if (!result.ok) throw new Error(diagnosticMessage(result));
        if (values.databases.some(item => item.id === source.id && item.id !== originalId)) throw new Error('来源名称已存在，请使用另一个名称。');
        const next = values.databases.filter(item => item.id !== originalId); next.push(source);
        update('databases', next); close();
        return '数据库已加入待保存设置；点击页面底部“保存并应用”后生效。';
      });
      const choose = (name, patch) => setSelections(current => ({ ...current, [name]: { ...current[name], ...patch } }));
      return h('section', { className: 'os-section' },
        h('div', { className: 'os-section-head' }, h('h2', null, '连接数据库'), h(Button, { onClick: () => open(), disabled: busy || Boolean(editor) }, '添加数据库')),
        h('p', null, '支持 SQLite、MySQL 和 PostgreSQL。使用只读账号，只开放明确选择的表与字段。'),
        h('div', { className: 'os-db-list' }, values.databases.length ? values.databases.map(source => h('div', { className: 'os-db-item', key: source.id },
          h(Button, { onClick: () => open(source), disabled: busy || Boolean(editor) }, `${source.id} · ${source.kind}`),
          h(Button, { 'aria-label': `移除 ${source.id}`, onClick: () => setRemoveId(source.id), disabled: busy || Boolean(editor), danger: true }, '移除'))) : h('p', { className: 'os-empty' }, '尚未连接数据库。文件检索不受影响。')),
        removeId && h('div', { className: 'os-alert' }, h('p', null, `移除 ${removeId} 的检索授权？保存后清理对应索引，不修改源数据库。`), h('div', { className: 'os-actions' }, h(Button, { onClick: () => { update('databases', values.databases.filter(x => x.id !== removeId)); setRemoveId(null); }, disabled: busy, danger: true }, '从待保存设置移除'), h(Button, { onClick: () => setRemoveId(null) }, '取消'))),
        editor && h('div', null,
          h('div', { className: 'os-alert' }, '正在编辑数据库。先检查并加入设置，再保存整个页面。'),
          h('div', { className: 'os-form-grid' }, h(Field, { title: '来源名称' }, h(Input, { value: editor.id, onChange: value => edit('id', value) })),
            h(Field, { title: '类型' }, h(Select, { value: editor.kind, options: [['sqlite','SQLite'],['mysql','MySQL'],['postgres','PostgreSQL']], onChange: value => { setEditor({ id: editor.id, kind: value, path: '', host: '127.0.0.1', port: value === 'mysql' ? 3306 : 5432, database: '', user: '', allowed_tables: [], allowed_columns: {}, index: [] }); setCatalog(null); setReport(null); setSecret(''); setAuth('none'); } })),
            editor.kind === 'sqlite' ? h(Field, { title: 'SQLite 文件完整路径', wide: true, hint: '路径位于 DSH 所在电脑；以只读方式打开。' }, h(Input, { value: editor.path, onChange: value => edit('path', value) })) : h(React.Fragment, null,
              h(Field, { title: '服务器地址' }, h(Input, { value: editor.host, onChange: value => edit('host', value) })),
              h(Field, { title: '端口' }, h(Input, { type: 'number', value: editor.port, onChange: value => edit('port', value) })),
              h(Field, { title: '数据库名称' }, h(Input, { value: editor.database, onChange: value => edit('database', value) })),
              h(Field, { title: '只读账号' }, h(Input, { autoComplete: 'off', value: editor.user, onChange: value => edit('user', value) })),
              h(Field, { title: '认证方式' }, h(Select, { value: auth, onChange: value => { setAuth(value); setCatalog(null); setSecret(''); }, options: [['none','显式无密码'],['vault','系统凭据库'],['environment','环境变量']] })),
              auth === 'vault' && h(React.Fragment, null,
                h(Field, { title: '新密码', hint: editor.credential_ref ? '留空复用当前凭据；密码不会读回或保存到配置。' : '发现结构时保存到系统凭据库；即使放弃本页编辑，已创建的凭据引用仍保留。' }, h(Input, { type: 'password', autoComplete: 'new-password', value: secret, onChange: value => { setSecret(value); setCatalog(null); } })),
                h(Field, { title: '已有凭据引用（可选）' }, h(Input, { value: editor.credential_ref, onChange: value => edit('credential_ref', value) }))),
              auth === 'environment' && h(Field, { title: '密码环境变量名称', hint: '变量必须存在于 DSH 服务启动环境中。' }, h(Input, { value: editor.password_env, onChange: value => edit('password_env', value) })),
              editor.kind === 'postgres' && h(Field, { title: 'TLS 模式' }, h(Select, { value: editor.ssl?.sslmode || '', onChange: value => edit('ssl', { ...editor.ssl, sslmode: value }), options: [['','使用驱动默认值'],['require','require'],['verify-ca','verify-ca'],['verify-full','verify-full'],['disable','disable']] })),
              h(Field, { title: 'TLS CA 证书路径（可选）' }, h(Input, { value: editor.ssl?.[editor.kind === 'postgres' ? 'sslrootcert' : 'ca'], onChange: value => edit('ssl', { ...editor.ssl, [editor.kind === 'postgres' ? 'sslrootcert' : 'ca']: value }) }))),
            h(Field, { title: '业务别名（可选）' }, h(Input, { value: editor.business_metadata?.alias, onChange: value => edit('business_metadata', { ...editor.business_metadata, alias: value }) })),
            h(Field, { title: '业务说明（可选）' }, h(Input, { value: editor.business_metadata?.description, onChange: value => edit('business_metadata', { ...editor.business_metadata, description: value }) }))),
          h('div', { className: 'os-actions', style: { marginTop: 17 } }, h(Button, { onClick: discover, disabled: busy, primary: true }, catalog ? '重新发现结构' : '发现表与字段'), originalId && !catalog && h('span', { className: 'os-note' }, `保留已有 ${editor.allowed_tables?.length || 0} 张表的授权；发现后可调整。`)),
          catalog && h('div', { style: { marginTop: 18 } }, h('h3', null, '选择允许读取的数据'), h('p', { className: 'os-note' }, '发现结构不会开放数据。逐列勾选“允许读取”；需要持续搜索正文时，再勾选“正文索引”。'),
            catalog.truncated && h('div', { className: 'os-alert' }, '本次仅显示前 64 张表。更大范围可通过配置文件管理。'),
            catalog.tables.map(table => { const state = selections[table.table]; if (!state) return null; const keys = table.index_recommendation?.key_candidates || [];
              return h('details', { className: 'os-table-picker', key: table.table, open: state.enabled || undefined }, h('summary', null, `${table.table}${state.enabled ? ' · 已选择' : ''}`),
                h(Check, { checked: state.enabled, onChange: value => choose(table.table, { enabled: value }) }, '允许读取此表'),
                state.enabled && h(React.Fragment, null,
                  h('div', { className: 'os-table-wrap' }, h('table', { className: 'os-table' }, h('thead', null, h('tr', null, h('th', null, '字段'), h('th', null, '类型'), h('th', null, '允许读取'), h('th', null, '正文索引'))),
                    h('tbody', null, table.columns.map(column => h('tr', { key: column.name }, h('td', null, column.name), h('td', { className: 'os-muted' }, column.type),
                      h('td', null, h('input', { type: 'checkbox', 'aria-label': `${table.table}.${column.name} 允许读取`, checked: state.columns.includes(column.name), onChange: e => choose(table.table, { columns: e.target.checked ? [...state.columns, column.name] : state.columns.filter(x => x !== column.name), index_text_columns: e.target.checked ? state.index_text_columns : state.index_text_columns.filter(x => x !== column.name) }) })),
                      h('td', null, h('input', { type: 'checkbox', 'aria-label': `${table.table}.${column.name} 正文索引`, disabled: !state.columns.includes(column.name) || !keys.length, checked: state.index_text_columns.includes(column.name), onChange: e => choose(table.table, { index_text_columns: e.target.checked ? [...state.index_text_columns, column.name] : state.index_text_columns.filter(x => x !== column.name) }) }))))))),
                  !keys.length && h('p', { className: 'os-note' }, '没有可验证的稳定单列唯一键，此表仅提供实时查询。'),
                  state.index_text_columns.length > 0 && h('div', { className: 'os-form-grid' }, h(Field, { title: '索引唯一键' }, h(Select, { value: state.id_column, onChange: value => choose(table.table, { id_column: value }), options: [['','请选择'], ...keys.filter(x => state.columns.includes(x.column)).map(x => [x.column,x.column])] })),
                    h(Field, { title: '更新水位（可选）', hint: '留空采用周期全量核对；有水位也会定期核对删除。' }, h(Select, { value: state.updated_column, onChange: value => choose(table.table, { updated_column: value, watermark_confirmed: false }), options: [['','不使用水位'], ...table.columns.filter(x => state.columns.includes(x.name)).map(x => [x.name,x.name])] })),
                    state.updated_column && h('div', { className: 'os-wide' }, h(Check, { checked: state.watermark_confirmed, onChange: value => choose(table.table, { watermark_confirmed: value }) }, '确认源系统会在每次新增或更新时维护该水位字段')))));
            })),
          report && h(Details, { title: report.ok ? '只读预检通过' : '查看预检问题', value: report }),
          h('div', { className: 'os-actions', style: { marginTop: 20 } }, h(Button, { onClick: stage, disabled: busy, primary: true }, '检查并加入待保存设置'), h(Button, { onClick: close, disabled: busy }, '放弃数据库编辑'))));
    }

    function Schedules({ active, request, run, busy }) {
      const [snapshot, setSnapshot] = useState(null);
      const [error, setError] = useState(null);
      const [editor, setEditor] = useState(null);
      const [validation, setValidation] = useState(null);
      const [deleting, setDeleting] = useState(null);
      const poller = useRef(null), epoch = useRef(0);
      useEffect(() => {
        if (!active) return;
        poller.current = createPoller({ document, intervalMs: 30000, maxDelay: 60000,
          request: async () => { const revision = epoch.current; return { revision, value: await request('schedules_get') }; },
          onValue: ({ revision, value }) => { if (revision === epoch.current) { setSnapshot(value); setError(null); } },
          onError: failure => setError(failure) });
        return () => { poller.current?.dispose(); poller.current = null; };
      }, [active, request]);
      useEffect(() => {
        if (!editor) return;
        const warn = event => { event.preventDefault(); event.returnValue = ''; };
        window.addEventListener('beforeunload', warn);
        return () => window.removeEventListener('beforeunload', warn);
      }, [Boolean(editor)]);
      const timezone = snapshot?.timezone;
      const accept = value => { epoch.current += 1; setSnapshot(value); setError(null); setEditor(null); setDeleting(null); setValidation(null); poller.current?.refresh(); };
      const edit = task => { setEditor({ ...scheduleDraft(task, timezone), revision: snapshot.revision }); setValidation(null); setDeleting(null); };
      const update = (key, value) => { setEditor(current => ({ ...current, [key]: value })); setValidation(null); };
      const save = () => {
        try { const task = scheduleTask(editor, timezone); return run('schedule_save', { revision: editor.revision, task }, accept); }
        catch (failure) { setValidation(failure); }
      };
      const toggle = task => run('schedule_save', { revision: snapshot.revision, task: { id: task.id, name: task.name, enabled: !task.enabled, schedule: task.schedule } }, accept);
      const weekdays = [[1,'周一'],[2,'周二'],[3,'周三'],[4,'周四'],[5,'周五'],[6,'周六'],[7,'周日']];
      const summary = task => task.schedule.kind === 'once' ? `仅一次 · ${scheduleDate(task.schedule.at, timezone)}` :
        `${task.schedule.kind === 'daily' ? '每天' : (task.schedule.weekdays || []).map(day => weekdays.find(([key]) => key === day)?.[1]).join('、')} ${task.schedule.time}`;
      const resultText = result => !result ? '尚未执行' : ({ success: '启动成功', succeeded: '启动成功', started: '启动成功', triggered: '已触发，结果待确认', running: '正在执行', failed: '执行失败', skipped: '已跳过' })[result.status] || label(result.status);
      return h('section', { className: 'os-section', 'aria-label': '定时启动管理' },
        h('div', { className: 'os-section-head' }, h('h2', null, '定时启动'), h('div', { className: 'os-actions' },
          h(Button, { onClick: () => poller.current?.refresh(), disabled: busy }, '刷新任务'),
          h(Button, { primary: true, onClick: () => edit(), disabled: busy || !snapshot || Boolean(editor) }, '新建启动任务'))),
        h('p', null, '由服务所在电脑的系统调度器执行；关闭网页或 DSH 后仍有效。任务到点会解除主动停止状态并启动服务。'),
        timezone && h('p', { className: 'os-note' }, `时间均按服务所在电脑的时区：${timezone.name || '本地时区'} (UTC${timezone.offset})。服务器当前时间：${scheduleDate(timezone.now, timezone)}。`),
        snapshot?.scheduler?.note && h('p', { className: 'os-note' }, snapshot.scheduler.note),
        error && h('div', { className: 'os-alert os-error', role: 'alert' }, errorContent(error), snapshot && h('p', null, '下方为上次读取的任务。编辑中的内容已保留。')),
        !snapshot && !error && h('p', { className: 'os-loading' }, '正在读取定时任务…'),
        snapshot && !snapshot.tasks?.length && h('p', { className: 'os-empty' }, '尚无定时任务。主动停止后，服务会保持停止，直到手动启动。'),
        (snapshot?.tasks || []).map(task => h('article', { className: 'os-schedule', key: task.id },
          h('div', { className: 'os-section-head' }, h('div', null, h('strong', null, task.name), h('span', { className: 'os-schedule-state' }, task.enabled ? '已启用' : '已禁用'), h('p', { className: 'os-note' }, summary(task))),
            h('div', { className: 'os-actions' }, h(Button, { disabled: busy || Boolean(editor), onClick: () => toggle(task) }, task.enabled ? '禁用' : '启用'),
              h(Button, { disabled: busy || Boolean(editor), onClick: () => edit(task) }, '编辑'),
              h(Button, { danger: true, disabled: busy || Boolean(editor), onClick: () => setDeleting(task.id) }, '删除'))),
          h('p', { className: 'os-note' }, `下次执行：${task.enabled ? scheduleDate(task.next_run_at, timezone) : '已禁用'} · 上次执行：${scheduleDate(task.last_run_at, timezone)}`),
          h('p', { className: 'os-note' }, `上次结果：${resultText(task.last_result)}`, task.last_result?.code && h(React.Fragment, null, ' · 错误码：', h('code', null, task.last_result.code))),
          deleting === task.id && h('div', { className: 'os-alert', role: 'alert' }, h('p', null, `删除“${task.name}”的启动任务？不会停止当前运行的服务。`),
            h('div', { className: 'os-actions' }, h(Button, { danger: true, disabled: busy, onClick: () => run('schedule_delete', { revision: snapshot.revision, id: task.id }, accept) }, '确认删除任务'),
              h(Button, { disabled: busy, onClick: () => setDeleting(null) }, '取消删除'))))),
        editor && h('section', { className: 'os-schedule-editor', 'aria-label': editor.id ? '编辑启动任务' : '新建启动任务' },
          h('h3', null, editor.id ? '编辑启动任务' : '新建启动任务'),
          h('fieldset', { className: 'os-fields', disabled: busy }, h('div', { className: 'os-form-grid' },
            h(Field, { title: '任务名称' }, h(Input, { value: editor.name, maxLength: 80, onChange: value => update('name', value), placeholder: '例如：工作日开始前启动' })),
            h(Field, { title: '重复方式' }, h(Select, { value: editor.kind, onChange: value => update('kind', value), options: [['once','仅一次'],['daily','每天'],['weekly','每周']] })),
            editor.kind === 'once' && h(Field, { title: '启动日期' }, h(Input, { type: 'date', value: editor.date, onChange: value => update('date', value) })),
            h(Field, { title: '启动时间', hint: `服务所在电脑时间 (UTC${timezone?.offset || '—'})` }, h(Input, { type: 'time', value: editor.time, onChange: value => update('time', value) })),
            editor.kind === 'weekly' && h('div', { className: 'os-wide' }, h('span', null, '每周执行日期'), h('div', { className: 'os-actions' }, weekdays.map(([day, title]) => h(Check, { key: day, checked: editor.weekdays.includes(day), onChange: enabled => update('weekdays', enabled ? [...editor.weekdays, day] : editor.weekdays.filter(value => value !== day)) }, title))))),
            h(Check, { checked: editor.enabled, onChange: value => update('enabled', value) }, '启用此任务'),
            validation && h('div', { className: 'os-alert os-error', role: 'alert' }, validation.message),
            h('div', { className: 'os-actions' }, h(Button, { primary: true, disabled: busy, onClick: save }, '保存启动任务'), h(Button, { disabled: busy, onClick: () => { setEditor(null); setValidation(null); } }, '放弃编辑')))));
    }

    function Panel({ request }) {
      const [tab, setTab] = useState('overview');
      const [status, setStatus] = useState(null);
      const [settings, setSettings] = useState(null);
      const [draft, setDraft] = useState(null);
      const [busy, setBusy] = useState('');
      const [notice, setNotice] = useState(null);
      const [connectionError, setConnectionError] = useState(null);
      const [settingsError, setSettingsError] = useState(null);
      const [preview, setPreview] = useState(null);
      const [conflict, setConflict] = useState(false);
      const [dbDirty, setDbDirty] = useState(false);
      const [discard, setDiscard] = useState(false);
      const [editorEpoch, setEditorEpoch] = useState(0);
      const mounted = useRef(true), lock = useRef(false), poller = useRef(null), draftRef = useRef(null), statusEpoch = useRef(0);
      draftRef.current = draft;
      const maintenance = status?.service?.status === 'maintenance';
      const managementDisabled = Boolean(busy) || maintenance;
      const serviceUnavailable = Boolean(connectionError) || ['offline', 'stopped'].includes(status?.service?.status);
      const controlsDisabled = managementDisabled || serviceUnavailable;
      const dirty = Boolean(draft && !same(draft.values, draft.original));
      useEffect(() => {
        mounted.current = true;
        let active = true, settingsRunning = false, settingsFailures = 0, retrySettingsAt = 0, disconnected = false;
        async function readMissingSettings(force = false) {
          if (!active || draftRef.current || settingsRunning || (!force && Date.now() < retrySettingsAt)) return;
          settingsRunning = true;
          try {
            const value = await request('settings_get');
            if (active && !draftRef.current) {
              const next = freshDraft(value); draftRef.current = next;
              setSettings(value); setDraft(next); setSettingsError(null);
            }
          } catch (error) {
            settingsFailures += 1; retrySettingsAt = Date.now() + Math.min(30000, 2000 * 2 ** settingsFailures);
            if (active) setSettingsError(error);
          } finally { settingsRunning = false; }
        }
        poller.current = createPoller({ document, request: async () => { const epoch = statusEpoch.current; return { epoch, value: await request('status') }; },
          onValue: ({ epoch, value }) => {
            if (epoch !== statusEpoch.current) return;
            setStatus(value); setConnectionError(null);
            const recovered = disconnected; disconnected = false;
            if (!['maintenance', 'offline', 'stopped'].includes(value.service?.status)) readMissingSettings(recovered);
          },
          onError: (error, delay) => { disconnected = true; setConnectionError({ error, delay }); } });
        readMissingSettings();
        return () => { active = false; mounted.current = false; poller.current?.dispose(); };
      }, [request]);
      useEffect(() => {
        if (!dirty && !dbDirty) return;
        const warn = event => { event.preventDefault(); event.returnValue = ''; };
        window.addEventListener('beforeunload', warn);
        return () => window.removeEventListener('beforeunload', warn);
      }, [dirty, dbDirty]);
      const update = (key, value) => { setDraft(current => ({ ...current, values: { ...current.values, [key]: value } })); setPreview(null); setNotice(null); };
      async function task(title, operation) {
        if (lock.current || maintenance) return;
        lock.current = true; setBusy(title); setNotice(null);
        try { const message = await operation(); if (mounted.current) { setNotice({ text: typeof message === 'string' ? message : `${title}已完成。`, error: false }); poller.current?.refresh(); } }
        catch (error) { if (mounted.current) { if (error.code === 'revision_conflict' && ['保存并应用', '预览设置影响'].includes(title)) setConflict(true); setNotice({ detail: error, error: true }); } }
        finally { lock.current = false; if (mounted.current) setBusy(''); }
      }
      const run = (action, params = {}, after) => task(({ pause: '暂停请求', resume: '恢复请求', scan: '重新扫描请求', refresh_path: '路径刷新请求', diagnose_path: '路径诊断', model_start: '模型准备请求', model_import: '模型导入请求', model_cancel: '模型取消请求', service_start: '启动服务', service_stop: '停止服务', service_force_stop: '强制结束', schedule_save: '保存启动任务', schedule_delete: '删除启动任务' })[action] || '操作', async () => {
        const result = await request(action, params);
        if (mounted.current) {
          after?.(result);
          if (['service_start', 'service_stop', 'service_force_stop'].includes(action) && result?.service) {
            statusEpoch.current += 1; setStatus(result); setConnectionError(null);
          }
          if (['pause', 'resume'].includes(action) && result && (typeof result.paused === 'boolean' || ['running', 'pausing', 'paused'].includes(result.pause_state))) {
            // An older in-flight sample must not undo the acknowledged action.
            statusEpoch.current += 1;
            setStatus(current => ({ ...current, index: { ...current?.index,
              pause_state: result.pause_state || (result.paused ? 'paused' : 'running'),
              runtime_policy: { ...(current?.index?.runtime_policy || current?.index?.progress?.runtime_policy),
                user_paused: result.user_paused ?? result.paused, pause_until: result.pause_until ?? null } } }));
          }
        }
        return ['pause','resume','scan','refresh_path','model_start','model_import','model_cancel'].includes(action) ? '请求已接受，后台状态会自动刷新。' : undefined;
      });
      const reload = () => task('重新加载设置', async () => { const value = await request('settings_get'); if (!mounted.current) return; setSettings(value); setDraft(freshDraft(value)); setSettingsError(null); setConflict(false); setPreview(null); setDbDirty(false); setEditorEpoch(x => x + 1); setDiscard(false); });
      const save = () => task('保存并应用', async () => {
        const current = draftRef.current; const value = await request('settings_save', { revision: current.revision, values: current.values });
        if (!mounted.current) return; setSettings(value); setDraft(freshDraft(value)); setConflict(false); setPreview(null);
        return '已保存并应用。后台将按新设置继续处理，首次扫描与正文更新可能需要一些时间。';
      });
      const previewSettings = () => task('预览设置影响', async () => { const current = draftRef.current; const value = await request('settings_preview', { revision: current.revision, values: current.values }); if (mounted.current) setPreview(value); return value.can_apply ? '预览完成，可保存并应用。' : '预览发现问题，请先修正后再保存。'; });
      const tabs = [['overview','概览'],['scope','检索范围'],['resources','资源'],['databases','数据库'],['schedules','定时启动']];
      const service = serviceStatus(status, Boolean(connectionError));
      const overallState = maintenance ? 'maintenance' : serviceUnavailable ? service.state : status?.index && pauseStatus(status).state !== 'running' ? pauseStatus(status).state :
        status?.index?.progress?.overall?.state || (status?.service?.status === 'running' ? 'running_service' : status?.service?.status);
      return h('div', { className: 'os-panel' }, h('style', null, css), h('div', { className: 'os-body' },
        h('header', { className: 'os-head' }, h('div', null, h('h1', null, 'one_search'), h('p', { className: 'os-subtitle' }, '本地资料，随时可找。')), h('span', { className: 'os-state', 'data-state': connectionError ? 'needs_attention' : overallState }, h('span', { className: 'os-dot' }), serviceUnavailable ? service.title : label(overallState || '连接中'))),
        h(ServiceControls, { status, run, busy: managementDisabled, disconnected: Boolean(connectionError) }),
        h(IndexControls, { status, run, busy: controlsDisabled, disconnected: Boolean(connectionError) }),
        h('div', { className: 'os-tabs', role: 'tablist', 'aria-label': 'one_search 设置' }, tabs.map(([key,text], index) => h('button', { key, type: 'button', className: 'os-tab', role: 'tab', id: `os-tab-${key}`, 'aria-controls': `os-content-${key}`, 'aria-selected': tab === key, tabIndex: tab === key ? 0 : -1, onClick: () => setTab(key), onKeyDown: event => { let next; if (event.key === 'ArrowRight') next = (index + 1) % tabs.length; if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length; if (event.key === 'Home') next = 0; if (event.key === 'End') next = tabs.length - 1; if (next !== undefined) { event.preventDefault(); setTab(tabs[next][0]); event.currentTarget.parentElement.children[next].focus(); } } }, text))),
        maintenance && h('div', { className: 'os-alert', role: 'status' }, h('strong', null, '正在升级或恢复 one_search'), h('p', null, '完成后会自动恢复连接。本页未保存的编辑仍保留，设置操作暂时不可用。'), h('p', null, '如果安装程序已意外退出，请重新运行同一安装命令完成恢复。')),
        connectionError && h('div', { className: 'os-alert os-error', role: 'status' }, errorContent(connectionError.error),
          h('p', { className: 'os-note' }, `${Math.ceil(connectionError.delay / 1000)} 秒后重试读取状态；状态读取不会解除主动停止。`), h(Button, { onClick: () => poller.current?.refresh() }, '立即重试')),
        !connectionError && status?.service?.error && h('div', { className: 'os-alert os-error', role: 'status' }, errorContent(status.service.error)),
        h('div', { role: 'status', 'aria-live': 'polite' }, busy ? h('div', { className: 'os-alert' }, `${busy}中…`) : notice && h('div', { className: `os-alert${notice.error ? ' os-error' : ''}` }, notice.error ? errorContent(notice.detail) : notice.text)),
        h('div', { id: 'os-content-overview', role: 'tabpanel', 'aria-labelledby': 'os-tab-overview', hidden: tab !== 'overview' }, h(Overview, { status: serviceUnavailable ? { ...status, index: undefined } : status, run, busy: controlsDisabled })),
        h('div', { id: 'os-content-schedules', role: 'tabpanel', 'aria-labelledby': 'os-tab-schedules', hidden: tab !== 'schedules' }, h(Schedules, { active: tab === 'schedules', request, run, busy: managementDisabled })),
        !draft && !['overview', 'schedules'].includes(tab) && h('div', { className: 'os-loading' }, settingsError ? errorContent(settingsError) : '正在读取设置…', settingsError && h(React.Fragment, null,
          h('p', { className: 'os-note' }, '后台恢复后会自动重新读取设置。'), h(Button, { onClick: reload, disabled: controlsDisabled }, '重新读取'))),
        draft && h('fieldset', { className: 'os-fields', disabled: controlsDisabled },
          h('div', { id: 'os-content-scope', role: 'tabpanel', 'aria-labelledby': 'os-tab-scope', hidden: tab !== 'scope' }, h(Scope, { values: draft.values, update })),
          h('div', { id: 'os-content-resources', role: 'tabpanel', 'aria-labelledby': 'os-tab-resources', hidden: tab !== 'resources' }, h(Resources, { values: draft.values, update, status, settings })),
          h('div', { id: 'os-content-databases', role: 'tabpanel', 'aria-labelledby': 'os-tab-databases', hidden: tab !== 'databases' }, h(Databases, { key: editorEpoch, values: draft.values, update, request, task, busy: controlsDisabled, onDirty: setDbDirty }))),
        preview && h('div', { className: 'os-alert' }, h('strong', null, preview.can_apply ? '设置可应用' : '请修正预检问题'),
          h('p', null, preview.scope?.available === false ? '当前无法估算范围影响。' : `已知文件撤销：${number(preview.scope?.known_files_revoked)}；已知正文缓存撤销：${number(preview.scope?.known_content_caches_revoked)}。`),
          preview.databases_changed && h('p', null, `数据库只读预检：${preview.preflight?.ok ? '通过' : '未通过'}`), h(Details, { title: '查看预览详情', value: preview })),
        conflict && h('div', { className: 'os-alert os-error' }, '设置已在其他位置修改。你的编辑仍保留；请复制需要保留的内容，再重新加载最新设置。'),
        discard && h('div', { className: 'os-alert' }, h('p', null, '重新加载会放弃本页未保存的设置与数据库编辑。'), h('div', { className: 'os-actions' }, h(Button, { onClick: reload, disabled: controlsDisabled }, '放弃并重新加载'), h(Button, { onClick: () => setDiscard(false), disabled: controlsDisabled }, '继续编辑'))),
        draft && (!['overview', 'schedules'].includes(tab) || dirty || dbDirty) && h('footer', { className: 'os-save' }, h('div', null, h('strong', { style: { fontSize: 13 } }, dbDirty ? '数据库尚在编辑' : dirty ? '有未保存的修改' : '设置已同步'), h('p', null, dbDirty ? '先完成或放弃数据库编辑。' : '保存后应用到后台服务；索引会逐步更新。')),
          h('div', { className: 'os-actions' }, h(Button, { onClick: () => dirty || dbDirty ? setDiscard(true) : reload(), disabled: controlsDisabled }, '重新加载'), h(Button, { onClick: previewSettings, disabled: controlsDisabled || !dirty || dbDirty || conflict }, '预览影响'), h(Button, { primary: true, onClick: save, disabled: controlsDisabled || !dirty || dbDirty || conflict || preview?.can_apply === false }, '保存并应用'))),
        h('p', { className: 'os-note', style: { marginTop: 20 } }, `状态更新：${date(status?.index?.progress?.sampled_at)} · 页面可见时每 2 秒读取状态 · 关闭页面不停止后台`)));
    }

    function SearchIcon({ size = 18 }) { return h('svg', { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.7, strokeLinecap: 'round', 'aria-hidden': true }, h('circle', { cx: 10, cy: 10, r: 6 }), h('path', { d: 'm14.5 14.5 5 5M7.5 10h5M10 7.5v5' })); }
    function apply(ctx) {
      const request = async (action, params = {}) => unwrap(await ctx.connection.rpc.call('/one-search', 'request', { action, params }));
      ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({ name: 'sidebar.panellist', id: 'one-search', order: 70, label: 'one_search' }, SearchIcon));
      ctx.slots.inject('main', () => ctx.slots.register({ name: 'main', key: 'one-search' }, () => h(Panel, { request })));
    }
    return { name: 'one-search-web', inject: ['slots', 'connection'], apply,
      __testing: { unwrap, createPoller, freshDraft, packSelections, selectionFor, connectionSource, lines, errorDescription, pauseStatus, serviceStatus, scheduleDraft, scheduleTask, scheduleDate, duration, Panel, Scope, Overview, Performance, Resources, IndexControls, ServiceControls, Schedules, Databases } };
  },
});
