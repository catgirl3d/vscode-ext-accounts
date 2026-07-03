import datetime
import os
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable, Sequence

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

from . import account_services
from . import saved_account_status


IDE_EXTENSION_ORDER = ("kilocode", "roo-cline", "kilo-new")
SUCCESS_GREEN = "#a6e3a1"
EXPIRED_ROW_TAG = "expired"
EXPIRED_ROW_FG = "#f38ba8"
REFRESH_OK_ROW_TAG = "refresh_ok"
REFRESH_ERROR_ROW_TAG = "refresh_error"
REFRESH_ERROR_ROW_FG = "#fab387"
TERMINAL_REFRESH_ERROR_ROW_TAG = "terminal_refresh_error"
TERMINAL_REFRESH_ERROR_ROW_FG = "#f38ba8"
SECTION_BG = "#181825"
RUNTIME_STATUS_VARIANTS = ("running !", "closed OK")
LAYOUT_WIDTH_PAD_PX = 4
IDE_ACCOUNT_IMPORT_HINT = (
    "Paste a JSON object or a one-item JSON array. Required fields: "
    "access_token, refresh_token, id_token. Optional: account_id and expires."
)
IDE_ACCOUNT_IMPORT_EXAMPLE = (
    "[\n"
    "  {\n"
    '    "access_token": "eyJ...",\n'
    '    "refresh_token": "rt.1....",\n'
    '    "id_token": "eyJ...",\n'
    '    "account_id": "acct-123",\n'
    '    "expires": 1767225600000\n'
    "  }\n"
    "]"
)


def current_time_ms():
    return int(datetime.datetime.now().timestamp() * 1000)


def max_text_width_px(font_spec: tuple[str, ...], texts: Sequence[str]) -> int:
    font = tkfont.Font(font=font_spec)
    return max((font.measure(text) for text in texts), default=0)


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


@dataclass(frozen=True)
class SavedAccountTreeColumn:
    name: str
    heading: str
    width: int
    anchor: str = "center"


@dataclass(frozen=True)
class SavedAccountActionButton:
    text: str
    handler_name: str
    accent: bool = False
    separator_before: bool = False
    attr_name: str | None = None
    hide_after_create: bool = False


@dataclass(frozen=True)
class SavedAccountTreeRow:
    iid: str
    values: tuple[Any, ...]
    tags: tuple[str, ...]


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
    expire_tags = expires_row_tags(expires_ms, now_ms=now_ms)
    if expire_tags:
        return expire_tags
    if status == saved_account_status.REFRESH_STATUS_OK:
        return (REFRESH_OK_ROW_TAG,)
    return ()


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
    return account_services.first_expires_ms(entries, skip_keys=skip_keys)


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


def clipboard_text_or_empty(root: tk.Tk) -> str:
    try:
        value = root.clipboard_get()
    except tk.TclError:
        return ""
    return value if isinstance(value, str) else str(value)


class IdeAccountImportDialog:
    def __init__(self, services: GuiServices):
        self.services = services
        self.result: tuple[str, str] | None = None
        self.window = tk.Toplevel(services.root)
        self.window.title("Import IDE Account")
        self.window.transient(services.root)
        self.window.configure(bg=services.bg)
        self.window.resizable(True, True)
        self.window.minsize(720, 560)
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)
        self.window.bind("<Escape>", self.cancel)

        content = tk.Frame(self.window, bg=services.bg, padx=12, pady=12)
        content.pack(fill="both", expand=True)

        tk.Label(content, text="Account name:", bg=services.bg, fg="#6c7086", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.name_entry = tk.Entry(
            content,
            bg=services.btn_bg,
            fg=services.fg,
            insertbackground=services.fg,
            relief="flat",
            font=("Segoe UI", 10),
        )
        self.name_entry.pack(fill="x", pady=(4, 10))

        tk.Label(content, text="Expected format:", bg=services.bg, fg="#6c7086", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        tk.Label(
            content,
            text=IDE_ACCOUNT_IMPORT_HINT,
            bg=services.bg,
            fg=services.fg,
            justify="left",
            wraplength=680,
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(4, 8))

        self.example_text = tk.Text(
            content,
            height=8,
            bg=services.btn_bg,
            fg=services.fg,
            insertbackground=services.fg,
            relief="flat",
            wrap="none",
            font=("Consolas", 9),
        )
        self.example_text.insert("1.0", IDE_ACCOUNT_IMPORT_EXAMPLE)
        self.example_text.configure(state="disabled")
        self.example_text.pack(fill="x", pady=(0, 10))

        payload_header = tk.Frame(content, bg=services.bg)
        payload_header.pack(fill="x")
        tk.Label(payload_header, text="Paste JSON:", bg=services.bg, fg="#6c7086", font=("Segoe UI", 9, "bold")).pack(side="left")
        tk.Button(
            payload_header,
            text="Paste",
            command=self.paste_from_clipboard,
            bg=services.btn_bg,
            fg=services.fg,
            activebackground=services.btn_act,
            activeforeground=services.fg,
            relief="flat",
            padx=10,
            pady=4,
            font=("Segoe UI", 9),
            cursor="hand2",
        ).pack(side="right")
        self.payload_text = scrolledtext.ScrolledText(
            content,
            height=14,
            bg=services.btn_bg,
            fg=services.fg,
            insertbackground=services.fg,
            relief="flat",
            wrap="word",
            font=("Consolas", 10),
        )
        self.payload_text.pack(fill="both", expand=True, pady=(4, 10))

        button_row = tk.Frame(content, bg=services.bg)
        button_row.pack(fill="x")
        tk.Button(
            button_row,
            text="Import",
            command=self.submit,
            bg="#89b4fa",
            fg=services.sel_fg,
            activebackground=services.btn_act,
            activeforeground=services.fg,
            relief="flat",
            padx=12,
            pady=6,
            font=("Segoe UI", 10),
            cursor="hand2",
        ).pack(side="right", padx=(8, 0))
        tk.Button(
            button_row,
            text="Cancel",
            command=self.cancel,
            bg=services.btn_bg,
            fg=services.fg,
            activebackground=services.btn_act,
            activeforeground=services.fg,
            relief="flat",
            padx=12,
            pady=6,
            font=("Segoe UI", 10),
            cursor="hand2",
        ).pack(side="right")

        self.name_entry.focus_set()

    def show(self) -> tuple[str, str] | None:
        self.window.grab_set()
        self.window.wait_window()
        return self.result

    def paste_from_clipboard(self):
        payload = clipboard_text_or_empty(self.services.root)
        if not payload:
            messagebox.showwarning("Import IDE Account", "Clipboard does not contain text.")
            self.payload_text.focus_set()
            return
        self.payload_text.delete("1.0", "end")
        self.payload_text.insert("1.0", payload)
        self.payload_text.focus_set()

    def submit(self, _event=None):
        name = self.name_entry.get().strip()
        payload = self.payload_text.get("1.0", "end-1c").strip()
        if not name:
            messagebox.showwarning("Import IDE Account", "Enter an account name.")
            self.name_entry.focus_set()
            return
        if not payload:
            messagebox.showwarning("Import IDE Account", "Paste the account JSON to import.")
            self.payload_text.focus_set()
            return
        self.result = (name, payload)
        self.window.destroy()

    def cancel(self, _event=None):
        self.result = None
        self.window.destroy()


def ask_ide_account_import(services: GuiServices) -> tuple[str, str] | None:
    return IdeAccountImportDialog(services).show()


def delete_saved_account(db_module, name, *, expected_kind: str | None = None):
    if expected_kind:
        db_module.delete_saved_account(name, expected_kind=expected_kind)
        return
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


def section_card(parent, services: GuiServices, *, padx=8, pady=6):
    return tk.Frame(
        parent,
        bg=SECTION_BG,
        padx=padx,
        pady=pady,
        highlightbackground=services.btn_act,
        highlightthickness=1,
    )


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


def tab_button_separator(parent, services: GuiServices):
    separator = tk.Frame(parent, bg=services.bg, padx=4)
    separator.pack(side="left", fill="y", padx=2)
    tk.Frame(separator, bg=services.btn_bg, width=1).pack(fill="y", pady=4)
    return separator


class SavedAccountsTreeTab:
    expected_kind: str | None = None
    selection_empty_message: str | None = None
    rename_dialog_title: str | None = None

    def _require_saved_account_config_value(self, attr_name: str) -> str:
        value = getattr(self, attr_name, None)
        if isinstance(value, str) and value:
            return value
        raise RuntimeError(f"{type(self).__name__} must define non-empty {attr_name}.")

    def _configure_saved_account_tree(self):
        self._require_saved_account_config_value("expected_kind")
        self._require_saved_account_config_value("selection_empty_message")
        self._require_saved_account_config_value("rename_dialog_title")
        if not hasattr(self, "tree"):
            raise RuntimeError(f"{type(self).__name__} must define self.tree before calling _configure_saved_account_tree().")
        if not hasattr(self, "services"):
            raise RuntimeError(f"{type(self).__name__} must define self.services before calling _configure_saved_account_tree().")
        if not callable(getattr(self, "on_use", None)):
            raise RuntimeError(f"{type(self).__name__} must define on_use() before calling _configure_saved_account_tree().")

        self.tree.tag_configure(REFRESH_OK_ROW_TAG, foreground=SUCCESS_GREEN)
        self.tree.tag_configure(EXPIRED_ROW_TAG, foreground=EXPIRED_ROW_FG)
        self.tree.tag_configure(REFRESH_ERROR_ROW_TAG, foreground=REFRESH_ERROR_ROW_FG)
        self.tree.tag_configure(TERMINAL_REFRESH_ERROR_ROW_TAG, foreground=TERMINAL_REFRESH_ERROR_ROW_FG)
        self.context_menu = tk.Menu(self.tree, tearoff=False)
        self.context_menu.add_command(label="Use selected", command=self.on_use)
        self.context_menu.add_command(label="Rename", command=self.on_rename)
        self.tree.bind("<F2>", self.on_rename_key)
        self.tree.bind("<Button-3>", self.on_tree_context_menu)

    def _build_saved_account_tree(self, columns: Sequence[SavedAccountTreeColumn], *, height: int = 8):
        column_names = tuple(column.name for column in columns)
        self.tree = ttk.Treeview(self.frame, columns=column_names, show="headings", height=height, selectmode="browse")
        for column in columns:
            self.tree.heading(column.name, text=column.heading, anchor="center")
            self.tree.column(column.name, width=column.width, anchor=column.anchor)
        self._configure_saved_account_tree()
        self.tree.pack(fill="both", expand=True, padx=10, pady=(0, 6))

    def _build_saved_account_actions(self, buttons: Sequence[SavedAccountActionButton]):
        self.btn_frame = tk.Frame(self.frame, bg=self.services.bg)
        self.btn_frame.pack(padx=10, pady=(0, 6))
        for button_spec in buttons:
            if button_spec.separator_before:
                tab_button_separator(self.btn_frame, self.services)
            button = tab_button(
                self.btn_frame,
                self.services,
                button_spec.text,
                getattr(self, button_spec.handler_name),
                accent=button_spec.accent,
            )
            if button_spec.attr_name:
                setattr(self, button_spec.attr_name, button)
            if button_spec.hide_after_create:
                button.pack_forget()

    def _present_saved_account_tree(
        self,
        kind: str,
        *,
        load_current_state: Callable[[], Any],
        update_current_state: Callable[[Any], None],
        build_row: Callable[[dict, Any], SavedAccountTreeRow | None],
        after_present: Callable[[Any], None] | None = None,
    ):
        previous_selection = self.tree.selection()
        previous_iid = previous_selection[0] if previous_selection else None
        self.tree.delete(*self.tree.get_children())
        current_state = load_current_state()
        update_current_state(current_state)
        for record in self.services.db.list_saved_accounts(kind):
            row = build_row(record, current_state)
            if row is None:
                continue
            self.tree.insert("", "end", iid=row.iid, values=row.values, tags=row.tags)
        if after_present:
            after_present(current_state)
        if previous_iid and self.tree.exists(previous_iid):
            self.tree.selection_set(previous_iid)
            self.tree.focus(previous_iid)
            self.tree.see(previous_iid)

    def selected_saved_account_name(self):
        return selected_name(self.tree, self._require_saved_account_config_value("selection_empty_message"))

    def on_tree_context_menu(self, event):
        popup_tree_context_menu(self.tree, self.context_menu, event)
        return "break"

    def on_rename_key(self, _event=None):
        self.on_rename()
        return "break"

    def on_rename(self):
        name = self.selected_saved_account_name()
        if not name:
            return
        expected_kind = self._require_saved_account_config_value("expected_kind")
        rename_dialog_title = self._require_saved_account_config_value("rename_dialog_title")
        new_name = ask_account_name(self.services.root, rename_dialog_title, "New account name:", initialvalue=name)
        if not new_name or new_name == name:
            return
        try:
            self.services.db.rename_saved_account(name, new_name, expected_kind=expected_kind)
            self.services.refresh_all()
            select_tree_item(self.tree, new_name)
            self.services.set_status(f"Renamed '{name}' to '{new_name}'", True)
        except Exception as exc:
            self.services.set_status(str(exc), False)

    def on_delete(self):
        name = self.selected_saved_account_name()
        if not name:
            return
        if not messagebox.askyesno("Delete", f"Delete saved account '{name}'?"):
            return
        try:
            delete_saved_account(
                self.services.db,
                name,
                expected_kind=self._require_saved_account_config_value("expected_kind"),
            )
            self.services.set_status(f"Deleted '{name}'", True)
            self.services.refresh_all()
        except Exception as exc:
            self.services.set_status(str(exc), False)

    def on_refresh_selected(self):
        name = self.selected_saved_account_name()
        if not name:
            return
        refresh_selected_account = partial(
            self.services.db.refresh_saved_account,
            name,
            expected_kind=self._require_saved_account_config_value("expected_kind"),
        )
        self.services.run_guarded(refresh_selected_account, log_prefix="manual-refresh")

    def on_refresh(self):
        self.services.refresh_all()
        self.services.set_status("Reloaded view", True)


class IdeAccountsTab(SavedAccountsTreeTab):
    expected_kind = "ide"
    selection_empty_message = "Select an IDE account first."
    rename_dialog_title = "Rename IDE account"

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
        section_label_font = ("Segoe UI", 9, "bold")
        runtime_status_width = max_text_width_px(
            section_label_font,
            [
                f"{str(cfg.get('label', ide))}: {status}"
                for ide, cfg in db.IDE_PATHS.items()
                for status in RUNTIME_STATUS_VARIANTS
            ],
        ) + 12 + LAYOUT_WIDTH_PAD_PX
        current_ide_label_width = max_text_width_px(
            section_label_font,
            [f"Current in {str(cfg.get('label', ide))}:" for ide, cfg in db.IDE_PATHS.items()],
        ) + LAYOUT_WIDTH_PAD_PX

        header = tk.Frame(self.frame, bg=bg, pady=3)
        header.pack(fill="x", padx=10)
        header.grid_columnconfigure(0, weight=0)
        header.grid_columnconfigure(1, weight=0)
        header.grid_columnconfigure(2, weight=1)

        ide_card = section_card(header, self.services)
        ide_card.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        tk.Label(ide_card, text="Target IDE", bg=SECTION_BG, fg="#6c7086", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        ide_top = tk.Frame(ide_card, bg=SECTION_BG)
        ide_top.pack(fill="x", pady=(4, 0))
        for value, label in [("vscode", "VSCode"), ("antigravity", "Antigravity")]:
            tk.Radiobutton(
                ide_top,
                text=label,
                variable=self.ide_var,
                value=value,
                command=self.on_ide_change,
                bg=SECTION_BG,
                fg=fg,
                selectcolor=self.services.btn_bg,
                activebackground=SECTION_BG,
                activeforeground=fg,
                font=("Segoe UI", 9, "bold"),
            ).pack(side="left", padx=(0, 10))

        runtime_card = section_card(header, self.services)
        runtime_card.grid(row=0, column=1, sticky="nsew", padx=4)
        runtime_card.grid_columnconfigure(0, minsize=runtime_status_width)
        tk.Label(runtime_card, text="Runtime status", bg=SECTION_BG, fg="#6c7086", font=section_label_font).grid(row=0, column=0, sticky="w")
        self.ide_state_label = tk.Label(
            runtime_card,
            text="",
            anchor="w",
            bg=self.services.btn_bg,
            fg=fg,
            font=section_label_font,
            padx=6,
            pady=2,
        )
        self.ide_state_label.grid(row=1, column=0, sticky="w", pady=(4, 0))

        ext_card = section_card(header, self.services)
        ext_card.grid(row=0, column=2, sticky="nsew", padx=(4, 0))
        tk.Label(ext_card, text="Extensions", bg=SECTION_BG, fg="#6c7086", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        ide_ext_frame = tk.Frame(ext_card, bg=SECTION_BG)
        ide_ext_frame.pack(fill="x", pady=(4, 0))
        for value, label in [("kilocode", "Kilocode"), ("roo-cline", "Roo-Cline"), ("kilo-new", "Kilo New")]:
            tk.Checkbutton(
                ide_ext_frame,
                text=label,
                variable=self.ide_ext_vars[value],
                bg=SECTION_BG,
                fg=fg,
                selectcolor=self.services.btn_bg,
                activebackground=SECTION_BG,
                activeforeground=fg,
                font=("Segoe UI", 9),
            ).pack(side="left", padx=(0, 10))

        current_ide_frame = section_card(header, self.services)
        current_ide_frame.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        current_ide_frame.grid_columnconfigure(0, minsize=current_ide_label_width)
        current_ide_frame.grid_columnconfigure(1, weight=1)

        self.current_ide_label = tk.Label(
            current_ide_frame,
            text="Current in VSCode:",
            anchor="w",
            bg=SECTION_BG,
            fg="#6c7086",
            font=section_label_font,
        )
        self.current_ide_label.grid(row=0, column=0, sticky="w")

        current_values_frame = tk.Frame(current_ide_frame, bg=SECTION_BG)
        current_values_frame.grid(row=0, column=1, sticky="ew", padx=(10, 0))

        for ext_name in IDE_EXTENSION_ORDER:
            ext_id = db.IDE_EXTENSIONS[ext_name]
            label = tk.Label(
                current_values_frame,
                text="",
                bg=self.services.btn_bg,
                fg="#6c7086",
                font=("Segoe UI", 9),
                padx=6,
                pady=2,
            )
            label.pack(side="left", padx=(0, 6))
            self.current_ide_labels[ext_id] = label

        self._build_saved_account_tree(
            (
                SavedAccountTreeColumn("name", "Name", 225, anchor="w"),
                SavedAccountTreeColumn("ext", "Ext", 75),
                SavedAccountTreeColumn("accountIds", "Account IDs", 100),
                SavedAccountTreeColumn("saved", "Saved", 112),
                SavedAccountTreeColumn("expires", "Expires", 80),
                SavedAccountTreeColumn("active", "Active", 60),
                SavedAccountTreeColumn("status", "Status", 60),
            )
        )

        self._build_saved_account_actions(
            (
                SavedAccountActionButton("▶ Use selected", "on_use", accent=True),
                SavedAccountActionButton("💾 Save current", "on_save"),
                SavedAccountActionButton("📥 Import account", "on_import_clipboard"),
                SavedAccountActionButton("↻ Renew tokens", "on_refresh_selected", separator_before=True),
                SavedAccountActionButton("✏ Rename", "on_rename"),
                SavedAccountActionButton("🗑 Delete", "on_delete"),
                SavedAccountActionButton("⟳ Reload", "on_refresh", separator_before=True),
                SavedAccountActionButton("RUN", "on_run", attr_name="run_button", hide_after_create=True),
                SavedAccountActionButton("📦 Full backup", "on_backup", attr_name="backup_button"),
            )
        )

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
                widget.config(text=f"{short}: {shorten_account_id(info.get('accountId'))}", fg=SUCCESS_GREEN)
            else:
                widget.config(text=f"{short}: -", fg="#6c7086")

    def refresh_runtime_state(self, force: bool = False) -> bool:
        current_ide = self.ide_var.get()
        running = self.services.db.is_ide_running(current_ide)
        runtime_state = (current_ide, running)
        if not force and runtime_state == self._last_runtime_state:
            return False

        ide_cfg = self.services.db.IDE_PATHS[current_ide]
        state_str = "running !" if running else "closed OK"
        state_color = "#c0392b" if running else SUCCESS_GREEN
        self.ide_state_label.config(text=f"{ide_cfg['label']}: {state_str}", fg=state_color)
        self.update_run_button_visibility(running)
        self._last_runtime_state = runtime_state
        return True

    def _load_ide_refresh_state(self):
        db = self.services.db
        accounts_per_ide = {}
        for ide in db.IDE_PATHS:
            try:
                accounts_per_ide[ide] = db.read_current_accounts_for_ide(ide)
            except Exception:
                accounts_per_ide[ide] = {}
        try:
            kilo_new_fp = db.get_kilo_new_fingerprint()
        except Exception:
            kilo_new_fp = None
        return {
            "accounts_per_ide": accounts_per_ide,
            "current_accounts": accounts_per_ide.get(self.ide_var.get(), {}),
            "kilo_new_fingerprint": kilo_new_fp,
        }

    def _update_ide_refresh_state(self, refresh_state):
        self.update_current_labels(refresh_state["current_accounts"])

    def _ide_active_tags(self, ide_entries, refresh_state) -> list[str]:
        db = self.services.db
        active_tags = []
        ide_short = {"vscode": "VS", "antigravity": "AG"}
        for ide, current_accounts in refresh_state["accounts_per_ide"].items():
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

        kilo_new_fp = refresh_state["kilo_new_fingerprint"]
        if kilo_new_fp:
            for entry in ide_entries:
                if db.account_fingerprint(entry.get("value", {})) == kilo_new_fp:
                    if "KN" not in active_tags:
                        active_tags.append("KN")
                    break
        return active_tags

    def _build_ide_saved_account_row(self, record: dict, refresh_state) -> SavedAccountTreeRow:
        name = record["name"]
        data = record["data"]
        entries = data.get("entries", [])
        ide_entries = [entry for entry in entries if entry.get("key") != self.services.db.CODEX_KEY]

        saved_at = format_saved_at(data)
        ext_tag = data.get("ext", "both")
        expires_ms = first_expires_ms(ide_entries)
        expires = format_saved_expires(expires_ms)
        accounts_short = summarize_account_ids(ide_entries)
        refresh_status = format_refresh_status(data.get("refresh_status"))
        active_tags = self._ide_active_tags(ide_entries, refresh_state)
        active = "+".join(active_tags) if active_tags else "-"
        return SavedAccountTreeRow(
            iid=name,
            values=(name, ext_tag, accounts_short, saved_at, expires, active, refresh_status),
            tags=account_row_tags(data, expires_ms),
        )

    def _after_ide_refresh(self, _refresh_state):
        self.refresh_runtime_state(force=True)

    def refresh(self):
        self._present_saved_account_tree(
            "ide",
            load_current_state=self._load_ide_refresh_state,
            update_current_state=self._update_ide_refresh_state,
            build_row=self._build_ide_saved_account_row,
            after_present=self._after_ide_refresh,
        )

    def on_ide_change(self):
        self.services.db.set_ide(self.ide_var.get())
        self.refresh()

    def on_save(self):
        name = ask_account_name(self.services.root, "Save IDE account", "Account name:")
        if not name:
            return
        exts = self.selected_exts()
        if not exts:
            return
        label = self.format_ext_selection(exts)
        self.services.run_guarded(self.services.db.save_ide_account, name, exts, success_msg=f"Saved '{name}' [{label}]")

    def on_import_clipboard(self):
        exts = self.selected_exts()
        if not exts:
            return

        import_data = ask_ide_account_import(self.services)
        if not import_data:
            return
        name, json_text = import_data

        label = self.format_ext_selection(exts)
        self.services.run_guarded(
            self.services.db.import_ide_account_from_json_string,
            json_text,
            name,
            exts,
            success_msg=f"Imported '{name}' [{label}]",
        )

    def on_use(self):
        name = self.selected_saved_account_name()
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


class CodexTab(SavedAccountsTreeTab):
    expected_kind = "codex"
    selection_empty_message = "Select a Codex account first."
    rename_dialog_title = "Rename Codex account"

    def __init__(self, notebook: ttk.Notebook, services: GuiServices):
        self.services = services
        self.frame = tk.Frame(notebook, bg=services.bg)
        notebook.add(self.frame, text="Codex")
        self._build()

    def _build(self):
        bg = self.services.bg
        fg = self.services.fg
        section_label_font = ("Segoe UI", 9, "bold")
        current_value_width = max_text_width_px(("Segoe UI", 9), ["-", shorten_account_id("12345678901234567890")]) + 20

        header = tk.Frame(self.frame, bg=bg, pady=3)
        header.pack(fill="x", padx=10)
        header.grid_columnconfigure(0, weight=0)
        header.grid_columnconfigure(1, weight=1)

        current_card = section_card(header, self.services)
        current_card.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        current_card.grid_columnconfigure(0, minsize=current_value_width)
        tk.Label(current_card, text="Current Codex", bg=SECTION_BG, fg="#6c7086", font=section_label_font).grid(row=0, column=0, sticky="w")
        self.current_value = tk.Label(
            current_card,
            text="-",
            bg=self.services.btn_bg,
            fg="#6c7086",
            font=("Segoe UI", 9),
            anchor="w",
            padx=6,
            pady=2,
        )
        self.current_value.grid(row=1, column=0, sticky="w", pady=(4, 0))

        auth_card = section_card(header, self.services)
        auth_card.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        tk.Label(auth_card, text="Auth file", bg=SECTION_BG, fg="#6c7086", font=section_label_font).pack(anchor="w")
        tk.Label(
            auth_card,
            text=self.services.db.CODEX_AUTH_PATH,
            bg=SECTION_BG,
            fg=fg,
            justify="left",
            anchor="w",
            wraplength=560,
            font=("Segoe UI", 9),
        ).pack(fill="x", pady=(4, 0))

        self._build_saved_account_tree(
            (
                SavedAccountTreeColumn("name", "Name", 150, anchor="w"),
                SavedAccountTreeColumn("accountId", "Account ID", 180),
                SavedAccountTreeColumn("saved", "Saved", 120),
                SavedAccountTreeColumn("expires", "Expires", 100),
                SavedAccountTreeColumn("active", "Active", 90),
                SavedAccountTreeColumn("status", "Status", 90),
            )
        )

        self._build_saved_account_actions(
            (
                SavedAccountActionButton("▶ Use selected Codex", "on_use", accent=True),
                SavedAccountActionButton("💾 Save current Codex", "on_save"),
                SavedAccountActionButton("📥 Import Codex auth", "on_import"),
                SavedAccountActionButton("↻ Renew tokens", "on_refresh_selected", separator_before=True),
                SavedAccountActionButton("✏ Rename", "on_rename"),
                SavedAccountActionButton("🗑 Delete", "on_delete"),
                SavedAccountActionButton("⟳ Reload", "on_refresh", separator_before=True),
            )
        )

    def update_current_label(self, current_account):
        if current_account:
            self.current_value.config(text=shorten_account_id(current_account.get("accountId")), fg=SUCCESS_GREEN)
        else:
            self.current_value.config(text="-", fg="#6c7086")

    def _load_codex_refresh_state(self):
        db = self.services.db
        try:
            current_codex = db.read_current_codex_account().get(db.CODEX_KEY, {})
        except Exception:
            current_codex = {}
        return {
            "current_codex": current_codex,
            "current_fingerprint": current_codex.get("fingerprint"),
        }

    def _update_codex_refresh_state(self, refresh_state):
        self.update_current_label(refresh_state["current_codex"])

    def _build_codex_saved_account_row(self, record: dict, refresh_state) -> SavedAccountTreeRow | None:
        name = record["name"]
        data = record["data"]
        entries = data.get("entries", [])
        codex_entry = next((entry for entry in entries if entry.get("key") == self.services.db.CODEX_KEY), None)
        if not codex_entry:
            return None

        value = codex_entry.get("value", {})
        saved_at = format_saved_at(data)
        account_id = shorten_account_id(value.get("accountId"))
        expires_ms = value.get("expires") if isinstance(value.get("expires"), int) else 0
        expires = format_saved_expires(expires_ms)
        current_fp = refresh_state["current_fingerprint"]
        active = "active" if current_fp and self.services.db.account_fingerprint(value) == current_fp else "-"
        refresh_status = format_refresh_status(data.get("refresh_status"))
        return SavedAccountTreeRow(
            iid=name,
            values=(name, account_id, saved_at, expires, active, refresh_status),
            tags=account_row_tags(data, expires_ms),
        )

    def refresh(self):
        self._present_saved_account_tree(
            "codex",
            load_current_state=self._load_codex_refresh_state,
            update_current_state=self._update_codex_refresh_state,
            build_row=self._build_codex_saved_account_row,
        )

    def on_save(self):
        name = ask_account_name(self.services.root, "Save Codex account", "Account name:")
        if not name:
            return
        self.services.run_guarded(self.services.db.save_codex_account, name, success_msg=f"Saved Codex account '{name}'")

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
        name = self.selected_saved_account_name()
        if not name:
            return
        if not messagebox.askyesno("Switch Codex account", f"Apply Codex account '{name}' to auth.json?"):
            return
        self.services.run_guarded(self.services.db.use_codex_account, name, success_msg=f"Switched Codex to '{name}'")
