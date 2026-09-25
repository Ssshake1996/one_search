"""Lightweight evidence and maintenance panels over the shared daemon contracts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import queue
import threading
from types import SimpleNamespace


def add_product_panels(notebook, config_path):
    import tkinter as tk
    from tkinter import ttk, filedialog
    from .config import load_config
    from .service import rpc
    from . import maintenance

    config_path = Path(config_path)
    def page(title):
        outer = ttk.Frame(notebook)
        canvas = tk.Canvas(outer, highlightthickness=0, background='#f2f5f4')
        scrollbar = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        body = ttk.Frame(canvas, padding=10)
        window_id = canvas.create_window((0, 0), window=body, anchor='nw')
        body.bind('<Configure>', lambda _: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window_id, width=event.width))
        notebook.add(outer, text=title)
        return outer, body, canvas

    search_page, search_tab, search_canvas = page('查找与核实')
    maintenance_page, maintenance_tab, maintenance_canvas = page('维护与退出')
    status = tk.StringVar(value='查找名称或内容。选中结果读取上下文；路径操作使用下方明确显示的目标。')
    jobs = queue.Queue()
    controller = SimpleNamespace(busy=False, closed=False, mutation=False, cancelled=False, after_id=None)
    notebook.one_search_product_controller = controller
    notebook.one_search_config_changed = False
    pending_read = None
    buttons, hit_buttons, path_buttons = [], [], []
    hits, citations, known_backups = {}, {}, {}
    restore_bundle = None
    restore_base_digest = None
    restore_label = tk.StringVar(value='尚未预览恢复文件。')
    search_generation = 0

    def state_buttons():
        for button in buttons:
            button.configure(state='disabled' if controller.busy else 'normal')
        if not controller.busy:
            for button in hit_buttons:
                button.configure(state='normal' if selected() else 'disabled')
            for button in path_buttons:
                button.configure(state='normal' if target_path.get().strip() else 'disabled')
            apply_button.configure(state='normal' if restore_bundle is not None else 'disabled')
        cancel_button.configure(state='normal' if controller.busy and not controller.mutation and not controller.cancelled else 'disabled')

    def run(operation, callback, *, mutation=False, failure=None):
        if controller.closed:
            return False
        if controller.busy or getattr(notebook, 'one_search_main_busy', lambda: False)():
            status.set('另一项操作正在执行，完成后再试。')
            return False
        controller.busy, controller.mutation, controller.cancelled = True, mutation, False
        status.set('处理中…' if not mutation else '正在执行本次操作；完成前请保持窗口打开。')
        state_buttons()
        def work():
            try:
                jobs.put((callback, operation(), None, failure))
            except Exception as error:
                jobs.put((callback, None, str(error), failure))
        # Normal window close is guarded by request_close. Destruction by the
        # application is also tolerated without calling Tk from this thread.
        threading.Thread(target=work, daemon=True).start()
        return True

    def request_close():
        if controller.busy:
            status.set('操作执行中，完成后可关闭窗口。')
            return False
        return True

    def cancel():
        nonlocal pending_read
        if controller.busy and not controller.mutation:
            controller.cancelled = True
            pending_read = None
            status.set('已取消结果展示；当前只读请求结束后即可继续或关闭窗口。')
            state_buttons()

    def poll():
        nonlocal pending_read
        if controller.closed:
            return
        try:
            callback, result, error, failure = jobs.get_nowait()
        except queue.Empty:
            pass
        else:
            cancelled = controller.cancelled
            controller.busy, controller.mutation, controller.cancelled = False, False, False
            if cancelled:
                status.set('已取消。')
            elif error:
                status.set('操作未完成：' + error)
                if failure:
                    failure(error)
            else:
                status.set('操作完成。请核对显示的来源、状态与范围。')
                callback(result)
            state_buttons()
            if pending_read and not controller.busy:
                wanted, pending_read = pending_read, None
                if selected() and selected()['id'] == wanted:
                    read()
        if not controller.closed:
            controller.after_id = notebook.after(75, poll)

    def destroyed(event):
        if event.widget != notebook:
            return
        controller.closed = True
        if controller.after_id:
            try:
                notebook.after_cancel(controller.after_id)
            except tk.TclError:
                pass
            controller.after_id = None
    notebook.bind('<Destroy>', destroyed, add='+')
    controller.request_close, controller.cancel = request_close, cancel

    def call(method, **params):
        return rpc(load_config(config_path), method, params)

    def label(parent, text=None, variable=None):
        widget = ttk.Label(parent, text=text, textvariable=variable, wraplength=600)
        widget.pack(fill='x', pady=4)
        parent.bind('<Configure>', lambda event: widget.configure(wraplength=max(220, event.width - 20)), add='+')
        return widget

    def action_grid(parent, entries, columns=3):
        frame = ttk.Frame(parent)
        frame.pack(fill='x', pady=4)
        output = []
        for i, (title, command) in enumerate(entries):
            button = ttk.Button(frame, text=title, command=command)
            button.grid(row=i // columns, column=i % columns, sticky='ew', padx=(0, 5), pady=3)
            frame.columnconfigure(i % columns, weight=1)
            buttons.append(button)
            output.append(button)
        return output

    text, mode = tk.StringVar(), tk.StringVar(value='hybrid')
    top = ttk.Frame(search_tab)
    top.pack(fill='x')
    query_entry = ttk.Entry(top, textvariable=text)
    query_entry.pack(side='left', fill='x', expand=True)
    ttk.Combobox(top, textvariable=mode, values=['hybrid', 'files', 'keyword', 'semantic'], state='readonly', width=10).pack(side='left', padx=6)
    options = ttk.LabelFrame(search_tab, text='筛选（可留空；日期使用 YYYY-MM-DD）', padding=6)
    options.pack(fill='x', pady=7)
    fields = {}
    for i, (key, title) in enumerate([('directory', '目录'), ('extensions', '扩展名，逗号分隔'), ('modified_after', '修改日期起（含）'), ('modified_before', '修改日期止（不含）')]):
        row, col = divmod(i, 2)
        ttk.Label(options, text=title).grid(row=row, column=col * 2, sticky='w')
        fields[key] = tk.StringVar()
        ttk.Entry(options, textvariable=fields[key], width=14).grid(row=row, column=col * 2 + 1, sticky='ew', padx=6, pady=3)
        options.columnconfigure(col * 2 + 1, weight=1)
    result_frame = ttk.Frame(search_tab)
    result_frame.pack(fill='both', expand=True)
    results = ttk.Treeview(result_frame, columns=('name', 'state', 'path'), show='headings', height=4, selectmode='browse')
    for key, title, width in [('name', '资料', 130), ('state', '有效性', 130), ('path', '来源路径', 350)]:
        results.heading(key, text=title)
        results.column(key, width=width, minwidth=70)
    result_scroll = ttk.Scrollbar(result_frame, orient='vertical', command=results.yview)
    results.configure(yscrollcommand=result_scroll.set)
    result_scroll.pack(side='right', fill='y')
    results.pack(fill='both', expand=True)
    preview = tk.Text(search_tab, height=8, wrap='word', state='disabled')

    def show(value):
        preview.configure(state='normal')
        preview.delete('1.0', 'end')
        if isinstance(value, dict) and 'chunks' in value:
            document = value.get('document') or {}
            rendered = '来源：' + str(document.get('path', '')) + '\n'
            rendered += '状态：' + ('索引快照已过期，请刷新后核实。' if document.get('stale') else '当前可核对的索引快照。') + '\n'
            rendered += '\n\n'.join(json.dumps(chunk.get('locator', {}), ensure_ascii=False) + '\n' + chunk.get('text', '') for chunk in value['chunks'])
            rendered += '\n\n来源引用：' + json.dumps(value.get('citation') or document, ensure_ascii=False)
        else:
            rendered = json.dumps(value, ensure_ascii=False, indent=2)
        preview.insert('1.0', rendered)
        preview.configure(state='disabled')

    def found(value):
        nonlocal pending_read
        pending_read = None
        hits.clear()
        citations.clear()
        results.delete(*results.get_children())
        for hit in value['results']:
            hits[hit['id']] = hit
            results.insert('', 'end', iid=hit['id'], values=(hit['name'], '过期 · 可刷新' if hit['stale'] else hit['status'], hit['path']))
        show({key: item for key, item in value.items() if key != 'results'})
        status.set(f"找到 {len(hits)} 项。选中结果核实上下文。" if hits else '当前条件没有命中；这不代表整台机器没有相关资料，可使用路径诊断。')

    def search():
        nonlocal search_generation
        args = {key: value.get().strip() for key, value in fields.items() if value.get().strip()}
        if 'extensions' in args:
            args['extensions'] = [part.strip() for part in args['extensions'].split(',') if part.strip()]
        query, selected_mode = text.get().strip(), mode.get()
        if not query:
            status.set('请输入名称或内容。')
            return
        if run(lambda: call('search', query=query, mode=selected_mode, **args), found):
            search_generation += 1

    def selected():
        values = results.selection()
        return hits.get(values[0]) if values else None

    def read():
        nonlocal pending_read
        hit = selected()
        if not hit:
            return
        identity, generation = hit['id'], search_generation
        if controller.busy:
            pending_read = identity
            return
        def read_done(value):
            if generation != search_generation or not selected() or selected()['id'] != identity:
                return
            if value.get('citation'):
                citations[identity] = value['citation']
            show(value)
        run(lambda: call('read_context', id=identity), read_done)

    target_path = tk.StringVar()
    path_automatic, changing_path = True, False
    def changed_path(*_):
        nonlocal path_automatic
        if not changing_path:
            path_automatic = False
        state_buttons()
    def use_selected_path():
        nonlocal path_automatic, changing_path
        hit = selected()
        if hit and hit.get('source_id') == 'files':
            changing_path = True
            target_path.set(hit['path'])
            changing_path, path_automatic = False, True
    def selection_changed(_event=None):
        if path_automatic:
            use_selected_path()
        state_buttons()
        if selected():
            show({'path': selected()['path'], 'state': '正在读取所选资料上下文…'})
            read()

    def copy_citation():
        hit = selected()
        if hit:
            citation = citations.get(hit['id']) or hit.get('citation')
            if not citation:
                status.set('此结果尚无可复制的引用，请先读取上下文。')
                return
            notebook.clipboard_clear()
            notebook.clipboard_append(json.dumps(citation, ensure_ascii=False))
            status.set('已复制所选资料引用；其中保留索引时间与过期标记。')

    def open_hit(folder):
        hit = selected()
        if hit:
            identity = hit['id']
            run(lambda: call('open_source', id=identity, folder=folder), show, mutation=True)

    search_button = ttk.Button(top, text='查找', command=search, style='Primary.TButton')
    search_button.pack(side='left')
    buttons.append(search_button)
    query_entry.bind('<Return>', lambda _: search())
    results.bind('<<TreeviewSelect>>', selection_changed)
    hit_buttons.extend(action_grid(search_tab, [('读取上下文', read), ('复制引用', copy_citation), ('使用所选路径', use_selected_path), ('打开文件', lambda: open_hit(False)), ('所在目录', lambda: open_hit(True))]))
    path_frame = ttk.LabelFrame(search_tab, text='路径诊断与更新 · 仅作用于此处显示的路径', padding=6)
    path_frame.pack(fill='x', pady=5)
    ttk.Entry(path_frame, textvariable=target_path).pack(fill='x')
    def target(action):
        path = target_path.get().strip()
        if not path or not Path(path).is_absolute():
            status.set('请输入完整的本地文件或目录路径。')
            return
        def complete(value):
            if action == 'refresh_path' and value.get('accepted'):
                # Refresh may replace chunk IDs. Never let old selected IDs or
                # citations appear current after the explicit update.
                for identity, hit in list(hits.items()):
                    if Path(hit['path']) == Path(path):
                        hits.pop(identity)
                        citations.pop(identity, None)
                        results.item(identity, values=(hit['name'], '已请求刷新 · 重新查找', hit['path']))
            show(value)
            status.set('操作目标：' + path + ('；重新查找可取得更新后的引用。' if action == 'refresh_path' and value.get('accepted') else ''))
        run(lambda: call(action, path=path), complete, mutation=action != 'diagnose_path')
    path_buttons.extend(action_grid(path_frame, [('诊断此路径', lambda: target('diagnose_path')), ('刷新此路径', lambda: target('refresh_path')), ('优先处理此路径', lambda: target('prioritize_path'))]))
    preview.pack(fill='both', expand=True)
    label(search_tab, variable=status)
    cancel_button = ttk.Button(search_tab, text='取消本次只读结果展示', command=cancel, state='disabled')
    cancel_button.pack(anchor='w', pady=3)

    ttk.Label(maintenance_tab, text='本地实例维护', font=('Microsoft YaHei UI', 15, 'bold')).pack(anchor='w')
    label(maintenance_tab, '索引、范围和后台由已登记连接共享。维护前先查看影响；源资料不会被迁移或删除。')
    maint_output = tk.Text(maintenance_tab, height=15, wrap='word', state='disabled')
    def show_maintenance(value):
        maint_output.configure(state='normal')
        maint_output.delete('1.0', 'end')
        maint_output.insert('1.0', json.dumps(value, ensure_ascii=False, indent=2))
        maint_output.configure(state='disabled')
    guessed_install = config_path.parent.parent / 'app'
    install = tk.StringVar(value=str(guessed_install) if (guessed_install / 'install-manifest.json').is_file() else '')
    row = ttk.Frame(maintenance_tab)
    row.pack(fill='x', pady=4)
    ttk.Label(row, text='安装目录（源码运行可留空）').pack(side='left')
    ttk.Entry(row, textvariable=install).pack(side='left', fill='x', expand=True, padx=6)
    def overview():
        directory = install.get().strip() or None
        def completed(value):
            known_backups.clear()
            known_backups.update({entry['id']: entry for entry in value['space'].get('retained_backups', [])})
            show_maintenance(value)
        run(lambda: {'instance': maintenance.compatibility_info(load_config(config_path)),
                     'space': maintenance.space_report(load_config(config_path), install_dir=directory)}, completed)
    def export():
        path = filedialog.asksaveasfilename(parent=notebook.winfo_toplevel(), defaultextension='.json', initialfile='one-search-settings.json')
        if path:
            run(lambda: maintenance.export_config(load_config(config_path), path), show_maintenance, mutation=True)
    def restore():
        path = filedialog.askopenfilename(parent=notebook.winfo_toplevel(), filetypes=[('设置导出', '*.json')])
        if path:
            def preview_restore():
                with Path(path).open('rb') as stream:
                    raw = stream.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError('设置导出超过允许大小。')
                bundle = json.loads(raw.decode('utf-8-sig'))
                digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
                result = maintenance.restore_config(load_config(config_path), bundle)
                result['restore_file'] = path
                return result, bundle, digest
            def preview_done(value):
                nonlocal restore_bundle, restore_base_digest
                result, bundle, digest = value
                restore_bundle = bundle if not result.get('issues') else None
                restore_base_digest = digest
                restore_label.set('已预览：' + path + ('；需先解决显示的路径问题。' if result.get('issues') else '；应用使用本次预览快照。'))
                show_maintenance(result)
            run(preview_restore, preview_done)
    def changed_configuration(value):
        nonlocal restore_bundle
        restore_bundle = None
        notebook.one_search_config_changed = True
        notebook.event_generate('<<OneSearchConfigChanged>>', when='tail')
        show_maintenance(value)
        status.set('配置已改变；请关闭并重新打开设置窗口，再修改其他设置。')
    def apply_restore():
        if restore_bundle is None:
            status.set('请先预览一个可恢复的设置文件。')
            return
        bundle, base_digest = json.loads(json.dumps(restore_bundle)), restore_base_digest
        def apply():
            if hashlib.sha256(config_path.read_bytes()).hexdigest() != base_digest:
                raise ValueError('当前配置已改变，请重新预览恢复内容。')
            return maintenance.restore_config(load_config(config_path), bundle, apply=True)
        run(apply, changed_configuration, mutation=True)
    def relocate():
        destination = filedialog.askdirectory(parent=notebook.winfo_toplevel(), title='选择空目录保存索引（先停止服务）')
        if destination:
            run(lambda: maintenance.relocate_index(load_config(config_path), destination), changed_configuration, mutation=True)
    toolbar = action_grid(maintenance_tab, [('实例与空间', overview), ('导出设置', export), ('预览恢复', restore), ('应用已预览恢复', apply_restore), ('迁移索引', relocate)])
    apply_button = toolbar[3]
    label(maintenance_tab, variable=restore_label)
    def installed_directory():
        directory = install.get().strip()
        if not directory:
            status.set('请先填写已安装实例的程序目录。')
        return directory
    def autostart(enabled):
        directory = installed_directory()
        if directory:
            run(lambda: maintenance.set_autostart(load_config(config_path), directory, enabled), show_maintenance, mutation=True)
    def exit_service():
        from .service import stop_service
        directory = installed_directory()
        if not directory:
            return
        def stop():
            config = load_config(config_path)
            disabled = maintenance.set_autostart(config, directory, False)
            return {'autostart': disabled, 'service': stop_service(config), 'clients': maintenance.registered_clients(config)}
        run(stop, show_maintenance, mutation=True)
    def lifecycle():
        directory = installed_directory()
        if directory:
            run(lambda: maintenance.lifecycle_actions(load_config(config_path), directory), show_maintenance)
    action_grid(maintenance_tab, [('启用自启动', lambda: autostart(True)), ('关闭自启动', lambda: autostart(False)),
        ('停止并关闭自启动', exit_service), ('更新 / 卸载命令', lifecycle)])
    clean = ttk.Frame(maintenance_tab)
    clean.pack(fill='x', pady=5)
    backup_id = tk.StringVar()
    ttk.Label(clean, text='空间报告中的备份 ID').pack(side='left')
    ttk.Entry(clean, textvariable=backup_id, width=24).pack(side='left', fill='x', expand=True, padx=5)
    def cleanup():
        value, directory = backup_id.get().strip(), install.get().strip() or None
        if value not in known_backups or not known_backups[value].get('cleanup_eligible'):
            status.set('先查看实例与空间，再填写报告中允许清理的备份 ID。')
            return
        run(lambda: maintenance.cleanup_backup(load_config(config_path), value, install_dir=directory), show_maintenance, mutation=True)
    cleanup_button = ttk.Button(clean, text='清除此备份', command=cleanup)
    cleanup_button.pack(side='left')
    buttons.append(cleanup_button)
    maint_output.pack(fill='both', expand=True, pady=6)
    label(maintenance_tab, variable=status)
    # The controller supports coordinated close and integration tests without
    # exposing Tk reads to worker callbacks.
    controller.__dict__.update(text=text, mode=mode, fields=fields, target_path=target_path, results=results,
        preview=preview, status=status, install=install, backup_id=backup_id, maint_output=maint_output,
        search=search, read=read, copy_citation=copy_citation, target=target, use_selected_path=use_selected_path,
        overview=overview, export=export, restore=restore, apply_restore=apply_restore, relocate=relocate,
        autostart=autostart, exit_service=exit_service, lifecycle=lifecycle, cleanup=cleanup,
        search_canvas=search_canvas, maintenance_canvas=maintenance_canvas, show=show, run=run)
    target_path.trace_add('write', changed_path)
    install.trace_add('write', lambda *_: known_backups.clear())
    state_buttons()
    poll()
    return search_page, maintenance_page
