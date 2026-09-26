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
    const mb = value => Number.isFinite(value) ? `${number(Math.round(value))} MB` : '—';
    const labels = {
      paused: '已暂停', waiting: '等待资源', indexing: '正在扫描与建索引', needs_attention: '需要关注',
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
    };
    const label = value => labels[value] || value || '—';

    function unwrap(transport) {
      if (!transport || transport.ok !== true) {
        const error = new Error(transport?.error?.message || '与 DSH 的连接中断，请稍后重试。');
        error.code = transport?.error?.code || 'connection_error';
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
    function createPoller({ request, onValue, onError, document: doc, setTimer = setTimeout, clearTimer = clearTimeout }) {
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
        catch (error) { if (!stopped) { errors += 1; onError(error, Math.min(30000, 2000 * 2 ** errors)); } }
        finally {
          running = false;
          if (!stopped) { const delay = again ? 0 : Math.min(30000, 2000 * 2 ** errors); again = false; schedule(delay); }
        }
      }
      const visibility = () => { clear(); if (visible()) tick(); };
      doc.addEventListener('visibilitychange', visibility);
      tick();
      return { refresh: tick, dispose() { stopped = true; clear(); doc.removeEventListener('visibilitychange', visibility); } };
    }

    function freshDraft(snapshot) { return { revision: snapshot.revision, values: clone(snapshot.values), original: clone(snapshot.values) }; }
    function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
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
.os-tabs{display:flex;gap:24px;border-bottom:1px solid var(--os-line);margin:26px 0 24px;overflow-x:auto}.os-tab{font:inherit;color:var(--os-muted);border:0;border-bottom:2px solid transparent;background:transparent;padding:0 0 12px;white-space:nowrap;cursor:pointer}.os-tab[aria-selected=true]{border-color:var(--os-blue);color:var(--os-blue);font-weight:600}.os-panel button:focus-visible,.os-panel input:focus-visible,.os-panel select:focus-visible,.os-panel textarea:focus-visible,.os-panel summary:focus-visible{outline:2px solid var(--os-blue);outline-offset:3px}.os-panel button:disabled{opacity:.48;cursor:not-allowed}.os-btn{font:inherit;font-size:13px;color:inherit;background:transparent;border:1px solid var(--os-line);border-radius:8px;min-height:34px;padding:6px 12px;cursor:pointer;white-space:normal}.os-btn:hover:enabled{background:var(--os-soft)}.os-btn.os-primary{background:var(--os-blue);border-color:var(--os-blue);color:#fff}.os-btn.os-primary:hover:enabled{filter:brightness(.94)}.os-btn.os-danger{color:#b84949}.os-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.os-section{border-top:1px solid var(--os-line);padding:22px 0}.os-section:first-child{border-top:0;padding-top:0}.os-section h2{margin:0 0 4px;font-size:16px;font-weight:600}.os-section>p{margin:0 0 17px;color:var(--os-muted);font-size:13px}.os-section-head{display:flex;justify-content:space-between;gap:14px;margin-bottom:15px;align-items:center}.os-section-head h2{margin:0}.os-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));padding:4px 0 24px;gap:18px}.os-metric small{display:block;color:var(--os-muted);font-size:12px}.os-metric strong{display:block;font-weight:550;font-size:26px;letter-spacing:-.6px;margin:5px 0}.os-metric span{font-size:12px;color:var(--os-muted)}.os-stage{display:grid;grid-template-columns:150px 1fr;gap:20px;padding:15px 0;border-bottom:1px solid var(--os-line)}.os-stage:last-child{border-bottom:0}.os-stage-title{font-weight:550}.os-stage p{margin:0;color:var(--os-muted);font-size:13px}.os-stage strong{font-weight:500}.os-root{display:grid;grid-template-columns:minmax(100px,1fr) auto;gap:8px;padding:10px 0;border-bottom:1px solid var(--os-line);font-size:13px}.os-root:last-child{border:0}.os-path{overflow-wrap:anywhere;font-family:var(--ds-font-family-code,monospace);font-size:12px}.os-note{color:var(--os-muted);font-size:12px;margin:8px 0}.os-alert{border:1px solid var(--os-line);background:var(--os-soft);border-left:3px solid var(--os-blue);padding:10px 13px;border-radius:5px;margin:12px 0;overflow-wrap:anywhere}.os-alert.os-error{border-left-color:#b84949}.os-alert p{margin:3px 0}.os-form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:15px 22px}.os-field{display:flex;flex-direction:column;gap:6px;min-width:0;font-size:13px}.os-field>span{font-weight:500}.os-field input,.os-field textarea,.os-field select,.os-select{font:inherit;color:inherit;background:var(--dsw-alias-bg-base,#fff);border:1px solid var(--os-line);border-radius:7px;padding:8px 10px;min-height:36px;width:100%}.os-field textarea{resize:vertical;min-height:82px;line-height:1.65}.os-field small{font-weight:400;color:var(--os-muted)}.os-wide{grid-column:1/-1}.os-check{display:flex;align-items:flex-start;gap:8px;cursor:pointer;font-size:13px;margin:10px 0}.os-check input{accent-color:var(--os-blue);margin-top:4px}.os-fields{border:0;margin:0;padding:0;min-width:0}.os-radio-group{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px}.os-radio{display:flex;align-items:center;gap:8px;padding:11px 15px;border:1px solid var(--os-line);border-radius:8px;cursor:pointer}.os-radio:has(input:checked){border-color:var(--os-blue);background:var(--os-soft)}.os-radio input{accent-color:var(--os-blue)}.os-save{position:sticky;bottom:0;background:var(--dsw-alias-bg-base,#fff);border-top:1px solid var(--os-line);padding:14px 0 6px;display:flex;gap:16px;justify-content:space-between;align-items:center;margin-top:18px;z-index:1}.os-save p{margin:0;font-size:12px;color:var(--os-muted)}.os-pre{font:12px/1.6 var(--ds-font-family-code,monospace);white-space:pre-wrap;overflow-wrap:anywhere;max-height:290px;overflow:auto;background:var(--os-soft);padding:12px;border-radius:6px}.os-details summary{cursor:pointer;font-size:13px;padding:8px 0}.os-db-list{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0 20px}.os-db-item{display:flex;gap:5px;align-items:center}.os-table-wrap{overflow:auto;max-height:350px;margin:10px 0}.os-table{border-collapse:collapse;width:100%;font-size:12px;text-align:left}.os-table th,.os-table td{padding:8px 10px;border-bottom:1px solid var(--os-line);vertical-align:top}.os-table th{color:var(--os-muted);font-weight:500}.os-table input{accent-color:var(--os-blue)}.os-table-picker{border:1px solid var(--os-line);border-radius:8px;padding:12px 16px;margin:10px 0}.os-table-picker>summary{cursor:pointer;font-weight:500;overflow-wrap:anywhere}.os-loading{padding:36px 0;color:var(--os-muted)}.os-preset small{display:block;color:var(--os-muted);font-size:11px}.os-preset .os-radio{flex:1;min-width:160px;align-items:flex-start}.os-empty{padding:14px 0;color:var(--os-muted);font-size:13px}.os-panel [hidden]{display:none!important}
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

    function Overview({ status, run, busy }) {
      const [path, setPath] = useState('');
      const [pathResult, setPathResult] = useState(null);
      const [modelPath, setModelPath] = useState('');
      const [minutes, setMinutes] = useState('30');
      const index = status?.index || {};
      const progress = index.progress;
      const model = index.semantic?.lifecycle || {};
      const policy = progress?.runtime_policy || index.runtime_policy || {};
      if (!progress) return h('div', { className: 'os-loading' }, status ? '进度尚不可用。请检查后台服务状态或升级 one_search。' : '正在读取后台状态…');
      const content = progress.content || {}, semantic = progress.semantic || {}, discovery = progress.discovery || {};
      const errors = progress.error_summary || {};
      const failed = ['error','budget','encrypted','partial'].reduce((sum, key) => sum + (content.counts?.[key] || 0), 0);
      const hasErrors = failed > 0 || errors.last_error || errors.vector_error || Object.keys(errors.source_errors || {}).length > 0 || (errors.scan_errors?.count || 0) > 0 || (errors.unavailable_roots || []).length > 0 || errors.discovery_error;
      const sources = Object.entries(progress.databases?.sources || {});
      const pathAction = action => run(action, { path: path.trim() }, result => setPathResult({ action, result }));
      return h(React.Fragment, null,
        h('div', { className: 'os-metrics' },
          h(Metric, { title: '已发现文件', value: number(progress.known_unique_files), note: '当前已知的唯一文件' }),
          h(Metric, { title: '正文待处理', value: number(content.pending), note: `${number(content.retry_waiting)} 项等待重试` }),
          h(Metric, { title: '语义片段', value: `${number(semantic.embedded)} / ${number(semantic.eligible)}`, note: '已计算 / 当前可计算' }),
          h(Metric, { title: '内存占用', value: mb(progress.resources?.rss_mb), note: '后台及其工作进程' })),
        h('section', { className: 'os-section' },
          h('div', { className: 'os-section-head' }, h('h2', null, '扫描与建立索引'),
            h('div', { className: 'os-actions' }, policy.user_paused ?
              h(Button, { onClick: () => run('resume'), disabled: busy }, '恢复索引') :
              h(React.Fragment, null, h(Select, { className: 'os-select', 'aria-label': '暂停时长', value: minutes, disabled: busy, options: [['15','15 分钟'],['30','30 分钟'],['60','1 小时'],['0','直到手动恢复']], onChange: setMinutes }),
                h(Button, { onClick: () => run('pause', { seconds: Number(minutes) ? Number(minutes) * 60 : null }), disabled: busy }, '暂停索引')),
              h(Button, { onClick: () => run('scan'), disabled: busy }, '重新扫描'))),
          (policy.reason || progress.overall?.reason && progress.overall.reason !== 'known_tasks_pending') && h('div', { className: 'os-alert' }, label(policy.reason || progress.overall.reason), policy.pause_until && ` · ${date(policy.pause_until)} 自动恢复`),
          h(Stage, { title: '文件发现' }, h('strong', null, discovery.active ? '正在发现文件' : discovery.complete ? '本轮配置范围已扫描' : '等待扫描'),
            h('p', null, `${number(discovery.queued_directories)} 个目录等待扫描。首次扫描总量未知，不显示整机百分比。`)),
          h(Stage, { title: '正文解析' }, h('strong', null, `已解析 ${number(content.counts?.ready || 0)} · 待处理 ${number(content.pending)} · 失败或不完整 ${number(failed)}`),
            h('p', null, `仅文件名 ${number((content.counts?.metadata || 0) + (content.counts?.unsupported || 0))} · 文件变动队列 ${number(content.queued_events)} 项${content.next_retry_at ? ` · 下次重试 ${date(content.next_retry_at)}` : ''}`)),
          h(Stage, { title: '语义索引' }, h('strong', null, !semantic.enabled ? '已关闭' : `模型${label(semantic.model_state)} · ${!model.ready ? '等待模型就绪' : semantic.vector_building ? '正在构建检索索引' : semantic.vector_pending ? '等待更新检索索引' : '当前向量批次已处理'}`),
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
      return h(React.Fragment, null, h('section', { className: 'os-section' }, h('h2', null, '选择资源档位'), h('p', null, '降低后台索引开销会延长首次建立索引的时间，已建立的索引仍可检索。'),
        h('div', { className: 'os-radio-group os-preset' }, [['low','节省资源','768 MB 预算 · 较小批次'],['balanced','均衡','1,024 MB 预算 · 默认档位'],['fast','优先速度','2,048 MB 预算 · 更大批次']].map(([key,title,note]) => h('label', { className: 'os-radio', key }, h('input', { type: 'radio', name: 'one-search-preset', checked: values.preset === key, onChange: () => { update('preset', key); change('preset', key); } }), h('span', null, title, h('small', null, note)))))),
        h('section', { className: 'os-section' }, h('h2', null, '后台运行策略'),
          h(Check, { checked: policy.enabled, onChange: value => change('enabled', value) }, '电脑繁忙或电量偏低时自动退让'),
          h(Check, { checked: policy.idle_only, onChange: value => change('idle_only', value) }, '仅在电脑空闲时建立索引'),
          h(Check, { checked: policy.on_ac_only, onChange: value => change('on_ac_only', value) }, '仅在接通电源时建立索引'),
          h('div', { className: 'os-form-grid', style: { marginTop: 15 } }, h(Field, { title: '空闲等待（秒）' }, h(Input, { type: 'number', min: 15, max: 86400, value: policy.idle_seconds, onChange: value => change('idle_seconds', Number(value)) })),
            h(Field, { title: '繁忙阈值（整机 CPU %）' }, h(Input, { type: 'number', min: 1, max: 100, value: policy.busy_cpu_percent, onChange: value => change('busy_cpu_percent', Number(value)) })))),
        h('section', { className: 'os-section' }, h('h2', null, '当前占用'),
          h('div', { className: 'os-metrics' }, h(Metric, { title: '后台内存', value: mb(resources.rss_mb), note: `当前预算 ${mb(settings.resource?.memory_mb)}` }), h(Metric, { title: '系统可用内存', value: mb(resources.available_mb), note: '整机剩余资源' }), h(Metric, { title: '索引空间', value: mb(resources.disk_mb), note: `当前预算 ${mb(settings.resource?.max_disk_mb)}` }), h(Metric, { title: '磁盘可用空间', value: mb(resources.free_disk_mb), note: '索引所在磁盘' })),
          h('p', { className: 'os-note' }, '内存为后台进程树采样值，磁盘占用定期校准；不包含浏览器与 DSH 模型。档位中的预算不是固定占用或绝对峰值保证。')));
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
      const mounted = useRef(true), lock = useRef(false), poller = useRef(null), draftRef = useRef(null);
      draftRef.current = draft;
      const dirty = Boolean(draft && !same(draft.values, draft.original));
      useEffect(() => {
        mounted.current = true;
        poller.current = createPoller({ document, request: () => request('status'), onValue: value => { setStatus(value); setConnectionError(null); }, onError: (error, delay) => setConnectionError(`${error.message} ${Math.ceil(delay / 1000)} 秒后重试；显示的是上次收到的状态。`) });
        request('settings_get').then(value => { if (mounted.current) { setSettings(value); setDraft(freshDraft(value)); } }).catch(error => { if (mounted.current) setSettingsError(error.message); });
        return () => { mounted.current = false; poller.current?.dispose(); };
      }, [request]);
      useEffect(() => {
        if (!dirty && !dbDirty) return;
        const warn = event => { event.preventDefault(); event.returnValue = ''; };
        window.addEventListener('beforeunload', warn);
        return () => window.removeEventListener('beforeunload', warn);
      }, [dirty, dbDirty]);
      const update = (key, value) => { setDraft(current => ({ ...current, values: { ...current.values, [key]: value } })); setPreview(null); setNotice(null); };
      async function task(title, operation) {
        if (lock.current) return;
        lock.current = true; setBusy(title); setNotice(null);
        try { const message = await operation(); if (mounted.current) { setNotice({ text: typeof message === 'string' ? message : `${title}已完成。`, error: false }); poller.current?.refresh(); } }
        catch (error) { if (mounted.current) { if (error.code === 'revision_conflict') setConflict(true); setNotice({ text: error.message, error: true }); } }
        finally { lock.current = false; if (mounted.current) setBusy(''); }
      }
      const run = (action, params = {}, after) => task(({ pause: '暂停请求', resume: '恢复请求', scan: '重新扫描请求', refresh_path: '路径刷新请求', diagnose_path: '路径诊断', model_start: '模型准备请求', model_import: '模型导入请求', model_cancel: '模型取消请求' })[action] || '操作', async () => { const result = await request(action, params); if (mounted.current) after?.(result); return ['scan','refresh_path','model_start','model_import','model_cancel'].includes(action) ? '请求已接受，后台处理结果会在进度中更新。' : undefined; });
      const reload = () => task('重新加载设置', async () => { const value = await request('settings_get'); if (!mounted.current) return; setSettings(value); setDraft(freshDraft(value)); setSettingsError(null); setConflict(false); setPreview(null); setDbDirty(false); setEditorEpoch(x => x + 1); setDiscard(false); });
      const save = () => task('保存并应用', async () => {
        const current = draftRef.current; const value = await request('settings_save', { revision: current.revision, values: current.values });
        if (!mounted.current) return; setSettings(value); setDraft(freshDraft(value)); setConflict(false); setPreview(null);
        return '已保存并应用。后台将按新设置继续处理，首次扫描与正文更新可能需要一些时间。';
      });
      const previewSettings = () => task('预览设置影响', async () => { const current = draftRef.current; const value = await request('settings_preview', { revision: current.revision, values: current.values }); if (mounted.current) setPreview(value); return value.can_apply ? '预览完成，可保存并应用。' : '预览发现问题，请先修正后再保存。'; });
      const tabs = [['overview','概览'],['scope','检索范围'],['resources','资源'],['databases','数据库']];
      return h('div', { className: 'os-panel' }, h('style', null, css), h('div', { className: 'os-body' },
        h('header', { className: 'os-head' }, h('div', null, h('h1', null, 'one_search'), h('p', { className: 'os-subtitle' }, '本地资料，随时可找。')), h('span', { className: 'os-state', 'data-state': connectionError ? 'needs_attention' : status?.index?.progress?.overall?.state }, h('span', { className: 'os-dot' }), connectionError ? '状态连接中断' : label(status?.index?.progress?.overall?.state || status?.service?.status || '连接中'))),
        h('div', { className: 'os-tabs', role: 'tablist', 'aria-label': 'one_search 设置' }, tabs.map(([key,text], index) => h('button', { key, type: 'button', className: 'os-tab', role: 'tab', id: `os-tab-${key}`, 'aria-controls': `os-content-${key}`, 'aria-selected': tab === key, tabIndex: tab === key ? 0 : -1, onClick: () => setTab(key), onKeyDown: event => { let next; if (event.key === 'ArrowRight') next = (index + 1) % tabs.length; if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length; if (event.key === 'Home') next = 0; if (event.key === 'End') next = tabs.length - 1; if (next !== undefined) { event.preventDefault(); setTab(tabs[next][0]); event.currentTarget.parentElement.children[next].focus(); } } }, text))),
        connectionError && h('div', { className: 'os-alert os-error', role: 'status' }, connectionError, h(Button, { onClick: () => poller.current?.refresh(), style: { marginLeft: 12 } }, '立即重试')),
        h('div', { role: 'status', 'aria-live': 'polite' }, busy ? h('div', { className: 'os-alert' }, `${busy}中…`) : notice && h('div', { className: `os-alert${notice.error ? ' os-error' : ''}` }, notice.text)),
        h('div', { id: 'os-content-overview', role: 'tabpanel', 'aria-labelledby': 'os-tab-overview', hidden: tab !== 'overview' }, h(Overview, { status, run, busy: Boolean(busy) })),
        !draft && tab !== 'overview' && h('div', { className: 'os-loading' }, settingsError || '正在读取设置…', settingsError && h(Button, { onClick: reload, disabled: Boolean(busy) }, '重新读取')),
        draft && h('fieldset', { className: 'os-fields', disabled: Boolean(busy) },
          h('div', { id: 'os-content-scope', role: 'tabpanel', 'aria-labelledby': 'os-tab-scope', hidden: tab !== 'scope' }, h(Scope, { values: draft.values, update })),
          h('div', { id: 'os-content-resources', role: 'tabpanel', 'aria-labelledby': 'os-tab-resources', hidden: tab !== 'resources' }, h(Resources, { values: draft.values, update, status, settings })),
          h('div', { id: 'os-content-databases', role: 'tabpanel', 'aria-labelledby': 'os-tab-databases', hidden: tab !== 'databases' }, h(Databases, { key: editorEpoch, values: draft.values, update, request, task, busy: Boolean(busy), onDirty: setDbDirty }))),
        preview && h('div', { className: 'os-alert' }, h('strong', null, preview.can_apply ? '设置可应用' : '请修正预检问题'),
          h('p', null, preview.scope?.available === false ? '当前无法估算范围影响。' : `已知文件撤销：${number(preview.scope?.known_files_revoked)}；已知正文缓存撤销：${number(preview.scope?.known_content_caches_revoked)}。`),
          preview.databases_changed && h('p', null, `数据库只读预检：${preview.preflight?.ok ? '通过' : '未通过'}`), h(Details, { title: '查看预览详情', value: preview })),
        conflict && h('div', { className: 'os-alert os-error' }, '设置已在其他位置修改。你的编辑仍保留；请复制需要保留的内容，再重新加载最新设置。'),
        discard && h('div', { className: 'os-alert' }, h('p', null, '重新加载会放弃本页未保存的设置与数据库编辑。'), h('div', { className: 'os-actions' }, h(Button, { onClick: reload, disabled: Boolean(busy) }, '放弃并重新加载'), h(Button, { onClick: () => setDiscard(false), disabled: Boolean(busy) }, '继续编辑'))),
        draft && (tab !== 'overview' || dirty || dbDirty) && h('footer', { className: 'os-save' }, h('div', null, h('strong', { style: { fontSize: 13 } }, dbDirty ? '数据库尚在编辑' : dirty ? '有未保存的修改' : '设置已同步'), h('p', null, dbDirty ? '先完成或放弃数据库编辑。' : '保存后应用到后台服务；索引会逐步更新。')),
          h('div', { className: 'os-actions' }, h(Button, { onClick: () => dirty || dbDirty ? setDiscard(true) : reload(), disabled: Boolean(busy) }, '重新加载'), h(Button, { onClick: previewSettings, disabled: Boolean(busy) || !dirty || dbDirty || conflict }, '预览影响'), h(Button, { primary: true, onClick: save, disabled: Boolean(busy) || !dirty || dbDirty || conflict || preview?.can_apply === false }, '保存并应用'))),
        h('p', { className: 'os-note', style: { marginTop: 20 } }, `状态更新：${date(status?.index?.progress?.sampled_at)} · 页面可见时每 2 秒刷新 · 关闭页面不停止后台`)));
    }

    function SearchIcon({ size = 18 }) { return h('svg', { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.7, strokeLinecap: 'round', 'aria-hidden': true }, h('circle', { cx: 10, cy: 10, r: 6 }), h('path', { d: 'm14.5 14.5 5 5M7.5 10h5M10 7.5v5' })); }
    function apply(ctx) {
      const request = async (action, params = {}) => unwrap(await ctx.connection.rpc.call('/one-search', 'request', { action, params }));
      ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({ name: 'sidebar.panellist', id: 'one-search', order: 70, label: 'one_search' }, SearchIcon));
      ctx.slots.inject('main', () => ctx.slots.register({ name: 'main', key: 'one-search' }, () => h(Panel, { request })));
    }
    return { name: 'one-search-web', inject: ['slots', 'connection'], apply,
      __testing: { unwrap, createPoller, freshDraft, packSelections, selectionFor, connectionSource, lines, Panel, Scope, Overview, Databases } };
  },
});
