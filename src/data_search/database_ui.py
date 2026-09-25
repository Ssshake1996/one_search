"""Local, explicit database onboarding dialog; no SQL or JSON editing required."""
from __future__ import annotations

import copy
import queue
import threading

from .credentials import CredentialError, delete_credential, store_credential
from .databases import DatabaseError
from .preflight import check_database
from .source_setup import discover_source, propose_source


def connection_candidate(original: dict, values: dict, credential_ref: str | None = None) -> dict:
    """Preserve advanced options while replacing the explicitly edited connection."""
    candidate = copy.deepcopy(original)
    identity, kind = values.get("id", "").strip(), values.get("kind", "sqlite")
    if not identity or kind not in {"sqlite", "mysql", "postgres"}:
        raise ValueError("请填写唯一来源名称并选择数据库类型。")
    if original.get("kind") and original["kind"] != kind:
        candidate = {"id": identity, "kind": kind}
    candidate.update(id=identity, kind=kind)
    candidate.setdefault("allowed_tables", [])
    candidate.setdefault("allowed_columns", {})
    if kind == "sqlite":
        if not values.get("path", "").strip():
            raise ValueError("请选择 SQLite 文件。")
        candidate["path"] = values["path"].strip()
        for key in ("host", "port", "database", "user", "password_env", "credential_ref", "ssl"):
            candidate.pop(key, None)
    else:
        for key in ("host", "database", "user"):
            if not values.get(key, "").strip():
                raise ValueError("请填写地址、数据库名称和只读账号。")
            candidate[key] = values[key].strip()
        try:
            candidate["port"] = int(values.get("port") or (3306 if kind == "mysql" else 5432))
        except (ValueError, TypeError):
            raise ValueError("端口必须是 1–65535 的整数。") from None
        if not 1 <= candidate["port"] <= 65535:
            raise ValueError("端口必须是 1–65535 的整数。")
        candidate.pop("path", None)
        candidate.pop("password_env", None)
        candidate.pop("credential_ref", None)
        if values.get("auth", "vault") == "environment":
            if not values.get("password_env", "").strip():
                raise ValueError("请填写密码环境变量名称。")
            candidate["password_env"] = values["password_env"].strip()
        elif values.get("auth", "vault") == "vault":
            reference = credential_ref or original.get("credential_ref")
            if not reference:
                raise ValueError("请填写密码并保存到系统凭据库，或显式选择环境变量/无密码连接。")
            candidate["credential_ref"] = reference
        elif values.get("auth") != "none":
            raise ValueError("请选择密码保存方式。")
        certificate = values.get("ssl_ca", "").strip()
        ssl = copy.deepcopy(candidate.get("ssl") or {})
        if kind == "postgres":
            mode = values.get("ssl_mode", "").strip()
            for key in ("sslmode", "sslrootcert"):
                ssl.pop(key, None)
            if mode:
                ssl["sslmode"] = mode
            if certificate:
                ssl["sslrootcert"] = certificate
        else:
            ssl.pop("ca", None)
            if certificate:
                ssl.update(ca=certificate, check_hostname=bool(values.get("verify_hostname", True)))
        if ssl:
            candidate["ssl"] = ssl
        else:
            candidate.pop("ssl", None)
    return candidate


def editor_selection(table: str, state: dict) -> dict:
    selected = {"table": table, "columns": sorted(state["columns"])}
    if not selected["columns"]:
        raise ValueError(f"请为 {table} 勾选至少一个允许读取的字段。")
    if state.get("index"):
        if not state.get("text"):
            raise ValueError(f"请为 {table} 勾选正文索引字段，或选择仅实时查询。")
        selected.update(index_text_columns=sorted(state["text"]), id_column=state.get("key") or None)
        if state.get("watermark"):
            if not state.get("watermark_confirmed"):
                raise ValueError(f"请确认 {table} 的水位字段由源系统在每次新增/更新时维护。")
            selected["updated_column"] = state["watermark"]
    return selected


class DatabaseEditor:
    def __init__(self, parent, source, on_save):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
        self.tk, self.ttk, self.filedialog, self.messagebox = tk, ttk, filedialog, messagebox
        self.original, self.on_save = copy.deepcopy(source or {}), on_save
        self.window = tk.Toplevel(parent)
        self.window.title("数据库 · one_search")
        self.window.geometry("960x760")
        self.window.minsize(720, 560)
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.work = queue.Queue()
        self.busy, self.closed = False, False
        self.staged_refs, self.retained_ref = set(), None
        self.pending_reference = None
        self.catalog, self.states, self.current_table, self.current_column = {}, {}, None, None
        self.discovered_source = None
        outer = ttk.Frame(self.window, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="连接你的数据库", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(outer, text="先发现结构，再选择可读取的表和字段。保存前会测试只读权限。", wraplength=850).pack(anchor="w", pady=(5, 12))
        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill="both", expand=True)
        connection_tab, data_tab = ttk.Frame(self.tabs, padding=10), ttk.Frame(self.tabs, padding=10)
        self.tabs.add(connection_tab, text="1 连接")
        self.tabs.add(data_tab, text="2 选择数据", state="disabled")
        # The connection form remains reachable at small display sizes/scaling.
        canvas = tk.Canvas(connection_tab, highlightthickness=0)
        scroll = ttk.Scrollbar(connection_tab, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)
        form = ttk.Frame(canvas)
        form_id = canvas.create_window((0, 0), window=form, anchor="nw")
        form.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(form_id, width=event.width))
        form.columnconfigure(1, weight=1)
        ssl = self.original.get("ssl") or {}
        metadata = self.original.get("business_metadata", {})
        initial = {"id": self.original.get("id", ""), "kind": self.original.get("kind", "sqlite"),
            "path": self.original.get("path", ""), "host": self.original.get("host", "127.0.0.1"),
            "port": str(self.original.get("port", "")), "database": self.original.get("database", ""),
            "user": self.original.get("user", ""), "password_env": self.original.get("password_env", ""),
            "auth": "environment" if self.original.get("password_env") else "vault", "password": "",
            "ssl_ca": ssl.get("sslrootcert", ssl.get("ca", "")), "ssl_mode": ssl.get("sslmode", ""),
            "alias": metadata.get("alias", ""), "description": metadata.get("description", "")}
        self.values = {key: tk.StringVar(value=value) for key, value in initial.items()}
        self.verify_hostname = tk.BooleanVar(value=ssl.get("check_hostname", True))
        definitions = [("id", "来源名称"), ("kind", "类型"), ("path", "SQLite 文件"),
            ("host", "地址"), ("port", "端口（留空使用默认值）"), ("database", "数据库名称"), ("user", "只读账号"),
            ("auth", "密码保存方式"), ("password", "新密码（留空复用已保存凭据）"), ("password_env", "密码环境变量名"),
            ("ssl_ca", "TLS CA 证书（可选）"), ("ssl_mode", "PostgreSQL TLS 模式"),
            ("alias", "业务别名（可选）"), ("description", "业务说明（可选）")]
        self.connection_widgets = {}
        for row, (key, label) in enumerate(definitions):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=6)
            if key == "kind":
                widget = ttk.Combobox(form, textvariable=self.values[key], values=("sqlite", "mysql", "postgres"), state="readonly")
            elif key == "auth":
                widget = ttk.Combobox(form, textvariable=self.values[key], values=("vault", "environment", "none"), state="readonly")
            elif key == "ssl_mode":
                widget = ttk.Combobox(form, textvariable=self.values[key], values=("", "require", "verify-ca", "verify-full", "disable"), state="readonly")
            else:
                widget = ttk.Entry(form, textvariable=self.values[key], show="●" if key == "password" else "")
            widget.grid(row=row, column=1, sticky="ew", pady=6)
            self.connection_widgets[key] = widget
            if key in {"path", "ssl_ca"}:
                ttk.Button(form, text="浏览", command=lambda key=key: self.values[key].set(filedialog.askopenfilename(parent=self.window))).grid(row=row, column=2, padx=(7, 0))
        ttk.Checkbutton(form, text="MySQL TLS 校验证书主机名", variable=self.verify_hostname).grid(row=14, column=1, sticky="w", pady=5)
        ttk.Label(form, text="vault：系统凭据库；environment：环境变量；none：显式无密码连接。\n系统凭据属于当前系统账号；密码不会写入配置文件。", wraplength=630).grid(row=15, column=0, columnspan=3, sticky="w", pady=8)
        self.discover_button = ttk.Button(form, text="发现可选表与字段", command=self.discover, style="Primary.TButton")
        self.discover_button.grid(row=16, column=1, sticky="w", pady=(10, 18))
        self.values["kind"].trace_add("write", lambda *_: self._connection_fields())
        self.values["auth"].trace_add("write", lambda *_: self._connection_fields())
        self._connection_fields()
        data_canvas = tk.Canvas(data_tab, highlightthickness=0)
        data_scroll = ttk.Scrollbar(data_tab, orient="vertical", command=data_canvas.yview)
        data_canvas.configure(yscrollcommand=data_scroll.set)
        data_scroll.pack(side="right", fill="y")
        data_canvas.pack(fill="both", expand=True)
        data_form = ttk.Frame(data_canvas)
        data_id = data_canvas.create_window((0, 0), window=data_form, anchor="nw")
        data_form.bind("<Configure>", lambda event: data_canvas.configure(scrollregion=data_canvas.bbox("all")))
        data_canvas.bind("<Configure>", lambda event: data_canvas.itemconfigure(data_id, width=event.width))
        data_tab = data_form
        data_tab.columnconfigure(1, weight=1)
        data_tab.rowconfigure(1, weight=1)
        ttk.Label(data_tab, text="勾选表，再逐列选择“读取”与“索引”。未选择的字段不会开放。", wraplength=850).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        left = ttk.Frame(data_tab)
        left.grid(row=1, column=0, sticky="ns", padx=(0, 10))
        self.table_tree = ttk.Treeview(left, show="tree", height=10, selectmode="browse")
        self.table_tree.column("#0", width=180, minwidth=100)
        table_scroll = ttk.Scrollbar(left, orient="vertical", command=self.table_tree.yview)
        self.table_tree.configure(yscrollcommand=table_scroll.set)
        self.table_tree.pack(side="left", fill="both", expand=True)
        table_scroll.pack(side="right", fill="y")
        self.table_tree.bind("<<TreeviewSelect>>", self._select_table)
        self.table_tree.bind("<Double-1>", self._toggle_table)
        self.table_tree.bind("<space>", self._toggle_table)
        right = ttk.Frame(data_tab)
        right.grid(row=1, column=1, sticky="nsew")
        self.table_enabled = tk.BooleanVar()
        self.table_alias, self.table_description = tk.StringVar(), tk.StringVar()
        self.index_enabled, self.watermark_confirmed = tk.BooleanVar(), tk.BooleanVar()
        self.key, self.watermark, self.table_note = tk.StringVar(), tk.StringVar(), tk.StringVar()
        ttk.Checkbutton(right, text="允许读取这张表", variable=self.table_enabled, command=self._remember_table).pack(anchor="w")
        labels = ttk.Frame(right)
        labels.pack(fill="x", pady=5)
        ttk.Label(labels, text="表别名").pack(side="left")
        ttk.Entry(labels, textvariable=self.table_alias, width=18).pack(side="left", padx=5)
        ttk.Label(labels, text="说明").pack(side="left")
        ttk.Entry(labels, textvariable=self.table_description).pack(side="left", fill="x", expand=True, padx=5)
        fields = ttk.Frame(right)
        fields.pack(fill="both", expand=True, pady=5)
        self.column_tree = ttk.Treeview(fields, columns=("read", "index", "name", "type"), show="headings", height=8, selectmode="browse")
        for key, label, width in (("read", "读取", 48), ("index", "索引", 48), ("name", "字段", 190), ("type", "类型", 100)):
            self.column_tree.heading(key, text=label)
            self.column_tree.column(key, width=width, minwidth=40, stretch=key in {"name", "type"})
        field_scroll = ttk.Scrollbar(fields, orient="vertical", command=self.column_tree.yview)
        self.column_tree.configure(yscrollcommand=field_scroll.set)
        self.column_tree.pack(side="left", fill="both", expand=True)
        field_scroll.pack(side="right", fill="y")
        self.column_tree.bind("<ButtonRelease-1>", self._column_click)
        self.column_tree.bind("<<TreeviewSelect>>", self._select_column)
        self.column_tree.bind("<space>", self._column_read_key)
        self.column_alias, self.column_description = tk.StringVar(), tk.StringVar()
        for label, variable in (("字段别名", self.column_alias), ("字段说明", self.column_description)):
            row = ttk.Frame(right)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=label, width=10).pack(side="left")
            ttk.Entry(row, textvariable=variable).pack(side="left", fill="x", expand=True)
        self.index_toggle = ttk.Checkbutton(right, text="同时建立本地正文索引（不勾选时仅实时查询）", variable=self.index_enabled)
        self.index_toggle.pack(anchor="w", pady=(8, 4))
        choice = ttk.Frame(right)
        choice.pack(fill="x", pady=3)
        ttk.Label(choice, text="稳定唯一键").pack(side="left")
        self.key_choice = ttk.Combobox(choice, textvariable=self.key, state="readonly", width=17)
        self.key_choice.pack(side="left", padx=5)
        ttk.Label(choice, text="水位（可选）").pack(side="left")
        self.watermark_choice = ttk.Combobox(choice, textvariable=self.watermark, state="readonly", width=17)
        self.watermark_choice.pack(side="left", padx=5)
        ttk.Checkbutton(right, text="确认源系统在每次新增/更新时维护所选水位", variable=self.watermark_confirmed).pack(anchor="w", pady=3)
        ttk.Label(right, textvariable=self.table_note, wraplength=520).pack(anchor="w", pady=5)
        self.activity = tk.StringVar(value="先填写连接信息。")
        ttk.Label(outer, textvariable=self.activity, wraplength=880).pack(anchor="w", pady=(12, 6))
        footer = ttk.Frame(outer)
        footer.pack(fill="x")
        self.save_button = ttk.Button(footer, text="测试并使用此配置", command=self.save, state="disabled", style="Primary.TButton")
        self.save_button.pack(side="right")
        ttk.Button(footer, text="取消", command=self.close).pack(side="right", padx=8)
        self.window.after(75, self._poll)

    def _connection_fields(self):
        sqlite = self.values["kind"].get() == "sqlite"
        for key in ("host", "port", "database", "user", "auth", "password", "password_env", "ssl_ca", "ssl_mode"):
            enabled = not sqlite and (key != "password" or self.values["auth"].get() == "vault") and (key != "password_env" or self.values["auth"].get() == "environment")
            self.connection_widgets[key].configure(state=("readonly" if key in {"auth", "ssl_mode"} else "normal") if enabled else "disabled")
        self.connection_widgets["path"].configure(state="normal" if sqlite else "disabled")

    def _start(self, work, callback):
        if self.busy:
            return
        self.busy = True
        self.discover_button.configure(state="disabled")
        self.save_button.configure(state="disabled")
        def run():
            try:
                result = work()
                self.work.put((callback, result, None))
            except (CredentialError, DatabaseError, ValueError) as error:
                self.work.put((callback, None, str(error)))
            except Exception:
                self.work.put((callback, None, "操作失败。请检查连接与系统凭据库后重试。"))
        threading.Thread(target=run, daemon=True).start()

    def _poll(self):
        if self.closed:
            return
        try:
            callback, result, error = self.work.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            self.discover_button.configure(state="normal")
            self.save_button.configure(state="normal" if self.catalog else "disabled")
            if error:
                self.activity.set(error)
            else:
                callback(result)
        if not self.closed:
            self.window.after(75, self._poll)

    def discover(self):
        values = {key: value.get() for key, value in self.values.items()}
        values["verify_hostname"] = self.verify_hostname.get()
        secret = values.pop("password")
        self.values["password"].set("")
        self.activity.set("正在连接并发现结构；不读取记录正文。")
        def discover():
            reference = self.pending_reference or (self.discovered_source.get("credential_ref") if self.discovered_source else None)
            if values["kind"] != "sqlite" and values["auth"] == "vault" and secret:
                reference = store_credential(secret)
                self.staged_refs.add(reference)
                self.pending_reference = reference
            candidate = connection_candidate(self.original, values, reference)
            return candidate, discover_source(candidate)
        self._start(discover, self._loaded)

    def _loaded(self, result):
        source, report = result
        if not report["ok"]:
            self.activity.set("；".join(item["message"] + " " + item.get("action", "") for item in report["diagnostics"]))
            return
        self.discovered_source = source
        self.catalog = {table["table"]: table for table in report["tables"]}
        self.states, self.current_table, self.current_column = {}, None, None
        self.table_tree.delete(*self.table_tree.get_children())
        metadata = self.original.get("business_metadata", {}).get("tables", {})
        indexes = {entry["table"]: entry for entry in self.original.get("index", [])}
        for name, table in self.catalog.items():
            existing = name in self.original.get("allowed_tables", []) and self.original.get("kind") == source["kind"]
            columns = {field["name"] for field in table["columns"]}
            entry, labels = indexes.get(name, {}) if existing else {}, metadata.get(name, {}) if existing else {}
            self.states[name] = {"enabled": existing, "columns": columns.intersection(self.original.get("allowed_columns", {}).get(name, columns)) if existing else set(),
                "text": columns.intersection(entry.get("text_columns", [])), "index": bool(entry),
                "key": entry.get("id_column", table["index_recommendation"]["id_column"] or ""),
                "watermark": entry.get("updated_column", ""), "watermark_confirmed": bool(entry.get("updated_column")),
                "alias": labels.get("alias", ""), "description": labels.get("description", ""), "column_labels": copy.deepcopy(labels.get("columns", {}))}
            self.table_tree.insert("", "end", iid=name, text=("☑ " if existing else "☐ ") + name)
        self.tabs.tab(1, state="normal")
        self.tabs.select(1)
        self.save_button.configure(state="normal" if self.catalog else "disabled")
        self.activity.set(f"已发现 {len(self.catalog)} 张表/视图。双击表名前的方框选择，再勾选字段。" + (" 结果已截断为前 64 张表。" if report.get("truncated") else ""))
        if self.catalog:
            self.table_tree.selection_set(next(iter(self.catalog)))

    def _remember_column(self):
        if self.current_table and self.current_column:
            labels = {key: value for key, value in (("alias", self.column_alias.get()), ("description", self.column_description.get())) if value}
            self.states[self.current_table]["column_labels"][self.current_column] = labels

    def _remember_table(self):
        self._remember_column()
        if self.current_table:
            state = self.states[self.current_table]
            state.update(enabled=self.table_enabled.get(), alias=self.table_alias.get(), description=self.table_description.get(),
                index=self.index_enabled.get(), key=self.key.get(), watermark=self.watermark.get(), watermark_confirmed=self.watermark_confirmed.get())
            self.table_tree.item(self.current_table, text=("☑ " if state["enabled"] else "☐ ") + self.current_table)

    def _select_table(self, _event=None):
        selection = self.table_tree.selection()
        if not selection or selection[0] == self.current_table:
            return
        self._remember_table()
        self.current_table, self.current_column = selection[0], None
        state, table = self.states[self.current_table], self.catalog[self.current_table]
        self.table_enabled.set(state["enabled"])
        for variable, key in ((self.table_alias, "alias"), (self.table_description, "description"), (self.index_enabled, "index"),
                              (self.key, "key"), (self.watermark, "watermark"), (self.watermark_confirmed, "watermark_confirmed")):
            variable.set(state[key])
        recommendation = table["index_recommendation"]
        self.key_choice.configure(values=[key["column"] for key in recommendation["key_candidates"]])
        self.watermark_choice.configure(values=[""] + [field["name"] for field in table["columns"]])
        available = bool(recommendation["key_candidates"])
        self.index_toggle.configure(state="normal" if available else "disabled")
        if not available:
            self.index_enabled.set(False)
        self.table_note.set("水位留空时按周期完整扫描。主键和水位也需勾选读取；唯一键仍需通过保存前预检。" if available else "未发现可用的单列唯一键，或此对象是视图。可选择字段用于实时查询。")
        self.column_alias.set("")
        self.column_description.set("")
        self.column_tree.delete(*self.column_tree.get_children())
        for field in table["columns"]:
            self.column_tree.insert("", "end", iid=field["name"], values=("☑" if field["name"] in state["columns"] else "☐",
                "☑" if field["name"] in state["text"] else "☐", field["name"], field["type"]))

    def _toggle_table(self, _event=None):
        self._select_table()
        if self.current_table:
            self.table_enabled.set(not self.table_enabled.get())
            self._remember_table()
        return "break"

    def _select_column(self, _event=None):
        selection = self.column_tree.selection()
        if not selection or selection[0] == self.current_column:
            return
        self._remember_column()
        self.current_column = selection[0]
        labels = self.states[self.current_table]["column_labels"].get(self.current_column, {})
        self.column_alias.set(labels.get("alias", ""))
        self.column_description.set(labels.get("description", ""))

    def _toggle_column(self, column, index=False):
        if not self.current_table:
            return
        state = self.states[self.current_table]
        selected = state["text"] if index else state["columns"]
        if index and not self.catalog[self.current_table]["index_recommendation"]["key_candidates"]:
            self.activity.set("这张表仅支持实时查询；未发现持续索引所需的唯一键。")
            return
        if column in selected:
            selected.remove(column)
            if not index:
                state["text"].discard(column)
        else:
            selected.add(column)
            if index:
                state["columns"].add(column)
                self.index_enabled.set(True)
            self.table_enabled.set(True)
        values = list(self.column_tree.item(column, "values"))
        values[:2] = ["☑" if column in state["columns"] else "☐", "☑" if column in state["text"] else "☐"]
        self.column_tree.item(column, values=values)
        self._remember_table()

    def _column_click(self, event):
        row, column = self.column_tree.identify_row(event.y), self.column_tree.identify_column(event.x)
        if row and column in {"#1", "#2"}:
            self._toggle_column(row, index=column == "#2")

    def _column_read_key(self, _event):
        if self.column_tree.selection():
            self._toggle_column(self.column_tree.selection()[0])
        return "break"

    def save(self):
        if self.busy or not self.discovered_source:
            return
        self._remember_table()
        try:
            values = {key: value.get() for key, value in self.values.items()}
            values["verify_hostname"] = self.verify_hostname.get()
            current = connection_candidate(self.original, values, self.discovered_source.get("credential_ref"))
            # Changed connection details require a fresh visible discovery step.
            if current != self.discovered_source or values["password"]:
                raise ValueError("连接信息已改变，请先重新发现表与字段。")
            selections = [editor_selection(name, state) for name, state in self.states.items() if state["enabled"]]
            if not selections:
                raise ValueError("请至少选择一张表及允许读取的字段。")
            metadata = {"alias": values["alias"], "description": values["description"], "tables": {}}
            for name, state in self.states.items():
                if state["enabled"]:
                    metadata["tables"][name] = {"alias": state["alias"], "description": state["description"],
                        "columns": {column: labels for column, labels in state["column_labels"].items() if column in state["columns"]}}
        except (ValueError, DatabaseError) as error:
            self.activity.set(str(error))
            return
        self.activity.set("正在核对字段与唯一键，并测试只读权限；通过后加入设置。")
        def validate():
            proposal = propose_source(self.discovered_source, selections, metadata)
            if not proposal["ok"]:
                raise ValueError("；".join(item["message"] for item in proposal["diagnostics"]))
            check = check_database(proposal["source"])
            if not check["ok"]:
                raise ValueError("；".join(item["message"] + " " + item.get("action", "") for item in check["checks"] if not item["ok"]))
            return proposal["source"]
        self._start(validate, self._saved)

    def _saved(self, source):
        try:
            self.on_save(source)
        except Exception:
            self.activity.set("未能加入设置，请检查来源名称是否重复后重试。")
            return
        self.retained_ref = source.get("credential_ref")
        self.close()

    def close(self):
        if self.busy:
            self.activity.set("正在完成有时限的操作，完成后即可关闭。")
            return
        if self.closed:
            return
        self.closed = True
        self.window.destroy()
        obsolete = self.staged_refs - {self.retained_ref}
        def cleanup():
            for reference in obsolete:
                try:
                    delete_credential(reference)
                except CredentialError:
                    pass
        if obsolete:
            threading.Thread(target=cleanup, daemon=True).start()


def open_database_editor(parent, source, on_save):
    """Open an async Tk editor and invoke on_save(candidate) only after preflight."""
    return DatabaseEditor(parent, source, on_save)
