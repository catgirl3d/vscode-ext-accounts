from __future__ import annotations

try:
    import test_utils.bootstrap  # type: ignore
except ImportError:
    import tests.test_utils.bootstrap  # type: ignore

import io
import queue
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch
from types import SimpleNamespace

from vscode_inject import gui_app
from vscode_inject.gui_app import (
    AUTO_REFRESH_START_DELAY_MS,
    POLL_INTERVAL_MS,
    execute_auto_refresh_tick,
    execute_guarded_call,
    log_auto_refresh_result,
    poll_ide_runtime_state,
    start_auto_refresh_worker,
)
from vscode_inject import parse_vscdb as db
from vscode_inject import refresh_scheduler


class GuiAppPollingTests(unittest.TestCase):
    def test_execute_guarded_call_surfaces_backend_exception_message_and_prints_it(self):
        output = io.StringIO()
        with redirect_stdout(output):
            message, ok = execute_guarded_call(lambda: (_ for _ in ()).throw(db.UserFacingError("Specific backend error")))

        self.assertFalse(ok)
        self.assertEqual(message, "Specific backend error")
        self.assertIn("Specific backend error", output.getvalue())

    def test_execute_guarded_call_prints_traceback_for_unexpected_exception(self):
        with patch("vscode_inject.gui_app.traceback.print_exc") as print_exc:
            message, ok = execute_guarded_call(lambda: (_ for _ in ()).throw(RuntimeError("Unexpected failure")))

        self.assertFalse(ok)
        self.assertEqual(message, "Unexpected failure")
        print_exc.assert_called_once_with()

    def test_execute_guarded_call_logs_manual_refresh_success_with_prefix(self):
        output = io.StringIO()

        with redirect_stdout(output):
            message, ok = execute_guarded_call(lambda: "Renewed tokens for 'alice' (1 token group, 1 entry)", log_prefix="manual-refresh")

        self.assertTrue(ok)
        self.assertEqual(message, "Renewed tokens for 'alice' (1 token group, 1 entry)")
        self.assertIn("[manual-refresh] INFO: Renewed tokens for 'alice' (1 token group, 1 entry)", output.getvalue())

    def test_execute_guarded_call_logs_manual_refresh_error_with_prefix_without_traceback(self):
        output = io.StringIO()

        with redirect_stdout(output), patch("vscode_inject.gui_app.traceback.print_exc") as print_exc:
            message, ok = execute_guarded_call(
                lambda: (_ for _ in ()).throw(db.SavedAccountRefreshError("Token renewal failed for 'alice': invalid_grant")),
                log_prefix="manual-refresh",
            )

        self.assertFalse(ok)
        self.assertEqual(message, "Token renewal failed for 'alice': invalid_grant")
        self.assertIn("[manual-refresh] ERROR: Token renewal failed for 'alice': invalid_grant", output.getvalue())
        print_exc.assert_not_called()

    def test_execute_auto_refresh_tick_returns_failed_result_on_unexpected_exception(self):
        scheduler = Mock()
        scheduler.policy = refresh_scheduler.RefreshPolicy(scan_interval_ms=AUTO_REFRESH_START_DELAY_MS * 10)
        scheduler.run_once.side_effect = RuntimeError("scheduler failure")

        with patch("vscode_inject.gui_app.traceback.print_exc") as print_exc:
            result = execute_auto_refresh_tick(scheduler)

        self.assertFalse(result.ok)
        self.assertEqual(result.message, "scheduler failure")
        self.assertEqual(result.next_delay_ms, AUTO_REFRESH_START_DELAY_MS * 10)
        print_exc.assert_called_once_with()

    def test_start_auto_refresh_worker_skips_when_worker_is_already_running(self):
        scheduler = Mock()
        result_queue: queue.Queue = queue.Queue()
        worker_state = {"running": True}

        started = start_auto_refresh_worker(result_queue, scheduler, worker_state)

        self.assertFalse(started)
        self.assertTrue(result_queue.empty())

    def test_start_auto_refresh_worker_enqueues_scheduler_result(self):
        expected = refresh_scheduler.AutoRefreshResult(next_delay_ms=1234, message="auto", ok=True)
        scheduler = Mock()
        scheduler.run_once.return_value = expected
        result_queue: queue.Queue = queue.Queue()
        worker_state = {"running": False}

        class InlineThread:
            def __init__(self, target=None, daemon=None):
                self.target = target
                self.daemon = daemon

            def start(self):
                if self.target:
                    self.target()

        with patch("vscode_inject.gui_app.threading.Thread", InlineThread):
            started = start_auto_refresh_worker(result_queue, scheduler, worker_state)

        self.assertTrue(started)
        self.assertTrue(worker_state["running"])
        self.assertEqual(result_queue.get_nowait(), expected)

    def test_log_auto_refresh_result_prints_prefixed_message(self):
        output = io.StringIO()
        failure = refresh_scheduler.RefreshFailure(
            group=db.oauth_refresh.RefreshGroup(
                key=db.oauth_refresh.RefreshGroupKey(provider="openai-codex", refresh_token="refresh-1"),
                bundle=db.oauth_refresh.TokenBundle(access_token="", refresh_token="refresh-1", expires=0),
                expires=0,
                entries=(
                    db.oauth_refresh.RefreshableRecordEntry(
                        record_name="codex3",
                        record_path="codex3.json",
                        entry_index=0,
                        group_key=db.oauth_refresh.RefreshGroupKey(provider="openai-codex", refresh_token="refresh-1"),
                        bundle=db.oauth_refresh.TokenBundle(access_token="", refresh_token="refresh-1", expires=0),
                    ),
                ),
            ),
            error_message="terminal token error",
            terminal=True,
        )
        result = refresh_scheduler.AutoRefreshResult(
            next_delay_ms=1000,
            ok=False,
            message="terminal token error",
            failures=(failure,),
        )

        with redirect_stdout(output):
            log_auto_refresh_result(result)

        log_output = output.getvalue()
        self.assertIn("[auto-refresh] ERROR: terminal token error", log_output)
        self.assertIn("[auto-refresh] ERROR DETAIL: accounts=[codex3] provider=openai-codex status=terminal: terminal token error", log_output)

    def test_poll_ide_runtime_state_runs_only_for_active_ide_tab(self):
        root = Mock()
        notebook = Mock()
        ide_tab = SimpleNamespace(frame=".ide", refresh_runtime_state=Mock())
        notebook.select.return_value = ".ide"

        poll_ide_runtime_state(root, notebook, ide_tab)

        ide_tab.refresh_runtime_state.assert_called_once_with()
        root.after.assert_called_once_with(POLL_INTERVAL_MS, poll_ide_runtime_state, root, notebook, ide_tab, POLL_INTERVAL_MS)

    def test_poll_ide_runtime_state_skips_inactive_tab(self):
        root = Mock()
        notebook = Mock()
        ide_tab = SimpleNamespace(frame=".ide", refresh_runtime_state=Mock())
        notebook.select.return_value = ".codex"

        poll_ide_runtime_state(root, notebook, ide_tab)

        ide_tab.refresh_runtime_state.assert_not_called()
        root.after.assert_called_once_with(POLL_INTERVAL_MS, poll_ide_runtime_state, root, notebook, ide_tab, POLL_INTERVAL_MS)

    def test_execute_guarded_call_surfaces_system_exit_and_prints_it(self):
        output = io.StringIO()
        def fail():
            raise SystemExit(42)
        with redirect_stdout(output):
            message, ok = execute_guarded_call(fail)
        self.assertFalse(ok)
        self.assertEqual(message, "Aborted (code 42)")
        self.assertIn("Aborted (code 42)", output.getvalue())

    @patch("vscode_inject.gui_app.refresh_scheduler.AutoRefreshScheduler")
    def test_main_wiring_and_queue_loops(self, mock_scheduler_class):
        mock_root = Mock()
        mock_root.winfo_reqwidth.return_value = 100
        mock_root.winfo_reqheight.return_value = 100
        
        # Capture the after calls
        after_calls = {}
        def mock_after(delay, func, *args):
            after_calls[func.__name__] = (func, args)
        mock_root.after.side_effect = mock_after
        
        mock_root.mainloop.side_effect = lambda: None
        
        mock_ide_tab = Mock()
        mock_codex_tab = Mock()
        mock_omp_tab = Mock()
        
        class InlineThread:
            def __init__(self, target=None, daemon=None):
                self.target = target
            def start(self):
                if self.target:
                    self.target()

        with patch("vscode_inject.gui_app.tk.Tk", return_value=mock_root), \
             patch("vscode_inject.gui_app.tk.StringVar"), \
             patch("vscode_inject.gui_app.tk.Label"), \
             patch("vscode_inject.gui_app.ttk.Notebook"), \
             patch("vscode_inject.gui_app.ttk.Style"), \
             patch("vscode_inject.gui_app.IdeAccountsTab", return_value=mock_ide_tab) as mock_ide_ctr, \
             patch("vscode_inject.gui_app.CodexTab", return_value=mock_codex_tab) as mock_codex_ctr, \
             patch("vscode_inject.gui_app.OmpOpenAITab", return_value=mock_omp_tab) as mock_omp_ctr, \
             patch("vscode_inject.gui_app.threading.Thread", InlineThread):

             gui_app.main()

             # Check that main initialized the tabs and scheduler
             mock_ide_ctr.assert_called_once()
             mock_codex_ctr.assert_called_once()
             mock_omp_ctr.assert_called_once()
             mock_scheduler_class.assert_called_once()

             # Check that loops are scheduled
             self.assertIn("process_ui_queue", after_calls)
             self.assertIn("process_auto_refresh_queue", after_calls)
             self.assertIn("poll_ide_runtime_state", after_calls)
             self.assertIn("request_auto_refresh", after_calls)

             # Now, retrieve the services object which has run_guarded
             services = mock_ide_ctr.call_args[0][1]
             
             # Check status label update via set_status and run_guarded:
             services.run_guarded(lambda: "Success message", success_msg="Worked!")
             
             # Call process_ui_queue_func to drain the ui_queue and refresh tabs
             process_ui_queue_func = after_calls["process_ui_queue"][0]
             process_ui_queue_func()

             # Verify state
             mock_ide_tab.refresh.assert_called()
             mock_codex_tab.refresh.assert_called()
             mock_omp_tab.refresh.assert_called()

             # We can test process_auto_refresh_queue similarly.
             request_auto_refresh_func = after_calls["request_auto_refresh"][0]
             
             # Mock the scheduler to return a fake AutoRefreshResult
             fake_result = refresh_scheduler.AutoRefreshResult(next_delay_ms=999, message="Tick!", ok=True, refresh_ui=True)
             
             # Let's mock the start_auto_refresh_worker to put the fake result into the queue:
             def fake_start_worker(q, s, state):
                 q.put(fake_result)
                 return True
                 
             with patch("vscode_inject.gui_app.start_auto_refresh_worker", side_effect=fake_start_worker):
                 request_auto_refresh_func()
                 
             # Now call process_auto_refresh_queue to process it:
             process_auto_refresh_queue_func = after_calls["process_auto_refresh_queue"][0]
             process_auto_refresh_queue_func()


if __name__ == "__main__":
    unittest.main()
