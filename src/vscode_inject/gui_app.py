"""
Minimal GUI for vscode_inject account manager.
Run: python main.py
"""

import queue
import threading
import traceback
import tkinter as tk
from tkinter import ttk
from typing import Any, Mapping

from . import parse_vscdb as db
from . import refresh_scheduler
from .gui_tabs import CodexTab, GuiServices, IdeAccountsTab, OmpOpenAITab, SUCCESS_GREEN


WINDOW_WIDTH = 500
POLL_INTERVAL_MS = 2000
AUTO_REFRESH_START_DELAY_MS = 1000
AUTO_REFRESH_QUEUE_POLL_INTERVAL_MS = 500
BG = "#1e1e2e"
FG = "#cdd6f4"
BTN_BG = "#313244"
BTN_ACT = "#45475a"
HEADER_BG = "#3a3d52"
HEADER_BORDER = "#42465e"
SEL_BG = "#89b4fa"
SEL_FG = "#1e1e2e"


def poll_ide_runtime_state(root, notebook, ide_tab, interval_ms=POLL_INTERVAL_MS):
    try:
        if notebook.select() == str(ide_tab.frame):
            ide_tab.refresh_runtime_state()
    except Exception:
        pass
    root.after(interval_ms, poll_ide_runtime_state, root, notebook, ide_tab, interval_ms)


def execute_guarded_call(fn, *args, success_msg=None, log_prefix=None):
    """Run a backend call and normalize the status payload for the GUI."""
    ok = True
    message = success_msg
    try:
        result = fn(*args)
        if message is None and isinstance(result, str):
            message = result
    except SystemExit as exc:
        ok = False
        message = f"Aborted (code {exc.code})"
        if not log_prefix:
            print(message)
    except db.UserFacingError as exc:
        ok = False
        message = str(exc)
        if not log_prefix:
            print(message)
    except Exception as exc:
        ok = False
        message = str(exc)
        traceback.print_exc()
    if log_prefix and message:
        level = "INFO" if ok else "ERROR"
        print(f"[{log_prefix}] {level}: {message}")
    return message, ok


def execute_auto_refresh_tick(scheduler: refresh_scheduler.AutoRefreshScheduler) -> refresh_scheduler.AutoRefreshResult:
    try:
        return scheduler.run_once()
    except Exception as exc:
        traceback.print_exc()
        return refresh_scheduler.AutoRefreshResult(
            next_delay_ms=scheduler.policy.scan_interval_ms,
            ok=False,
            message=str(exc),
        )


def start_auto_refresh_worker(
    result_queue: queue.Queue,
    scheduler: refresh_scheduler.AutoRefreshScheduler,
    worker_state: dict[str, bool],
) -> bool:
    if worker_state.get("running"):
        return False

    worker_state["running"] = True

    def _run():
        result_queue.put(execute_auto_refresh_tick(scheduler))

    threading.Thread(target=_run, daemon=True).start()
    return True


def log_auto_refresh_result(result: refresh_scheduler.AutoRefreshResult) -> None:
    if not result.message:
        return
    level = "INFO" if result.ok else "ERROR"
    print(f"[auto-refresh] {level}: {result.message}")
    for failure in result.failures:
        names = ", ".join(failure.group.account_names()) or "saved account"
        terminal_label = "terminal" if failure.terminal else "retryable"
        print(
            "[auto-refresh] ERROR DETAIL: "
            f"accounts=[{names}] provider={failure.group.key.provider} status={terminal_label}: {failure.error_message}"
        )


def write_saved_account_mapping_batch(updates: Mapping[str, dict[str, Any]]) -> None:
    db.write_saved_account_batch(dict(updates))


def main():
    ui_queue = queue.Queue()
    auto_refresh_queue: queue.Queue[refresh_scheduler.AutoRefreshResult] = queue.Queue()
    root = tk.Tk()
    root.title("Account Manager")
    root.resizable(False, False)
    root.configure(bg=BG)

    status_var = tk.StringVar(value="Ready")

    def set_status(msg, ok=True):
        color = SUCCESS_GREEN if ok else "#c0392b"
        status_var.set(msg)
        status_label.config(fg=color)

    def run_guarded(fn, *args, success_msg=None, log_prefix=None):
        """Run fn in a worker thread and refresh UI on completion."""

        def _run():
            ui_queue.put(execute_guarded_call(fn, *args, success_msg=success_msg, log_prefix=log_prefix))

        threading.Thread(target=_run, daemon=True).start()

    style = ttk.Style()
    style.theme_use("clam")
    style.configure(
        "Treeview",
        background="#181825",
        fieldbackground="#181825",
        foreground=FG,
        rowheight=26,
        font=("Segoe UI", 10),
        borderwidth=1,
        relief="flat",
        bordercolor=BTN_ACT,
        lightcolor=BTN_ACT,
        darkcolor=BTN_ACT,
    )
    style.configure(
        "Treeview.Heading",
        background=HEADER_BG,
        foreground=FG,
        font=("Segoe UI", 9, "bold"),
        relief="raised",
        borderwidth=1,
        bordercolor=HEADER_BORDER,
        lightcolor=HEADER_BORDER,
        darkcolor=BTN_ACT,
    )
    style.map("Treeview.Heading", background=[("active", HEADER_BG), ("pressed", HEADER_BG)], foreground=[("active", FG), ("pressed", FG)])
    style.map("Treeview", background=[("selected", SEL_BG)], foreground=[("selected", SEL_FG)])
    style.configure(
        "TNotebook",
        background=BG,
        borderwidth=1,
        relief="flat",
        tabmargins=(0, 0, 0, 0),
        bordercolor=BTN_ACT,
        lightcolor=BTN_ACT,
        darkcolor=BTN_ACT,
        focuscolor=BTN_ACT,
    )
    style.configure(
        "TNotebook.Tab",
        background=BTN_BG,
        foreground=FG,
        padding=(14, 8),
        borderwidth=1,
        bordercolor=BTN_ACT,
        lightcolor=BTN_ACT,
        darkcolor=BTN_ACT,
        focuscolor=BTN_ACT,
        font=("Segoe UI", 9, "bold"),
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", SEL_BG), ("!selected", BTN_BG)],
        foreground=[("selected", SEL_FG), ("!selected", FG)],
        bordercolor=[("selected", BTN_ACT), ("!selected", BTN_ACT)],
        lightcolor=[("selected", BTN_ACT), ("!selected", BTN_ACT)],
        darkcolor=[("selected", BTN_ACT), ("!selected", BTN_ACT)],
        focuscolor=[("selected", BTN_ACT), ("!selected", BTN_ACT)],
        padding=[("selected", (14, 8)), ("!selected", (14, 8))],
        expand=[("selected", (0, 0, 0, 0)), ("!selected", (0, 0, 0, 0))],
    )

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=10, pady=(8, 6))

    services = GuiServices(
        root,
        db,
        BG,
        FG,
        BTN_BG,
        BTN_ACT,
        SEL_FG,
        run_guarded,
        set_status,
    )

    ide_tab = IdeAccountsTab(notebook, services)
    codex_tab = CodexTab(notebook, services)
    omp_tab = OmpOpenAITab(notebook, services)
    auto_scheduler = refresh_scheduler.AutoRefreshScheduler(
        list_saved_accounts=db.list_saved_accounts,
        write_saved_account_batch=write_saved_account_mapping_batch,
        persist_refreshed_group=db.persist_auto_refresh_group,
        operation_lock=db.SAVED_ACCOUNT_REFRESH_LOCK,
    )
    auto_refresh_state = {"running": False}

    def refresh_all():
        ide_tab.refresh()
        codex_tab.refresh()
        omp_tab.refresh()

    def request_auto_refresh():
        started = start_auto_refresh_worker(auto_refresh_queue, auto_scheduler, auto_refresh_state)
        if not started:
            schedule_auto_refresh(auto_scheduler.policy.min_delay_ms)

    def schedule_auto_refresh(delay_ms: int):
        root.after(delay_ms, request_auto_refresh)

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

    def process_auto_refresh_queue():
        while True:
            try:
                result = auto_refresh_queue.get_nowait()
            except queue.Empty:
                break
            auto_refresh_state["running"] = False
            if result.message:
                log_auto_refresh_result(result)
                set_status(result.message, ok=result.ok)
            if result.refresh_ui:
                refresh_all()
            schedule_auto_refresh(result.next_delay_ms)
        root.after(AUTO_REFRESH_QUEUE_POLL_INTERVAL_MS, process_auto_refresh_queue)

    services.refresh_all = refresh_all

    status_label = tk.Label(root, textvariable=status_var, bg=BG, fg=SUCCESS_GREEN, font=("Segoe UI", 9), anchor="w")
    status_label.pack(fill="x", padx=10, pady=(0, 8))

    refresh_all()
    process_ui_queue()
    process_auto_refresh_queue()
    poll_ide_runtime_state(root, notebook, ide_tab)
    schedule_auto_refresh(AUTO_REFRESH_START_DELAY_MS)
    root.update_idletasks()
    root.geometry(f"{max(root.winfo_reqwidth(), WINDOW_WIDTH)}x{root.winfo_reqheight()}")
    root.mainloop()


if __name__ == "__main__":
    main()
