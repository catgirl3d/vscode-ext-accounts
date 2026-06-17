import datetime
import os
from dataclasses import dataclass, field
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import saved_account_status


IDE_EXTENSION_ORDER = ("kilocode", "roo-cline", "kilo-new")
IDE_STATE_LABEL_WIDTH = 24
CURRENT_IDE_LABEL_WIDTH = 24
CODEX_CURRENT_LABEL_WIDTH = 16
EXPIRED_ROW_TAG = "expired"
EXPIRED_ROW_FG = "#f38ba8"
REFRESH_ERROR_ROW_TAG = "refresh_error"
REFRESH_ERROR_ROW_FG = "#fab387"
TERMINAL_REFRESH_ERROR_ROW_TAG = "terminal_refresh_error"
TERMINAL_REFRESH_ERROR_ROW_FG = "#f38ba8"


def current_time_ms():
    return int(datetime.datetime.now().timestamp() * 1000)


@dataclass
class GuiServices:
    root: tk.Tk
    db: Any
    bg: str
    fg: str
    btn_bg: str
    btn_act: str
    sel_fg: str
    run_guarded: Callable[..., None]
    set_status: Callable[..., None]
    refresh_all: Callable[[], None] = field(default=lambda: None)


def format_saved_at(data):
    return data.get("saved_at", "")[:16].replace("T", " ") or "?"


def format_expires_ms(expires_ms):
    if not expires_ms:
        return ""
    try:
        exp_dt = datetime.datetime.fromtimestamp(expires_ms / 1000)
        return exp_dt.strftime("%Y-%m-%d")
    except Exception:
        return ""


def is_expired_ms(expires_ms, now_ms=None):
    if not isinstance(expires_ms, int) or expires_ms <= 0:
        return False
    current_ms = now_ms if now_ms is not None else current_time_ms()
    return expires_ms <= current_ms


def format_saved_expires(expires_ms, now_ms=None):
    if is_expired_ms(expires_ms, now_ms=now_ms):
        return "expired"
    return format_expires_ms(expires_ms)


def expires_row_tags(expires_ms, now_ms=None):
    if is_expired_ms(expires_ms, now_ms=now_ms):
        return (EXPIRED_ROW_TAG,)
    return ()


def format_refresh_status(status):
    if status == saved_account_status.REFRESH_STATUS_TERMINAL_ERROR:
        return "invalid"
    if status == saved_account_status.REFRESH_STATUS_ERROR:
        return "error"
    if status == saved_account_status.REFRESH_STATUS_OK:
        return "ok"
    return "-"


def account_row_tags(data, expires_ms, now_ms=None):
    status = data.get("refresh_status") if isinstance(data, dict) else None
    if status == saved_account_status.REFRESH_STATUS_TERMINAL_ERROR:
        return (TERMINAL_REFRESH_ERROR_ROW_TAG,)
    if status == saved_account_status.REFRESH_STATUS_ERROR:
        return (REFRESH_ERROR_ROW_TAG,)
    return expires_row_tags(expires_ms, now_ms=now_ms)


def shorten_account_id(account_id, limit=12):
    if isinstance(account_id, str) and len(account_id) > limit:
        return account_id[:limit] + "..."
    return account_id or "?"


def summarize_account_ids(entries, skip_keys=None):
    skip_keys = set(skip_keys or [])
    account_ids = []
    for entry in entries:
        if entry.get("key") in skip_keys:
            continue
        value = entry.get("value", {})
        if isinstance(value, dict) and value.get("accountId"):
            account_ids.append(shorten_account_id(value["accountId"], limit=8))
    return ", ".join(account_ids) if account_ids else "?"


def first_expires(entries, skip_keys=None):
    expires_ms = first_expires_ms(entries, skip_keys=skip_keys)
    return format_saved_expires(expires_ms)


def first_expires_ms(entries, skip_keys=None):
    skip_keys = set(skip_keys or [])
    expires_values = []
    for entry in entries:
        if entry.get("key") in skip_keys:
            continue
        value = entry.get("value", {})
        if isinstance(value, dict):
            expires_ms = value.get("expires")
            if isinstance(expires_ms, int) and expires_ms > 0:
                expires_values.append(expires_ms)
    return min(expires_values, default=0)


def selected_name(tree, empty_message):
    selection = tree.selection()
    if not selection:
        messagebox.showwarning("No selection", empty_message)
        return None
    return selection[0]


def ask_account_name(root, title, prompt, initialvalue=None):
    name = simpledialog.askstring(title, prompt, parent=root, initialvalue=initialvalue)
    if not name:
        return None
    normalized = name.strip()
    return normalized or None


def delete_saved_account(db_module, name):
    db_module.delete_saved_account(name)


def select_tree_item(tree, name):
    if not tree.exists(name):
        return
    tree.selection_set(name)
    tree.focus(name)
    tree.see(name)
    tree.focus_set()


def popup_tree_context_menu(tree, menu, event):
    item = tree.identify_row(event.y)
    if not item:
        return
    tree.selection_set(item)
    tree.focus(item)
    tree.see(item)
    tree.focus_set()
    try:
        menu.tk_popup(event.x_root, event.y_root)
    finally:
        menu.grab_release()


def tab_button(parent, services: GuiServices, text: str, cmd, accent=False):
    bg = "#89b4fa" if accent else services.btn_bg
    fg = services.sel_fg if accent else services.fg
    button = tk.Button(
        parent,
        text=text,
        command=cmd,
        bg=bg,
        fg=fg,
        activebackground=services.btn_act,
        activeforeground=services.fg,
        relief="flat",
        padx=12,
        pady=6,
        font=("Segoe UI", 10),
        cursor="hand2",
    )
    button.pack(side="left", padx=4)
    return button


class IdeAccountsTab:
    def __init__(self, notebook: ttk.Notebook, services: GuiServices):
        self.services = services
        self.frame = tk.Frame(notebook, bg=services.bg)
        notebook.add(self.frame, text="IDE Accounts")

        self.ide_var = tk.StringVar(value="vscode")
        self.ide_ext_vars = {
            "kilocode": tk.BooleanVar(value=False),
            "roo-cline": tk.BooleanVar(value=False),
            "kilo-new": tk.BooleanVar(value=False),
        }
        self.current_ide_labels: dict[str, tk.Label] = {}
        self.run_button_visible = False
        self._last_runtime_state: tuple[str, bool] | None = None

        self._build()

    def _build(self):
        db = self.services.db
        bg = self.services.bg
        fg = self.services.fg

        ide_top = tk.Frame(self.frame, bg=bg, pady=6)
        ide_top.pack(fill="x", padx=10)

        tk.Label(ide_top, text="IDE:", bg=bg, fg="#6c7086", font=("Segoe UI", 9)).pack(side="left")
        for value, label in [("vscode", "VSCode"), ("antigravity", "Antigravity")]:
            tk.Radiobutton(
                ide_top,
                text=label,
                variable=self.ide_var,
                value=value,
                command=self.on_ide_change,
                bg=bg,
                fg=fg,
                selectcolor=self.services.btn_bg,
                activebackground=bg,
                activeforeground=fg,
                font=("Segoe UI", 9, "bold"),
            ).pack(side="left", padx=4)

        tk.Label(ide_top, text="  ", bg=bg).pack(side="left")

        self.ide_state_label = tk.Label(
            ide_top,
            text="",
            width=IDE_STATE_LABEL_WIDTH,
            anchor="w",
            bg=bg,
            fg=fg,
            font=("Segoe UI", 10, "bold"),
        )
        self.ide_state_label.pack(side="left", padx=(0, 20))

        tk.Label(ide_top, text="Extensions:", bg=bg, fg="#6c7086", font=("Segoe UI", 9)).pack(side="left")
        ide_ext_frame = tk.Frame(ide_top, bg=bg)
        ide_ext_frame.pack(side="left")
        for value, label in [("kilocode", "Kilocode"), ("roo-cline", "Roo-Cline"), ("kilo-new", "Kilo New")]:
            tk.Checkbutton(
                ide_ext_frame,
                text=label,
                variable=self.ide_ext_vars[value],
                bg=bg,
                fg=fg,
                selectcolor=self.services.btn_bg,
                activebackground=bg,
                activeforeground=fg,
                font=("Segoe UI", 9),
            ).pack(side="left", padx=4)

        current_ide_frame = tk.Frame(self.frame, bg=bg)
        current_ide_frame.pack(fill="x", padx=10, pady=(0, 2))

        self.current_ide_label = tk.Label(
            current_ide_frame,
            text="Current in VSCode:",
            width=CURRENT_IDE_LABEL_WIDTH,
            anchor="w",
            bg=bg,
            fg="#6c7086",
            font=("Segoe UI", 9, "bold"),
        )
        self.current_ide_label.pack(side="left")

        for ext_name in IDE_EXTENSION_ORDER:
            ext_id = db.IDE_EXTENSIONS[ext_name]
            label = tk.Label(current_ide_frame, text="", bg=bg, fg="#6c7086", font=("Segoe UI", 9))
            label.pack(side="left", padx=(8, 0))
            self.current_ide_labels[ext_id] = label

        ide_cols = ("name", "ext", "accountIds", "saved", "expires", "active", "status")
        self.tree = ttk.Treeview(self.frame, columns=ide_cols, show="headings", height=8, selectmode="browse")
        self.tree.heading("name", text="Name")
        self.tree.heading("ext", text="Ext")
        self.tree.heading("accountIds", text="Account IDs")
        self.tree.heading("saved", text="Saved")
        self.tree.heading("expires", text="Expires")
        self.tree.heading("active", text="Active")
        self.tree.heading("status", text="Status")
        self.tree.column("name", width=225, anchor="w")
        self.tree.column("ext", width=75, anchor="w")
        self.tree.column("accountIds", width=100, anchor="w")
        self.tree.column("saved", width=112, anchor="center")
        self.tree.column("expires", width=80, anchor="center")
        self.tree.column("active", width=60, anchor="center")
        self.tree.column("status", width=60, anchor="center")
        self.tree.tag_configure(EXPIRED_ROW_TAG, foreground=EXPIRED_ROW_FG)
        self.tree.tag_configure(REFRESH_ERROR_ROW_TAG, foreground=REFRESH_ERROR_ROW_FG)
        self.tree.tag_configure(TERMINAL_REFRESH_ERROR_ROW_TAG, foreground=TERMINAL_REFRESH_ERROR_ROW_FG)
        self.tree.pack(padx=10, pady=(0, 6))
        self.context_menu = tk.Menu(self.tree, tearoff=False)
        self.context_menu.add_command(label="Use selected", command=self.on_use)
        self.context_menu.add_command(label="Rename", command=self.on_rename)
        self.tree.bind("<F2>", self.on_rename_key)
        self.tree.bind("<Button-3>", self.on_tree_context_menu)

        self.btn_frame = tk.Frame(self.frame, bg=bg)
        self.btn_frame.pack(padx=10, pady=(0, 6))
        tab_button(self.btn_frame, self.services, "💾 Save current", self.on_save, accent=True)
        tab_button(self.btn_frame, self.services, "▶ Use selected", self.on_use)
        tab_button(self.btn_frame, self.services, "↻ Refresh selected", self.on_refresh_selected)
        tab_button(self.btn_frame, self.services, "✏ Rename", self.on_rename)
        tab_button(self.btn_frame, self.services, "🗑 Delete", self.on_delete)
        tab_button(self.btn_frame, self.services, "⟳ Refresh", self.on_refresh)
        self.run_button = tab_button(self.btn_frame, self.services, "RUN", self.on_run)
        self.backup_button = tab_button(self.btn_frame, self.services, "📦 Full backup", self.on_backup)
        self.run_button.pack_forget()

    def update_run_button_visibility(self, running: bool):
        if running:
            if self.run_button_visible:
                self.run_button.pack_forget()
                self.run_button_visible = False
            return

        if not self.run_button_visible:
            self.run_button.pack(side="left", padx=4, before=self.backup_button)
            self.run_button_visible = True

    def selected_exts(self, show_warning=True):
        exts = [name for name in IDE_EXTENSION_ORDER if self.ide_ext_vars[name].get()]
        if not exts and show_warning:
            messagebox.showwarning("No extension", "Select at least one IDE extension.")
        return exts

    def format_ext_selection(self, exts):
        return "+".join(exts)

    def db_target_ides_for_exts(self, exts):
        return [self.ide_var.get()] if any(ext in ("kilocode", "roo-cline") for ext in exts) else []

    def kilo_new_target_ides_for_exts(self, exts):
        return list(self.services.db.IDE_PATHS) if "kilo-new" in exts else []

    def can_hot_swap_kilo_new(self, exts, db_target_ides, running_kilo_new_ides):
        return "kilo-new" in exts and bool(running_kilo_new_ides) and not any(
            ide in db_target_ides for ide in running_kilo_new_ides
        )

    def required_closed_ides_for_exts(self, exts, *, allow_kilo_new_while_running=False):
        targets = []
        for ide in self.db_target_ides_for_exts(exts):
            if ide not in targets:
                targets.append(ide)
        if not allow_kilo_new_while_running:
            for ide in self.kilo_new_target_ides_for_exts(exts):
                if ide not in targets:
                    targets.append(ide)
        return targets

    def format_ide_labels(self, ides):
        labels = [self.services.db.IDE_PATHS[ide]["label"] for ide in ides]
        if not labels:
            return ""
        if len(labels) == 1:
            return labels[0]
        return " and ".join(labels)

    def update_current_labels(self, current_accounts):
        db = self.services.db
        ide_label_text = db.IDE_PATHS[self.ide_var.get()]["label"]
        self.current_ide_label.config(text=f"Current in {ide_label_text}:")
        for ext_id, widget in self.current_ide_labels.items():
            info = current_accounts.get(ext_id)
            short = db._EXT_DISPLAY.get(ext_id, ext_id)
            if info:
                widget.config(text=f"  {short}: {shorten_account_id(info.get('accountId'))}", fg="#a6e3a1")
            else:
                widget.config(text=f"  {short}: -", fg="#6c7086")

    def refresh_runtime_state(self, force: bool = False) -> bool:
        current_ide = self.ide_var.get()
        running = self.services.db.is_ide_running(current_ide)
        runtime_state = (current_ide, running)
        if not force and runtime_state == self._last_runtime_state:
            return False

        ide_cfg = self.services.db.IDE_PATHS[current_ide]
        state_str = "running !" if running else "closed OK"
        state_color = "#c0392b" if running else "#2d8a4e"
        self.ide_state_label.config(text=f"{ide_cfg['label']}: {state_str}", fg=state_color)
        self.update_run_button_visibility(running)
        self._last_runtime_state = runtime_state
        return True

    def refresh(self):
        db = self.services.db
        self.tree.delete(*self.tree.get_children())

        accounts_per_ide = {}
        for ide in db.IDE_PATHS:
            try:
                accounts_per_ide[ide] = db.read_current_accounts_for_ide(ide)
            except Exception:
                accounts_per_ide[ide] = {}

        current_ide = self.ide_var.get()
        self.update_current_labels(accounts_per_ide.get(current_ide, {}))

        try:
            kilo_new_fp = db.get_kilo_new_fingerprint()
        except Exception:
            kilo_new_fp = None

        for record in db.list_saved_accounts("ide"):
            name = record["name"]
            data = record["data"]
            entries = data.get("entries", [])
            ide_entries = [entry for entry in entries if entry.get("key") != db.CODEX_KEY]

            saved_at = format_saved_at(data)
            ext_tag = data.get("ext", "both")
            expires_ms = first_expires_ms(ide_entries)
            expires = format_saved_expires(expires_ms)
            accounts_short = summarize_account_ids(ide_entries)
            refresh_status = format_refresh_status(data.get("refresh_status"))

            active_tags = []
            ide_short = {"vscode": "VS", "antigravity": "AG"}
            for ide, current_accounts in accounts_per_ide.items():
                ide_accounts = {
                    ext_id: info
                    for ext_id, info in current_accounts.items()
                    if ext_id != db.KILO_NEW_KEY
                }
                hits = db.match_saved_to_current(ide_entries, ide_accounts)
                if hits:
                    tag = ide_short.get(ide, ide)
                    if tag not in active_tags:
                        active_tags.append(tag)

            if kilo_new_fp:
                for entry in ide_entries:
                    if db.account_fingerprint(entry.get("value", {})) == kilo_new_fp:
                        if "KN" not in active_tags:
                            active_tags.append("KN")
                        break

            active = "+".join(active_tags) if active_tags else "-"
            self.tree.insert(
                "",
                "end",
                iid=name,
                values=(name, ext_tag, accounts_short, saved_at, expires, active, refresh_status),
                tags=account_row_tags(data, expires_ms),
            )

        self.refresh_runtime_state(force=True)

    def on_ide_change(self):
        self.services.db.set_ide(self.ide_var.get())
        self.refresh()

    def on_tree_context_menu(self, event):
        popup_tree_context_menu(self.tree, self.context_menu, event)
        return "break"

    def on_rename_key(self, _event=None):
        self.on_rename()
        return "break"

    def on_rename(self):
        name = selected_name(self.tree, "Select an IDE account first.")
        if not name:
            return
        new_name = ask_account_name(self.services.root, "Rename IDE account", "New account name:", initialvalue=name)
        if not new_name or new_name == name:
            return
        try:
            self.services.db.rename_saved_account(name, new_name, expected_kind="ide")
            self.services.refresh_all()
            select_tree_item(self.tree, new_name)
            self.services.set_status(f"Renamed '{name}' to '{new_name}'", True)
        except Exception as exc:
            self.services.set_status(str(exc), False)

    def on_save(self):
        name = ask_account_name(self.services.root, "Save IDE account", "Account name:")
        if not name:
            return
        exts = self.selected_exts()
        if not exts:
            return
        label = self.format_ext_selection(exts)
        self.services.run_guarded(self.services.db.save_ide_account, name, exts, success_msg=f"Saved '{name}' [{label}]")

    def on_use(self):
        name = selected_name(self.tree, "Select an IDE account first.")
        if not name:
            return
        exts = self.selected_exts()
        if not exts:
            return

        db_target_ides = self.db_target_ides_for_exts(exts)
        running_ides = [ide for ide in db_target_ides if self.services.db.is_ide_running(ide)]
        if running_ides:
            running_labels = self.format_ide_labels(running_ides)
            messagebox.showerror(f"{running_labels} running", f"Close {running_labels} before switching accounts.")
            return

        allow_kilo_new_while_running = False
        kilo_new_target_ides = self.kilo_new_target_ides_for_exts(exts)
        running_kilo_new_ides = [ide for ide in kilo_new_target_ides if self.services.db.is_ide_running(ide)]
        if running_kilo_new_ides:
            if not self.can_hot_swap_kilo_new(exts, db_target_ides, running_kilo_new_ides):
                running_labels = self.format_ide_labels(running_kilo_new_ides)
                messagebox.showerror(f"{running_labels} running", f"Close {running_labels} before switching accounts.")
                return

            running_labels = self.format_ide_labels(running_kilo_new_ides)
            prompt = (
                f"{running_labels} may be using shared Kilo New auth.\n"
                "Experimental mode will rewrite the shared Kilo New auth.json without closing the IDE.\n"
                "Use this only for testing. Continue?"
            )
            if not messagebox.askyesno("Experimental Kilo New write", prompt):
                return
            allow_kilo_new_while_running = True

        label = self.format_ext_selection(exts)
        target_ides = self.required_closed_ides_for_exts(exts, allow_kilo_new_while_running=allow_kilo_new_while_running)
        hold_labels = self.format_ide_labels(target_ides)
        prompt = f"Switch '{name}' [{label}]?"
        if hold_labels:
            prompt += f"\n{hold_labels} must stay closed until done."
        if not messagebox.askyesno("Switch IDE account", prompt):
            return

        self.services.run_guarded(
            self.services.db.use_ide_account,
            name,
            exts,
            allow_kilo_new_while_running,
            success_msg=f"Switched '{name}' [{label}]",
        )

    def on_delete(self):
        name = selected_name(self.tree, "Select an IDE account first.")
        if not name:
            return
        if not messagebox.askyesno("Delete", f"Delete saved account '{name}'?"):
            return
        try:
            delete_saved_account(self.services.db, name)
            self.services.set_status(f"Deleted '{name}'", True)
            self.services.refresh_all()
        except Exception as exc:
            self.services.set_status(str(exc), False)

    def on_refresh_selected(self):
        name = selected_name(self.tree, "Select an IDE account first.")
        if not name:
            return
        self.services.run_guarded(self.services.db.refresh_saved_account, name, log_prefix="manual-refresh")

    def on_backup(self):
        self.services.run_guarded(self.services.db.backup)

    def on_run(self):
        try:
            message = self.services.db.launch_ide(self.ide_var.get())
        except Exception as exc:
            messagebox.showerror("Run IDE", str(exc))
            self.services.set_status(str(exc), False)
            return

        self.services.set_status(message, True)
        self.refresh()

    def on_refresh(self):
        self.services.refresh_all()
        self.services.set_status("Refreshed", True)


class CodexTab:
    def __init__(self, notebook: ttk.Notebook, services: GuiServices):
        self.services = services
        self.frame = tk.Frame(notebook, bg=services.bg)
        notebook.add(self.frame, text="Codex")
        self._build()

    def _build(self):
        bg = self.services.bg
        fg = self.services.fg

        top = tk.Frame(self.frame, bg=bg, pady=6)
        top.pack(fill="x", padx=10)
        tk.Label(top, text="Auth file:", bg=bg, fg="#6c7086", font=("Segoe UI", 9)).pack(side="left")
        tk.Label(top, text=self.services.db.CODEX_AUTH_PATH, bg=bg, fg=fg, font=("Segoe UI", 9)).pack(side="left", padx=(6, 0))

        current_frame = tk.Frame(self.frame, bg=bg)
        current_frame.pack(fill="x", padx=10, pady=(0, 6))
        tk.Label(
            current_frame,
            text="Current Codex:",
            width=CODEX_CURRENT_LABEL_WIDTH,
            anchor="w",
            bg=bg,
            fg="#6c7086",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")
        self.current_value = tk.Label(current_frame, text="-", bg=bg, fg="#6c7086", font=("Segoe UI", 9))
        self.current_value.pack(side="left")

        cols = ("name", "accountId", "saved", "expires", "active", "status")
        self.tree = ttk.Treeview(self.frame, columns=cols, show="headings", height=8, selectmode="browse")
        self.tree.heading("name", text="Name")
        self.tree.heading("accountId", text="Account ID")
        self.tree.heading("saved", text="Saved")
        self.tree.heading("expires", text="Expires")
        self.tree.heading("active", text="Active")
        self.tree.heading("status", text="Status")
        self.tree.column("name", width=150, anchor="w")
        self.tree.column("accountId", width=180, anchor="w")
        self.tree.column("saved", width=120, anchor="center")
        self.tree.column("expires", width=100, anchor="center")
        self.tree.column("active", width=90, anchor="center")
        self.tree.column("status", width=90, anchor="center")
        self.tree.tag_configure(EXPIRED_ROW_TAG, foreground=EXPIRED_ROW_FG)
        self.tree.tag_configure(REFRESH_ERROR_ROW_TAG, foreground=REFRESH_ERROR_ROW_FG)
        self.tree.tag_configure(TERMINAL_REFRESH_ERROR_ROW_TAG, foreground=TERMINAL_REFRESH_ERROR_ROW_FG)
        self.tree.pack(padx=10, pady=(0, 6))
        self.context_menu = tk.Menu(self.tree, tearoff=False)
        self.context_menu.add_command(label="Use selected", command=self.on_use)
        self.context_menu.add_command(label="Rename", command=self.on_rename)
        self.tree.bind("<F2>", self.on_rename_key)
        self.tree.bind("<Button-3>", self.on_tree_context_menu)

        self.btn_frame = tk.Frame(self.frame, bg=bg)
        self.btn_frame.pack(padx=10, pady=(0, 6))
        tab_button(self.btn_frame, self.services, "💾 Save current Codex", self.on_save, accent=True)
        tab_button(self.btn_frame, self.services, "📥 Import Codex auth", self.on_import)
        tab_button(self.btn_frame, self.services, "▶ Use selected Codex", self.on_use)
        tab_button(self.btn_frame, self.services, "↻ Refresh selected", self.on_refresh_selected)
        tab_button(self.btn_frame, self.services, "✏ Rename", self.on_rename)
        tab_button(self.btn_frame, self.services, "🗑 Delete", self.on_delete)
        tab_button(self.btn_frame, self.services, "⟳ Refresh", self.on_refresh)

    def update_current_label(self, current_account):
        if current_account:
            self.current_value.config(text=shorten_account_id(current_account.get("accountId")), fg="#a6e3a1")
        else:
            self.current_value.config(text="-", fg="#6c7086")

    def refresh(self):
        db = self.services.db
        self.tree.delete(*self.tree.get_children())

        try:
            current_codex = db.read_current_codex_account().get(db.CODEX_KEY, {})
        except Exception:
            current_codex = {}
        self.update_current_label(current_codex)

        current_fp = current_codex.get("fingerprint")
        for record in db.list_saved_accounts("codex"):
            name = record["name"]
            data = record["data"]
            entries = data.get("entries", [])
            codex_entry = next((entry for entry in entries if entry.get("key") == db.CODEX_KEY), None)
            if not codex_entry:
                continue

            value = codex_entry.get("value", {})
            saved_at = format_saved_at(data)
            account_id = shorten_account_id(value.get("accountId"))
            expires_ms = value.get("expires") if isinstance(value.get("expires"), int) else 0
            expires = format_saved_expires(expires_ms)
            active = "active" if current_fp and db.account_fingerprint(value) == current_fp else "-"
            refresh_status = format_refresh_status(data.get("refresh_status"))
            self.tree.insert(
                "",
                "end",
                iid=name,
                values=(name, account_id, saved_at, expires, active, refresh_status),
                tags=account_row_tags(data, expires_ms),
            )

    def on_save(self):
        name = ask_account_name(self.services.root, "Save Codex account", "Account name:")
        if not name:
            return
        self.services.run_guarded(self.services.db.save_codex_account, name, success_msg=f"Saved Codex account '{name}'")

    def on_tree_context_menu(self, event):
        popup_tree_context_menu(self.tree, self.context_menu, event)
        return "break"

    def on_rename_key(self, _event=None):
        self.on_rename()
        return "break"

    def on_rename(self):
        name = selected_name(self.tree, "Select a Codex account first.")
        if not name:
            return
        new_name = ask_account_name(self.services.root, "Rename Codex account", "New account name:", initialvalue=name)
        if not new_name or new_name == name:
            return
        try:
            self.services.db.rename_saved_account(name, new_name, expected_kind="codex")
            self.services.refresh_all()
            select_tree_item(self.tree, new_name)
            self.services.set_status(f"Renamed '{name}' to '{new_name}'", True)
        except Exception as exc:
            self.services.set_status(str(exc), False)

    def on_import(self):
        path = filedialog.askopenfilename(
            title="Select auth.json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            initialdir=os.path.expanduser("~/.codex"),
            parent=self.services.root,
        )
        if not path:
            return
        name = ask_account_name(self.services.root, "Import Codex auth", "Account name:")
        if not name:
            return
        self.services.run_guarded(self.services.db.import_codex_account, path, name, success_msg=f"Imported Codex account '{name}'")

    def on_use(self):
        name = selected_name(self.tree, "Select a Codex account first.")
        if not name:
            return
        if not messagebox.askyesno("Switch Codex account", f"Apply Codex account '{name}' to auth.json?"):
            return
        self.services.run_guarded(self.services.db.use_codex_account, name, success_msg=f"Switched Codex to '{name}'")

    def on_delete(self):
        name = selected_name(self.tree, "Select a Codex account first.")
        if not name:
            return
        if not messagebox.askyesno("Delete", f"Delete saved account '{name}'?"):
            return
        try:
            delete_saved_account(self.services.db, name)
            self.services.set_status(f"Deleted '{name}'", True)
            self.services.refresh_all()
        except Exception as exc:
            self.services.set_status(str(exc), False)

    def on_refresh_selected(self):
        name = selected_name(self.tree, "Select a Codex account first.")
        if not name:
            return
        self.services.run_guarded(self.services.db.refresh_saved_account, name, log_prefix="manual-refresh")

    def on_refresh(self):
        self.services.refresh_all()
        self.services.set_status("Refreshed", True)
