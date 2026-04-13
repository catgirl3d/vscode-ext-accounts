"""
Minimal GUI for vscode_inject account manager.
Run: python main.py
"""

import queue
import threading
import tkinter as tk
from tkinter import ttk

from . import parse_vscdb as db
from .gui_tabs import CodexTab, GuiServices, IdeAccountsTab


WINDOW_WIDTH = 980
BG = "#1e1e2e"
FG = "#cdd6f4"
BTN_BG = "#313244"
BTN_ACT = "#45475a"
SEL_BG = "#89b4fa"
SEL_FG = "#1e1e2e"


def main():
    ui_queue = queue.Queue()
    root = tk.Tk()
    root.title("Account Manager")
    root.resizable(False, False)
    root.configure(bg=BG)

    status_var = tk.StringVar(value="Ready")

    def set_status(msg, ok=True):
        color = "#2d8a4e" if ok else "#c0392b"
        status_var.set(msg)
        status_label.config(fg=color)

    def run_guarded(fn, *args, success_msg=None):
        """Run fn in a worker thread and refresh UI on completion."""

        def _run():
            ok = True
            message = success_msg
            try:
                result = fn(*args)
                if message is None and isinstance(result, str):
                    message = result
            except SystemExit as exc:
                ok = False
                message = f"Aborted (code {exc.code})"
            except Exception as exc:
                ok = False
                message = str(exc)
            ui_queue.put((message, ok))

        threading.Thread(target=_run, daemon=True).start()

    style = ttk.Style()
    style.theme_use("clam")
    style.configure("Treeview", background="#181825", fieldbackground="#181825", foreground=FG, rowheight=26, font=("Segoe UI", 10))
    style.configure("Treeview.Heading", background=BTN_BG, foreground=FG, font=("Segoe UI", 9, "bold"))
    style.map("Treeview", background=[("selected", SEL_BG)], foreground=[("selected", SEL_FG)])
    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
    style.configure(
        "TNotebook.Tab",
        background=BTN_BG,
        foreground=FG,
        padding=(14, 8),
        borderwidth=1,
        font=("Segoe UI", 9, "bold"),
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", SEL_BG), ("!selected", BTN_BG)],
        foreground=[("selected", SEL_FG), ("!selected", FG)],
        padding=[("selected", (14, 8)), ("!selected", (14, 8))],
        expand=[("selected", (0, 0, 0, 0)), ("!selected", (0, 0, 0, 0))],
    )

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=10, pady=(8, 6))

    services = GuiServices(
        root=root,
        db=db,
        bg=BG,
        fg=FG,
        btn_bg=BTN_BG,
        btn_act=BTN_ACT,
        sel_fg=SEL_FG,
        run_guarded=run_guarded,
        set_status=set_status,
    )

    ide_tab = IdeAccountsTab(notebook, services)
    codex_tab = CodexTab(notebook, services)

    def refresh_all():
        ide_tab.refresh()
        codex_tab.refresh()

    def process_ui_queue():
        while True:
            try:
                message, ok = ui_queue.get_nowait()
            except queue.Empty:
                break
            if message:
                set_status(message, ok=ok)
            refresh_all()
        root.after(100, process_ui_queue)

    services.refresh_all = refresh_all

    status_label = tk.Label(root, textvariable=status_var, bg=BG, fg="#2d8a4e", font=("Segoe UI", 9), anchor="w")
    status_label.pack(fill="x", padx=10, pady=(0, 8))

    refresh_all()
    process_ui_queue()
    root.update_idletasks()
    root.geometry(f"{max(root.winfo_reqwidth(), WINDOW_WIDTH)}x{root.winfo_reqheight()}")
    root.mainloop()


if __name__ == "__main__":
    main()
