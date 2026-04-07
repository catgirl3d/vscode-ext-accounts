"""
Minimal GUI for vscode_inject account manager.
Run: python gui.py
"""

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
import datetime
import json
import os
import sys
import threading

# Import logic from parse_vscdb
sys.path.insert(0, os.path.dirname(__file__))
import parse_vscdb as db


# ── Helpers ───────────────────────────────────────────────────────────────────

def run_guarded(fn, *args, success_msg=None):
    """Run fn in thread, show result in status bar."""
    def _run():
        try:
            fn(*args)
            if success_msg:
                set_status(success_msg, ok=True)
        except SystemExit as e:
            set_status(f"Aborted (code {e.code})", ok=False)
        except Exception as e:
            set_status(str(e), ok=False)
        finally:
            refresh_accounts()

    threading.Thread(target=_run, daemon=True).start()


def set_status(msg, ok=True):
    color = "#2d8a4e" if ok else "#c0392b"
    status_var.set(msg)
    status_label.config(fg=color)


# ── Account list ──────────────────────────────────────────────────────────────

def refresh_accounts():
    tree.delete(*tree.get_children())

    # Read active accounts from both IDEs independently
    accounts_per_ide = {}
    for ide in db.IDE_PATHS:
        try:
            accounts_per_ide[ide] = db.read_current_accounts_for_ide(ide)
        except Exception:
            accounts_per_ide[ide] = {}

    current_ide = ide_var.get()
    _update_current_labels(accounts_per_ide.get(current_ide, {}))

    d = db._accounts_dir()
    files = sorted(f for f in os.listdir(d) if f.endswith(".json"))
    for f in files:
        name = f[:-5]
        try:
            with open(os.path.join(d, f), encoding="utf-8") as fh:
                data = json.load(fh)
            saved_at = data.get("saved_at", "")[:16].replace("T", " ")
            ext_tag = data.get("ext", "both")
            exp_str = ""
            account_ids = []
            for e in data.get("entries", []):
                v = e.get("value", {})
                if isinstance(v, dict):
                    if "expires" in v:
                        exp_dt = datetime.datetime.fromtimestamp(v["expires"] / 1000)
                        exp_str = exp_dt.strftime("%Y-%m-%d")
                    if "accountId" in v:
                        account_ids.append(v["accountId"][:8] + "…")
            accounts_short = ", ".join(account_ids) if account_ids else "?"

            # Check which IDEs / storages have this account active
            active_tags = []
            ide_short = {"vscode": "VS", "antigravity": "AG"}
            for ide, cur_acc in accounts_per_ide.items():
                hits = db.match_saved_to_current(data.get("entries", []), cur_acc)
                if hits:
                    active_tags.append(ide_short.get(ide, ide))
            # Check kilo-new (auth.json)
            kn_fp = db.get_kilo_new_fingerprint()
            if kn_fp:
                for e in data.get("entries", []):
                    if db.account_fingerprint(e.get("value", {})) == kn_fp:
                        active_tags.append("KN")
                        break
            active_str = "+".join(active_tags) if active_tags else "—"

            tree.insert("", "end", iid=name,
                        values=(name, ext_tag, accounts_short, saved_at, exp_str, active_str))
        except Exception:
            tree.insert("", "end", iid=name,
                        values=(name, "?", "?", "?", "?", "?"))

    ide_cfg = db.IDE_PATHS[current_ide]
    running = db.is_ide_running(current_ide)
    state_str = "running ⚠" if running else "closed ✓"
    state_color = "#c0392b" if running else "#2d8a4e"
    vscode_label.config(text=f"{ide_cfg['label']}: {state_str}", fg=state_color)


def _update_current_labels(current_accounts: dict):
    """Update the 'Current: …' labels below the top bar."""
    ide_label_text = db.IDE_PATHS[ide_var.get()]["label"]
    current_ide_label.config(text=f"Current in {ide_label_text}:")
    for ext_id, widget in _current_labels.items():
        info = current_accounts.get(ext_id)
        if info:
            aid = info.get("accountId", "?")
            if isinstance(aid, str) and len(aid) > 12:
                aid = aid[:12] + "…"
            exp = info.get("expires")
            exp_str = ""
            if exp:
                try:
                    exp_dt = datetime.datetime.fromtimestamp(exp / 1000)
                    exp_str = f"  exp {exp_dt.strftime('%Y-%m-%d')}"
                except Exception:
                    pass
            short = db._EXT_DISPLAY.get(ext_id, ext_id)
            widget.config(text=f"  {short}: {aid}{exp_str}", fg="#a6e3a1")
        else:
            short = db._EXT_DISPLAY.get(ext_id, ext_id)
            widget.config(text=f"  {short}: —", fg="#6c7086")


def selected_name():
    sel = tree.selection()
    if not sel:
        messagebox.showwarning("No selection", "Select an account first.")
        return None
    return sel[0]


# ── Actions ───────────────────────────────────────────────────────────────────

def selected_ext():
    return ext_var.get() if ext_var.get() != "both" else None


def on_save():
    name = simpledialog.askstring("Save account", "Account name:", parent=root)
    if not name:
        return
    name = name.strip().replace(" ", "_")
    ext = selected_ext()
    run_guarded(db.save_account, name, ext, success_msg=f"Saved '{name}' [{ext or 'both'}]")


def on_use():
    name = selected_name()
    if not name:
        return
    ext = ext_var.get() if ext_var.get() != "both" else None

    # kilo-new lives inside Antigravity — always check Antigravity process
    if ext_var.get() == "kilo-new":
        if db.is_ide_running("antigravity"):
            messagebox.showerror("Antigravity is running", "Close Antigravity before switching Kilo New account.")
            return
        if not messagebox.askyesno("Switch account", f"Switch '{name}' [kilo-new]?\nAntigravity must stay closed until done."):
            return
        run_guarded(db.use_account, name, "kilo-new", success_msg=f"Switched '{name}' [kilo-new]. Start Antigravity.")
        return

    current_ide = ide_var.get()
    ide_label = db.IDE_PATHS[current_ide]["label"]
    if db.is_ide_running(current_ide):
        messagebox.showerror(f"{ide_label} is running", f"Close {ide_label} before switching accounts.")
        return
    label = f"[{ext}]" if ext else "[both]"
    if not messagebox.askyesno("Switch account", f"Switch to '{name}' {label}?\n{ide_label} must stay closed until done."):
        return
    run_guarded(db.use_account, name, ext, success_msg=f"Switched to '{name}' {label}. Start {ide_label}.")


def on_delete():
    name = selected_name()
    if not name:
        return
    if not messagebox.askyesno("Delete", f"Delete saved account '{name}'?"):
        return
    path = os.path.join(db._accounts_dir(), f"{name}.json")
    try:
        os.remove(path)
        set_status(f"Deleted '{name}'", ok=True)
        refresh_accounts()
    except Exception as e:
        set_status(str(e), ok=False)


def on_import_codex():
    path = filedialog.askopenfilename(
        title="Select auth.json",
        filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        initialdir=os.path.expanduser("~/.codex"),
    )
    if not path:
        return
    name = simpledialog.askstring("Import Codex auth", "Account name:", parent=root)
    if not name:
        return
    name = name.strip().replace(" ", "_")
    ext = selected_ext()
    run_guarded(db.import_codex_auth, path, name, ext,
                success_msg=f"Imported '{name}' [{ext or 'both'}]")


def on_backup():
    run_guarded(db.backup, success_msg="Full backup saved.")


def on_refresh():
    refresh_accounts()
    set_status("Refreshed", ok=True)


# ── Build UI ──────────────────────────────────────────────────────────────────

root = tk.Tk()
root.title("VSCode Account Manager")
root.resizable(False, False)

BG = "#1e1e2e"
FG = "#cdd6f4"
BTN_BG = "#313244"
BTN_ACT = "#45475a"
SEL_BG = "#89b4fa"
SEL_FG = "#1e1e2e"

root.configure(bg=BG)

style = ttk.Style()
style.theme_use("clam")
style.configure("Treeview",
    background="#181825", fieldbackground="#181825",
    foreground=FG, rowheight=26, font=("Segoe UI", 10))
style.configure("Treeview.Heading",
    background=BTN_BG, foreground=FG, font=("Segoe UI", 9, "bold"))
style.map("Treeview", background=[("selected", SEL_BG)], foreground=[("selected", SEL_FG)])

# Top bar
top = tk.Frame(root, bg=BG, pady=6)
top.pack(fill="x", padx=10)

ide_var = tk.StringVar(value="vscode")

def on_ide_change():
    db.set_ide(ide_var.get())
    refresh_accounts()

tk.Label(top, text="IDE:", bg=BG, fg="#6c7086", font=("Segoe UI", 9)).pack(side="left")
for val, label in [("vscode", "VSCode"), ("antigravity", "Antigravity")]:
    tk.Radiobutton(top, text=label, variable=ide_var, value=val, command=on_ide_change,
                   bg=BG, fg=FG, selectcolor=BTN_BG, activebackground=BG,
                   activeforeground=FG, font=("Segoe UI", 9, "bold")).pack(side="left", padx=4)

tk.Label(top, text="  ", bg=BG).pack(side="left")

vscode_label = tk.Label(top, text="", bg=BG, fg=FG, font=("Segoe UI", 10, "bold"))
vscode_label.pack(side="left", padx=(0, 20))

tk.Label(top, text="Extension:", bg=BG, fg="#6c7086", font=("Segoe UI", 9)).pack(side="left")
ext_var = tk.StringVar(value="both")
for val, label in [("both", "Both"), ("kilocode", "Kilocode"), ("roo-cline", "Roo-Cline"), ("kilo-new", "Kilo New")]:
    tk.Radiobutton(top, text=label, variable=ext_var, value=val,
                   bg=BG, fg=FG, selectcolor=BTN_BG, activebackground=BG,
                   activeforeground=FG, font=("Segoe UI", 9)).pack(side="left", padx=4)

# Current accounts display (below top bar)
current_frame = tk.Frame(root, bg=BG)
current_frame.pack(fill="x", padx=10, pady=(0, 2))

current_ide_label = tk.Label(current_frame, text="Current in VSCode:", bg=BG, fg="#6c7086",
         font=("Segoe UI", 9, "bold"))
current_ide_label.pack(side="left")

_current_labels: dict[str, tk.Label] = {}
for _ext_id in db.EXTENSIONS.values():
    if _ext_id is None:
        continue
    lbl = tk.Label(current_frame, text="", bg=BG, fg="#6c7086",
                   font=("Segoe UI", 9))
    lbl.pack(side="left", padx=(8, 0))
    _current_labels[_ext_id] = lbl

# Tree
cols = ("name", "ext", "accountIds", "saved", "expires", "active")
tree = ttk.Treeview(root, columns=cols, show="headings", height=8, selectmode="browse")
tree.heading("name",       text="Name")
tree.heading("ext",        text="Ext")
tree.heading("accountIds", text="Account IDs")
tree.heading("saved",      text="Saved")
tree.heading("expires",    text="Expires")
tree.heading("active",     text="Active")
tree.column("name",       width=110, anchor="w")
tree.column("ext",        width=70,  anchor="center")
tree.column("accountIds", width=140, anchor="w")
tree.column("saved",      width=110, anchor="center")
tree.column("expires",    width=80,  anchor="center")
tree.column("active",     width=100, anchor="center")
tree.pack(padx=10, pady=(0, 6))

# Buttons
btn_frame = tk.Frame(root, bg=BG)
btn_frame.pack(padx=10, pady=(0, 6))

def btn(parent, text, cmd, accent=False):
    bg = "#89b4fa" if accent else BTN_BG
    fg = SEL_FG if accent else FG
    b = tk.Button(parent, text=text, command=cmd,
                  bg=bg, fg=fg, activebackground=BTN_ACT, activeforeground=FG,
                  relief="flat", padx=12, pady=6, font=("Segoe UI", 10), cursor="hand2")
    b.pack(side="left", padx=4)
    return b

btn(btn_frame, "💾 Save current",  on_save,           accent=True)
btn(btn_frame, "📥 Import Codex",  on_import_codex)
btn(btn_frame, "▶ Use selected",   on_use)
btn(btn_frame, "🗑 Delete",         on_delete)
btn(btn_frame, "⟳ Refresh",        on_refresh)
btn(btn_frame, "📦 Full backup",    on_backup)

# Status bar
status_var = tk.StringVar(value="Ready")
status_label = tk.Label(root, textvariable=status_var, bg=BG, fg="#2d8a4e",
                        font=("Segoe UI", 9), anchor="w")
status_label.pack(fill="x", padx=10, pady=(0, 8))

refresh_accounts()
root.mainloop()
