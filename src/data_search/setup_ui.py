"""Small local settings window; no web listener or additional UI dependency."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading

from .config import atomic_json, defaults, load_config


def default_config_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share"))
    return base / "data-search/data/config.json"


def settings_config(current: dict, values: dict) -> dict:
    """Merge editable fields while retaining budgets, TLS options and advanced settings."""
    candidate = deepcopy(current)
    candidate.pop("config_path", None)
    candidate["scope"] = values["scope"]
    candidate["roots"] = values["roots"] if values["scope"] == "directories" else []
    if candidate["scope"] == "directories" and not candidate["roots"]:
        raise ValueError("目录模式至少需要一个目录 / Choose at least one directory")
    candidate["databases"] = values["databases"]
    if not isinstance(candidate["databases"], list):
        raise ValueError("数据库配置必须是 JSON 数组 / Database settings must be a JSON array")
    from .databases import DatabaseSource
    for source in candidate["databases"]:
        DatabaseSource(source)
    candidate["semantic"]["enabled"] = bool(values["semantic_enabled"])
    candidate.setdefault("indexing", {}).update(values["indexing"])
    all_roots = list(candidate["roots"])
    for tier in ("content", "semantic"):
        if candidate["indexing"][f"{tier}_scope"] == "directories":
            roots = candidate["indexing"][f"{tier}_roots"]
            if not roots:
                raise ValueError(f"{tier}: 目录模式需要目录 / Directory scope requires a directory")
            all_roots.extend(roots)
    for root in all_roots:
        if not Path(root).expanduser().is_dir():
            raise ValueError(f"目录不存在 / Directory does not exist: {root}")
    # Validate with the same loader used by the service, before stopping or saving.
    with tempfile.TemporaryDirectory(prefix="data-search-settings-") as temp:
        path = Path(temp) / "config.json"
        atomic_json(path, candidate)
        load_config(path)
    return candidate


def main(argv=None):
    from .runtime import configure_native_threads
    configure_native_threads()
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    parser = argparse.ArgumentParser(description="one_search local settings")
    parser.add_argument("--config", type=Path, default=default_config_path())
    args = parser.parse_args(argv)
    config_path = args.config.expanduser().resolve()
    if config_path.exists():
        current = json.loads(config_path.read_text(encoding="utf-8-sig"))
    else:
        current = defaults(str(config_path.parent))
        program_dir = Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False) and program_dir.name == "runtime":
            program_dir = program_dir.parent
        current.setdefault("exclude_paths", []).append(str(program_dir))
    # Older saved configurations preserve their previously selected directory scope.
    current.setdefault("scope", "directories")
    baseline = defaults(current["data_dir"], [])
    for key in ("indexing", "semantic"):
        current[key] = {**baseline[key], **current.get(key, {})}
    window = tk.Tk()
    window.title("one_search · 本地搜索设置")
    window.geometry("900x700")
    window.minsize(740, 560)
    style = ttk.Style(window)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    style.configure(".", font=("Microsoft YaHei UI", 9), background="#f2f5f4", foreground="#193b38")
    style.configure("TButton", padding=(10, 6))
    style.configure("Primary.TButton", background="#17675d", foreground="white")
    style.map("Primary.TButton", background=[("active", "#0e5149")])
    container = ttk.Frame(window, padding=12)
    container.pack(fill="both", expand=True)
    ttk.Label(container, text="one_search", font=("Bahnschrift", 23, "bold")).pack(anchor="w")
    ttk.Label(container, text=f"配置：{config_path}", wraplength=840).pack(anchor="w", pady=(0, 8))
    tabs = ttk.Notebook(container)
    tabs.pack(fill="both", expand=True)
    scope_page = ttk.Frame(tabs)
    scope_canvas = tk.Canvas(scope_page, background="#f2f5f4", highlightthickness=0)
    scope_scrollbar = ttk.Scrollbar(scope_page, orient="vertical", command=scope_canvas.yview)
    scope_canvas.configure(yscrollcommand=scope_scrollbar.set)
    scope_scrollbar.pack(side="right", fill="y")
    scope_canvas.pack(side="left", fill="both", expand=True)
    scope_tab = ttk.Frame(scope_canvas, padding=10)
    scope_window = scope_canvas.create_window((0, 0), window=scope_tab, anchor="nw")
    scope_tab.bind("<Configure>", lambda _: scope_canvas.configure(scrollregion=scope_canvas.bbox("all")))
    scope_canvas.bind("<Configure>", lambda event: scope_canvas.itemconfigure(scope_window, width=event.width))
    database_tab, status_tab = (ttk.Frame(tabs, padding=10) for _ in range(2))
    tabs.add(scope_page, text="检索范围与资源")
    tabs.add(database_tab, text="数据库")
    tabs.add(status_tab, text="状态与错误")
    scope = tk.StringVar(value=current["scope"])
    ttk.Radiobutton(scope_tab, text="整个电脑 / 服务器（当前账号可访问的本地文件系统）", variable=scope, value="machine").pack(anchor="w")
    ttk.Radiobutton(scope_tab, text="仅下列目录", variable=scope, value="directories").pack(anchor="w")

    def folder_list(parent, initial, height=3):
        box = ttk.Frame(parent)
        box.pack(fill="x", pady=3)
        text = tk.Text(box, height=height, wrap="none")
        text.pack(side="left", fill="x", expand=True)
        text.insert("1.0", "\n".join(initial))
        def add():
            directory = filedialog.askdirectory(parent=window)
            if directory:
                existing = text.get("1.0", "end").strip()
                text.delete("1.0", "end")
                text.insert("1.0", (existing + "\n" if existing else "") + directory)
        ttk.Button(box, text="添加目录", command=add).pack(side="right", padx=(8, 0))
        return text

    roots = folder_list(scope_tab, current.get("roots", []))
    ttk.Label(scope_tab, text="文件名索引覆盖上述范围。正文与语义可进一步缩小范围以节省资源。", wraplength=780).pack(anchor="w", pady=5)
    tier_widgets = {}
    for tier, label in (("content", "正文"), ("semantic", "语义")):
        frame = ttk.LabelFrame(scope_tab, text=label, padding=6)
        frame.pack(fill="x", pady=4)
        row = ttk.Frame(frame)
        row.pack(fill="x")
        mode = tk.StringVar(value=current["indexing"][f"{tier}_scope"])
        ttk.Combobox(row, textvariable=mode, values=("all", "directories", "none"), state="readonly", width=16).pack(side="left")
        ttk.Label(row, text="all 全部支持文件 · directories 指定目录 · none 关闭").pack(side="left", padx=8)
        tier_roots = folder_list(frame, current["indexing"][f"{tier}_roots"], height=2)
        extensions = tk.StringVar(value=", ".join(current["indexing"][f"{tier}_extensions"]))
        ttk.Label(frame, text="扩展名（逗号分隔，留空为全部支持类型）：").pack(anchor="w")
        ttk.Entry(frame, textvariable=extensions).pack(fill="x")
        tier_widgets[tier] = (mode, tier_roots, extensions)
    semantic_enabled = tk.BooleanVar(value=current["semantic"]["enabled"])
    ttk.Checkbutton(scope_tab, text="启用本地语义模型（首次使用需下载模型）", variable=semantic_enabled).pack(anchor="w", pady=5)
    ttk.Label(scope_tab, text="内存和磁盘预算沿用配置文件中的 resource 设置；达到预算会暂停或限制建库。", wraplength=780).pack(anchor="w")

    ttk.Label(database_tab, text="只读账号；密码填写环境变量名，不填写密码本身。环境变量需在服务启动前可用。", wraplength=780).pack(anchor="w")
    db_text = tk.Text(database_tab, height=24, wrap="none", undo=True, font=("Consolas", 10))
    db_text.pack(fill="both", expand=True, pady=8)
    db_text.insert("1.0", json.dumps(current.get("databases", []), ensure_ascii=False, indent=2))

    def add_database():
        dialog = tk.Toplevel(window)
        dialog.title("添加数据库")
        dialog.transient(window)
        form = ttk.Frame(dialog, padding=12)
        form.pack(fill="both", expand=True)
        fields = {}
        specifications = [("kind", "类型", "sqlite"), ("id", "唯一名称", "local_data"),
            ("path", "SQLite 文件路径", ""), ("host", "服务器地址", "127.0.0.1"),
            ("port", "端口（留空用默认值）", ""), ("database", "库名", ""),
            ("user", "只读账号", ""), ("password_env", "密码环境变量名", ""),
            ("table", "表名（PostgreSQL 用 schema.table）", ""),
            ("columns", "允许字段（逗号分隔）", ""), ("id_column", "索引主键（留空仅实时查询）", ""),
            ("text_columns", "正文索引字段（逗号分隔）", ""), ("updated_column", "更新时间字段（可选）", "")]
        for row, (key, label, value) in enumerate(specifications):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=3)
            variable = tk.StringVar(value=value)
            fields[key] = variable
            if key == "kind":
                widget = ttk.Combobox(form, textvariable=variable, values=("sqlite", "mysql", "postgres"), state="readonly", width=44)
            else:
                widget = ttk.Entry(form, textvariable=variable, width=47)
            widget.grid(row=row, column=1, sticky="ew", padx=6)
            if key == "path":
                ttk.Button(form, text="浏览", command=lambda: fields["path"].set(filedialog.askopenfilename(parent=dialog))).grid(row=row, column=2)
        def accept():
            try:
                values = {key: variable.get().strip() for key, variable in fields.items()}
                table = values["table"]
                columns = [item.strip() for item in values["columns"].split(",") if item.strip()]
                if not table or not columns:
                    raise ValueError("请填写允许访问的表名和字段")
                source = {"id": values["id"], "kind": values["kind"], "allowed_tables": [table], "allowed_columns": {table: columns}}
                if values["kind"] == "sqlite":
                    if not Path(values["path"]).is_file():
                        raise ValueError("请选择已存在的 SQLite 文件")
                    source["path"] = str(Path(values["path"]).resolve())
                else:
                    if not values["database"] or not values["user"]:
                        raise ValueError("请填写库名和只读账号")
                    for key in ("host", "database", "user", "password_env"):
                        source[key] = values[key]
                    source["port"] = int(values["port"] or (3306 if values["kind"] == "mysql" else 5432))
                if values["id_column"]:
                    text_columns = [item.strip() for item in values["text_columns"].split(",") if item.strip()]
                    if not text_columns or not set([values["id_column"], *text_columns]).issubset(columns):
                        raise ValueError("索引主键和正文索引字段必须包含在允许字段中")
                    index = {"table": table, "id_column": values["id_column"], "text_columns": text_columns}
                    if values["updated_column"]:
                        if values["updated_column"] not in columns:
                            raise ValueError("更新时间字段必须包含在允许字段中")
                        index["updated_column"] = values["updated_column"]
                    source["index"] = [index]
                from .databases import DatabaseSource
                DatabaseSource(source)
                databases = json.loads(db_text.get("1.0", "end"))
                if any(item["id"] == source["id"] for item in databases):
                    raise ValueError("数据库名称已存在，请更换唯一名称")
                databases.append(source)
                db_text.delete("1.0", "end")
                db_text.insert("1.0", json.dumps(databases, ensure_ascii=False, indent=2))
                dialog.destroy()
            except (ValueError, KeyError, TypeError) as error:
                messagebox.showerror("配置错误", str(error), parent=dialog)
        ttk.Button(form, text="加入配置", command=accept).grid(row=len(specifications), column=1, sticky="e", pady=10)
    ttk.Button(database_tab, text="添加数据库…", command=add_database).pack(anchor="w")
    ttk.Label(database_tab, text="高级 TLS、多个表和字段配置可在上方 JSON 中编辑。保存不会测试远程数据库连接。", wraplength=780).pack(anchor="w", pady=5)
    status_summary = tk.StringVar(value="刷新状态，查看后台服务与索引进度。")
    ttk.Label(status_tab, textvariable=status_summary, font=("Microsoft YaHei UI", 12, "bold"), wraplength=780).pack(anchor="w", pady=(0, 12))
    ttk.Label(status_tab, text="详细状态 / 错误信息").pack(anchor="w", pady=(0, 5))
    status_text = tk.Text(status_tab, wrap="word", state="disabled", font=("Consolas", 10))
    status_text.pack(fill="both", expand=True)
    messages = queue.Queue()
    busy = False
    controls = ttk.Frame(container)
    controls.pack(fill="x", pady=(10, 0))
    activity = tk.StringVar(value="就绪；保存并启动后开始检索范围内的后台索引。")
    ttk.Label(container, textvariable=activity, wraplength=840).pack(anchor="w")

    def run_task(operation):
        nonlocal busy
        if busy:
            return
        busy = True
        activity.set("处理中…")
        def task():
            try:
                messages.put((True, operation()))
            except Exception as error:
                messages.put((False, f"{type(error).__name__}: {error}"))
        threading.Thread(target=task, daemon=True).start()

    def configuration():
        return load_config(config_path)

    def show_status():
        from .service import rpc, service_status
        config = configuration()
        return {"service": service_status(config), "index": rpc(config, "index_status")}

    def save_start():
        nonlocal current
        if busy:
            return
        try:
            indexing = {}
            for tier, (mode, paths, extensions) in tier_widgets.items():
                indexing[f"{tier}_scope"] = mode.get()
                indexing[f"{tier}_roots"] = [p.strip() for p in paths.get("1.0", "end").splitlines() if p.strip()]
                indexing[f"{tier}_extensions"] = [p.strip() for p in extensions.get().split(",") if p.strip()]
            candidate = settings_config(current, {"scope": scope.get(), "roots": [p.strip() for p in roots.get("1.0", "end").splitlines() if p.strip()],
                "databases": json.loads(db_text.get("1.0", "end")), "semantic_enabled": semantic_enabled.get(), "indexing": indexing})
        except (ValueError, KeyError, TypeError, OSError) as error:
            messagebox.showerror("配置错误", str(error), parent=window)
            return
        def operation():
            nonlocal current
            from .service import start_service, stop_service
            if config_path.exists():
                stop_service(configuration())
            atomic_json(config_path, candidate)
            current = candidate
            return start_service(configuration())
        run_task(operation)

    def control(action):
        def operation():
            from .service import rpc, start_service, stop_service
            config = configuration()
            if action == "start": return start_service(config)
            if action == "stop": return stop_service(config)
            return rpc(config, action)
        run_task(operation)

    def download():
        def operation():
            from .model import download_model
            return download_model(current["semantic"]["model_dir"])
        run_task(operation)

    for label, command in (("保存并启动", save_start), ("刷新状态", lambda: run_task(show_status)),
        ("暂停索引", lambda: control("pause")), ("恢复索引", lambda: control("resume")),
        ("停止服务", lambda: control("stop")), ("下载模型", download)):
        ttk.Button(controls, text=label, command=command, style="Primary.TButton" if label == "保存并启动" else "TButton").pack(side="left", padx=(0, 5))

    def poll():
        nonlocal busy
        try:
            success, payload = messages.get_nowait()
        except queue.Empty:
            pass
        else:
            busy = False
            activity.set("完成。" if success else "操作失败，详见状态与错误。")
            if success and isinstance(payload, dict) and "index" in payload:
                index = payload["index"]
                coverage = index["coverage"]
                count = sum(coverage["documents"].values())
                state = "索引已暂停" if index["paused"] else "正在建立索引" if coverage["scanning"] else "服务运行中"
                status_summary.set(f"{state} · 已发现 {count} 个文件/记录 · 已生成 {coverage['embedded_chunks']} 段语义索引")
            elif success:
                status_summary.set("操作完成。可刷新状态查看当前覆盖范围。")
            else:
                status_summary.set("操作失败，请查看下面的错误信息。")
            status_text.configure(state="normal")
            status_text.delete("1.0", "end")
            status_text.insert("1.0", json.dumps(payload, ensure_ascii=False, indent=2) if success else payload)
            status_text.configure(state="disabled")
            tabs.select(status_tab)
        window.after(200, poll)
    poll()
    window.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
