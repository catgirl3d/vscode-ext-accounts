from __future__ import annotations

try:
    import test_utils.bootstrap  # type: ignore
except ImportError:
    import tests.test_utils.bootstrap  # type: ignore

import os
import unittest
import tkinter as tk
from tkinter import ttk
from unittest.mock import Mock, patch
from types import SimpleNamespace

from vscode_inject import gui_tabs
from vscode_inject.gui_tabs import CodexTab, EXPIRED_ROW_TAG, GuiServices, IdeAccountsTab, OmpOpenAITab
from vscode_inject import parse_vscdb as db


def button_texts(widget: tk.Misc) -> list[str]:
    return [child.cget("text") for child in widget.winfo_children() if isinstance(child, tk.Button)]


class GuiTabsHelperTests(unittest.TestCase):
    def test_formatting_and_selection_helpers_cover_edge_cases(self):
        self.assertIsInstance(gui_tabs.current_time_ms(), int)
        self.assertEqual(gui_tabs.format_saved_at({}), "?")
        self.assertEqual(gui_tabs.format_saved_at({"saved_at": "2026-05-17T11:18:00"}), "2026-05-17 11:18")
        self.assertEqual(gui_tabs.format_expires_ms(0), "")
        self.assertEqual(gui_tabs.format_expires_ms(86_400_000), "1970-01-02")
        with patch("vscode_inject.gui_tabs.datetime.datetime") as fake_datetime:
            fake_datetime.fromtimestamp.side_effect = RuntimeError("bad timestamp")
            self.assertEqual(gui_tabs.format_expires_ms(86_400_000), "")

        self.assertFalse(gui_tabs.is_expired_ms("bad"))
        self.assertFalse(gui_tabs.is_expired_ms(2_000, now_ms=1_000))
        self.assertTrue(gui_tabs.is_expired_ms(1_000, now_ms=2_000))
        self.assertEqual(gui_tabs.format_saved_expires(1_000, now_ms=2_000), "expired")
        self.assertEqual(gui_tabs.format_saved_expires(86_400_000, now_ms=1_000), "1970-01-02")
        self.assertEqual(gui_tabs.expires_row_tags(1_000, now_ms=2_000), (EXPIRED_ROW_TAG,))
        self.assertEqual(gui_tabs.expires_row_tags(86_400_000, now_ms=1_000), ())
        self.assertEqual(gui_tabs.format_refresh_status(gui_tabs.saved_account_status.REFRESH_STATUS_TERMINAL_ERROR), "invalid")
        self.assertEqual(gui_tabs.format_refresh_status(gui_tabs.saved_account_status.REFRESH_STATUS_ERROR), "error")
        self.assertEqual(gui_tabs.format_refresh_status(gui_tabs.saved_account_status.REFRESH_STATUS_OK), "ok")
        self.assertEqual(gui_tabs.format_refresh_status(None), "-")
        self.assertEqual(
            gui_tabs.account_row_tags({"refresh_status": gui_tabs.saved_account_status.REFRESH_STATUS_TERMINAL_ERROR}, 86_400_000, now_ms=1_000),
            (gui_tabs.TERMINAL_REFRESH_ERROR_ROW_TAG,),
        )
        self.assertEqual(
            gui_tabs.account_row_tags({"refresh_status": gui_tabs.saved_account_status.REFRESH_STATUS_ERROR}, 86_400_000, now_ms=1_000),
            (gui_tabs.REFRESH_ERROR_ROW_TAG,),
        )
        self.assertEqual(
            gui_tabs.account_row_tags({"refresh_status": gui_tabs.saved_account_status.REFRESH_STATUS_OK}, 86_400_000, now_ms=1_000),
            (gui_tabs.REFRESH_OK_ROW_TAG,),
        )
        self.assertEqual(
            gui_tabs.account_row_tags({}, 1_000, now_ms=2_000),
            (EXPIRED_ROW_TAG,),
        )
        self.assertEqual(gui_tabs.shorten_account_id("abcdefghijklmnop", limit=8), "abcdefgh...")
        self.assertEqual(gui_tabs.shorten_account_id(None), "?")

        entries = [
            {"key": "skip-me", "value": {"accountId": "acct-skip"}},
            {"key": "keep-me", "value": {"accountId": "acct-keep-123456"}},
            {"key": "no-id", "value": {}},
        ]
        self.assertEqual(gui_tabs.summarize_account_ids(entries, skip_keys={"skip-me"}), "acct-kee...")
        self.assertEqual(gui_tabs.summarize_account_ids([{"key": "no-id", "value": {}}]), "?")
        self.assertEqual(gui_tabs.first_expires_ms(entries, skip_keys={"skip-me"}), 0)
        self.assertEqual(
            gui_tabs.first_expires_ms(
                [
                    {"key": "a", "value": {"expires": 5_000}},
                    {"key": "b", "value": {"expires": 2_000}},
                ]
            ),
            2_000,
        )
        with patch("vscode_inject.gui_tabs.current_time_ms", return_value=0):
            self.assertEqual(gui_tabs.first_expires([{"key": "a", "value": {"expires": 1_000}}]), "1970-01-01")

        tree = Mock()
        tree.selection.return_value = ()
        with patch("vscode_inject.gui_tabs.messagebox.showwarning") as showwarning:
            self.assertIsNone(gui_tabs.selected_name(tree, "Pick something"))
        showwarning.assert_called_once_with("No selection", "Pick something")

        tree.selection.return_value = ("alice", "bob")
        self.assertEqual(gui_tabs.selected_name(tree, "Pick something"), "alice")

        with patch("vscode_inject.gui_tabs.simpledialog.askstring", return_value=None):
            self.assertIsNone(gui_tabs.ask_account_name(Mock(), "Save", "Name"))
        with patch("vscode_inject.gui_tabs.simpledialog.askstring", return_value="alice account"):
            self.assertEqual(gui_tabs.ask_account_name(Mock(), "Save", "Name"), "alice account")

        clipboard_root = Mock()
        clipboard_root.clipboard_get.return_value = '{"access_token":"a"}'
        self.assertEqual(gui_tabs.clipboard_text_or_empty(clipboard_root), '{"access_token":"a"}')

        clipboard_root.clipboard_get.side_effect = tk.TclError()
        self.assertEqual(gui_tabs.clipboard_text_or_empty(clipboard_root), "")

        delete_saved_account = Mock()
        db_module = SimpleNamespace(delete_saved_account=delete_saved_account)
        gui_tabs.delete_saved_account(db_module, "alice", expected_kind="ide")
        delete_saved_account.assert_called_once_with("alice", expected_kind="ide")

    def test_saved_accounts_tree_tab_requires_explicit_contract_fields(self):
        class BrokenTab(gui_tabs.SavedAccountsTreeTab):
            def __init__(self):
                self.tree = Mock()
                self.services = SimpleNamespace(root=Mock())

            def on_use(self):
                return None

        with self.assertRaisesRegex(RuntimeError, "must define non-empty expected_kind"):
            BrokenTab()._configure_saved_account_tree()


class IdeAccountsTabTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def make_db(self, *, running: bool, antigravity_running: bool = False, vscode_running: bool | None = None):
        running_state = {
            "vscode": running if vscode_running is None else vscode_running,
            "antigravity": antigravity_running,
        }
        ide_extensions = {
            "kilocode": "kilocode.kilo-code",
            "roo-cline": "rooveterinaryinc.roo-cline",
            "kilo-new": "kilo-new://openai",
        }
        save_ide_account = Mock(name="save_ide_account")
        import_ide_account_from_json_string = Mock(name="import_ide_account_from_json_string")
        use_ide_account = Mock(name="use_ide_account")
        refresh_saved_account = Mock(name="refresh_saved_account")
        rename_saved_account = Mock(name="rename_saved_account")
        backup = Mock(name="backup")

        return SimpleNamespace(
            IDE_EXTENSIONS=ide_extensions,
            IDE_PATHS={
                "vscode": {"label": "VSCode"},
                "antigravity": {"label": "Antigravity"},
            },
            _EXT_DISPLAY={value: key for key, value in ide_extensions.items()},
            KILO_NEW_KEY="kilo-new://openai",
            CODEX_KEY="codex://openai",
            read_current_accounts_for_ide=lambda ide: {},
            get_kilo_new_fingerprint=lambda: None,
            list_saved_accounts=lambda kind: [],
            match_saved_to_current=lambda entries, current_accounts: [],
            account_fingerprint=lambda value: None,
            is_ide_running=lambda ide=None: running_state[ide or "vscode"],
            set_ide=lambda name: None,
            save_ide_account=save_ide_account,
            import_ide_account_from_json_string=import_ide_account_from_json_string,
            use_ide_account=use_ide_account,
            refresh_saved_account=refresh_saved_account,
            rename_saved_account=rename_saved_account,
            backup=backup,
            launch_ide=lambda ide=None: f"Started {ide or 'vscode'}",
        )

    def make_services(self, db_module):
        return GuiServices(
            root=self.root,
            db=db_module,
            bg="#1e1e2e",
            fg="#cdd6f4",
            btn_bg="#313244",
            btn_act="#45475a",
            sel_fg="#1e1e2e",
            run_guarded=lambda *args, **kwargs: None,
            set_status=lambda *args, **kwargs: None,
        )

    def test_run_button_is_rendered_in_bottom_button_panel(self):
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, self.make_services(self.make_db(running=False)))

        self.assertIs(tab.run_button.master, tab.btn_frame)
        self.assertEqual(tab.context_menu.entrycget(0, "label"), "Use selected")
        self.assertEqual(tab.context_menu.entrycget(1, "label"), "Rename")
        self.assertEqual(button_texts(tab.btn_frame)[:3], ["▶ Use selected", "💾 Save current", "📥 Import account"])
        self.assertIn("↻ Renew tokens", button_texts(tab.btn_frame))
        self.assertIn("⟳ Reload", button_texts(tab.btn_frame))

    def test_run_button_is_hidden_while_selected_ide_is_running(self):
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, self.make_services(self.make_db(running=True)))

        tab.refresh()
        self.root.update_idletasks()

        self.assertEqual(tab.run_button.winfo_manager(), "")
        self.assertFalse(tab.run_button_visible)

    def test_run_button_reappears_when_selected_ide_is_closed(self):
        db_module = self.make_db(running=False)
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, self.make_services(db_module))

        tab.refresh()
        self.root.update_idletasks()

        self.assertEqual(tab.run_button.winfo_manager(), "pack")
        self.assertTrue(tab.run_button_visible)

    def test_refresh_runtime_state_skips_ui_when_status_unchanged(self):
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, self.make_services(self.make_db(running=False)))

        with patch.object(tab.ide_state_label, "config") as label_config, patch.object(tab, "update_run_button_visibility") as update_visibility:
            first = tab.refresh_runtime_state()
            second = tab.refresh_runtime_state()

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(label_config.call_count, 1)
        self.assertEqual(update_visibility.call_count, 1)

    def test_refresh_runtime_state_updates_when_selected_ide_changes(self):
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, self.make_services(self.make_db(running=False)))

        tab.refresh_runtime_state()
        tab.ide_var.set("antigravity")

        with patch.object(tab.ide_state_label, "config") as label_config, patch.object(tab, "update_run_button_visibility") as update_visibility:
            changed = tab.refresh_runtime_state()

        self.assertTrue(changed)
        self.assertEqual(label_config.call_count, 1)
        self.assertEqual(update_visibility.call_count, 1)

    def test_on_use_allows_kilo_new_live_write_for_vscode_too(self):
        db_module = self.make_db(running=False, vscode_running=True)
        services = self.make_services(db_module)
        services.run_guarded = Mock()
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, services)

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch.object(tab, "selected_exts", return_value=["kilo-new"]), patch(
            "vscode_inject.gui_tabs.messagebox.askyesno", return_value=True
        ) as askyesno, patch("vscode_inject.gui_tabs.messagebox.showerror") as showerror:
            tab.on_use()

        showerror.assert_not_called()
        askyesno.assert_called_once_with("Switch IDE account", "Switch 'alice' [kilo-new]?")
        services.run_guarded.assert_called_once_with(
            db_module.use_ide_account,
            "alice",
            ["kilo-new"],
            True,
            success_msg="Switched 'alice' [kilo-new]",
        )

    def test_on_use_cancels_kilo_new_live_write_when_switch_confirmation_is_rejected(self):
        db_module = self.make_db(running=False, antigravity_running=True)
        services = self.make_services(db_module)
        services.run_guarded = Mock()
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, services)

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch.object(tab, "selected_exts", return_value=["kilo-new"]), patch(
            "vscode_inject.gui_tabs.messagebox.askyesno", return_value=False
        ) as askyesno, patch("vscode_inject.gui_tabs.messagebox.showerror") as showerror:
            tab.on_use()

        showerror.assert_not_called()
        askyesno.assert_called_once_with("Switch IDE account", "Switch 'alice' [kilo-new]?")
        services.run_guarded.assert_not_called()

    def test_on_use_still_blocks_running_antigravity_when_db_write_is_needed(self):
        db_module = self.make_db(running=False, antigravity_running=True)
        services = self.make_services(db_module)
        services.run_guarded = Mock()
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, services)
        tab.ide_var.set("antigravity")

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch.object(tab, "selected_exts", return_value=["kilocode", "kilo-new"]), patch(
            "vscode_inject.gui_tabs.messagebox.askyesno"
        ) as askyesno, patch("vscode_inject.gui_tabs.messagebox.showerror") as showerror:
            tab.on_use()

        askyesno.assert_not_called()
        showerror.assert_called_once()
        services.run_guarded.assert_not_called()

    def test_on_refresh_selected_runs_saved_account_refresh_for_ide_tab(self):
        db_module = self.make_db(running=False)
        services = self.make_services(db_module)
        services.run_guarded = Mock()
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, services)

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"):
            tab.on_refresh_selected()

        refresh_call = services.run_guarded.call_args
        self.assertIsNotNone(refresh_call)
        self.assertEqual(refresh_call.args[1:], ())
        self.assertEqual(refresh_call.kwargs, {"log_prefix": "manual-refresh"})
        refresh_call.args[0]()
        db_module.refresh_saved_account.assert_called_once_with("alice", expected_kind="ide")

    def test_refresh_marks_expired_ide_rows_red_and_labels_them_expired(self):
        db_module = self.make_db(running=False)
        db_module.list_saved_accounts = lambda kind: [
            {
                "name": "expired_ide",
                "data": {
                    "ext": "kilocode",
                    "saved_at": "2026-05-15T10:00:00",
                    "entries": [
                        {
                            "key": db_module.IDE_EXTENSIONS["kilocode"],
                            "value": {
                                "accountId": "acct-1",
                                "expires": 1_000,
                            },
                        }
                    ],
                },
            }
        ]
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, self.make_services(db_module))

        with patch("vscode_inject.gui_tabs.current_time_ms", return_value=2_000):
            tab.refresh()

        self.assertEqual(tab.tree.item("expired_ide", "tags"), (EXPIRED_ROW_TAG,))
        self.assertEqual(tab.tree.item("expired_ide", "values")[4], "expired")

    def test_refresh_marks_invalid_ide_rows_from_terminal_refresh_errors(self):
        db_module = self.make_db(running=False)
        db_module.list_saved_accounts = lambda kind: [
            {
                "name": "invalid_ide",
                "data": {
                    "ext": "kilocode",
                    "saved_at": "2026-05-15T10:00:00",
                    "refresh_status": db.oauth_refresh.REFRESH_STATUS_TERMINAL_ERROR,
                    "entries": [
                        {
                            "key": db_module.IDE_EXTENSIONS["kilocode"],
                            "value": {
                                "accountId": "acct-1",
                                "expires": 4_102_444_800_000,
                            },
                        }
                    ],
                },
            }
        ]
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, self.make_services(db_module))

        tab.refresh()

        self.assertEqual(tab.tree.item("invalid_ide", "tags"), (gui_tabs.TERMINAL_REFRESH_ERROR_ROW_TAG,))
        self.assertEqual(tab.tree.item("invalid_ide", "values")[6], "invalid")

    def test_refresh_marks_ok_ide_rows_green(self):
        db_module = self.make_db(running=False)
        db_module.list_saved_accounts = lambda kind: [
            {
                "name": "ok_ide",
                "data": {
                    "ext": "kilocode",
                    "saved_at": "2026-05-15T10:00:00",
                    "refresh_status": db.oauth_refresh.REFRESH_STATUS_OK,
                    "entries": [
                        {
                            "key": db_module.IDE_EXTENSIONS["kilocode"],
                            "value": {
                                "accountId": "acct-1",
                                "expires": 4_102_444_800_000,
                            },
                        }
                    ],
                },
            }
        ]
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, self.make_services(db_module))

        tab.refresh()

        self.assertEqual(tab.tree.item("ok_ide", "tags"), (gui_tabs.REFRESH_OK_ROW_TAG,))
        self.assertEqual(tab.tree.item("ok_ide", "values")[6], "ok")

    def test_helper_methods_cover_selection_labels_and_current_account_rendering(self):
        db_module = self.make_db(running=False)
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, self.make_services(db_module))

        with patch("vscode_inject.gui_tabs.messagebox.showwarning") as showwarning:
            self.assertEqual(tab.selected_exts(), [])
        showwarning.assert_called_once_with("No extension", "Select at least one IDE extension.")
        self.assertEqual(tab.selected_exts(show_warning=False), [])

        tab.ide_ext_vars["kilocode"].set(True)
        tab.ide_ext_vars["kilo-new"].set(True)
        self.assertEqual(tab.selected_exts(), ["kilocode", "kilo-new"])
        self.assertEqual(tab.format_ext_selection(["kilocode", "kilo-new"]), "kilocode+kilo-new")
        self.assertEqual(tab.db_target_ides_for_exts(["kilocode"]), ["vscode"])
        self.assertEqual(tab.db_target_ides_for_exts(["kilo-new"]), [])
        self.assertEqual(tab.kilo_new_target_ides_for_exts(["kilo-new"]), ["vscode", "antigravity"])
        self.assertTrue(tab.can_hot_swap_kilo_new(["kilo-new"], [], ["antigravity"]))
        self.assertFalse(tab.can_hot_swap_kilo_new(["kilo-new"], ["antigravity"], ["antigravity"]))
        self.assertEqual(tab.required_closed_ides_for_exts(["kilocode", "kilo-new"]), ["vscode", "antigravity"])
        self.assertEqual(
            tab.required_closed_ides_for_exts(["kilocode", "kilo-new"], allow_kilo_new_while_running=True),
            ["vscode"],
        )
        self.assertEqual(tab.format_ide_labels([]), "")
        self.assertEqual(tab.format_ide_labels(["vscode"]), "VSCode")
        self.assertEqual(tab.format_ide_labels(["vscode", "antigravity"]), "VSCode and Antigravity")

        tab.update_current_labels({"kilocode.kilo-code": {"accountId": "acct-kilo-1234567890"}})

        self.assertEqual(tab.current_ide_label.cget("text"), "Current in VSCode:")
        self.assertIn("kilocode:", tab.current_ide_labels["kilocode.kilo-code"].cget("text"))
        self.assertTrue(tab.current_ide_labels["kilocode.kilo-code"].cget("text").endswith("..."))
        self.assertEqual(tab.current_ide_labels["rooveterinaryinc.roo-cline"].cget("text"), "roo-cline: -")

    def test_refresh_runtime_state_hides_visible_run_button_when_ide_starts_running(self):
        running_state = {"vscode": False, "antigravity": False}
        db_module = self.make_db(running=False)
        db_module.is_ide_running = lambda ide=None: running_state[ide or "vscode"]
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, self.make_services(db_module))

        tab.refresh()
        self.root.update_idletasks()
        self.assertEqual(tab.run_button.winfo_manager(), "pack")
        self.assertTrue(tab.run_button_visible)

        running_state["vscode"] = True
        changed = tab.refresh_runtime_state(force=True)
        self.root.update_idletasks()

        self.assertTrue(changed)
        self.assertEqual(tab.run_button.winfo_manager(), "")
        self.assertFalse(tab.run_button_visible)

    def test_refresh_survives_backend_errors_and_marks_active_targets(self):
        db_module = self.make_db(running=False)
        db_module.list_saved_accounts = lambda kind: [
            {
                "name": "alice",
                "data": {
                    "ext": "kilocode",
                    "saved_at": "2026-05-17T11:18:00",
                    "entries": [
                        {
                            "key": db_module.IDE_EXTENSIONS["kilocode"],
                            "value": {
                                "accountId": "acct-kilo-1234567890",
                                "refresh_token": "refresh-match",
                                "expires": 86_400_000,
                            },
                        }
                    ],
                },
            }
        ]
        db_module.account_fingerprint = lambda value: value.get("refresh_token")
        db_module.match_saved_to_current = lambda entries, current_accounts: ["kilocode"] if current_accounts else []

        failing_db_module = self.make_db(running=False)
        failing_db_module.list_saved_accounts = db_module.list_saved_accounts
        failing_db_module.read_current_accounts_for_ide = lambda ide: (_ for _ in ()).throw(RuntimeError(f"{ide} unavailable"))
        failing_db_module.get_kilo_new_fingerprint = lambda: (_ for _ in ()).throw(RuntimeError("kilo unavailable"))
        notebook = ttk.Notebook(self.root)
        failing_tab = IdeAccountsTab(notebook, self.make_services(failing_db_module))

        failing_tab.refresh()

        self.assertEqual(failing_tab.tree.item("alice", "values")[5], "-")

        db_module.read_current_accounts_for_ide = lambda ide: {
            "kilocode.kilo-code": {"accountId": "acct-live", "fingerprint": "refresh-match"}
        }
        db_module.get_kilo_new_fingerprint = lambda: "refresh-match"
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, self.make_services(db_module))

        tab.refresh()

        values = tab.tree.item("alice", "values")
        self.assertEqual(values[5], "VS+AG+KN")
        self.assertEqual(values[2], "acct-kil...")

    def test_on_ide_change_and_save_handlers_delegate_correctly(self):
        db_module = self.make_db(running=False)
        db_module.set_ide = Mock()
        db_module.list_saved_accounts = lambda kind: [
            {
                "name": "alice",
                "data": {
                    "ext": "kilocode",
                    "saved_at": "2026-05-15T10:00:00",
                    "entries": [
                        {
                            "key": db_module.IDE_EXTENSIONS["kilocode"],
                            "value": {
                                "accountId": "acct-1",
                                "expires": 4_102_444_800_000,
                            },
                        }
                    ],
                },
            },
            {
                "name": "bob",
                "data": {
                    "ext": "roo-cline",
                    "saved_at": "2026-05-16T10:00:00",
                    "entries": [
                        {
                            "key": db_module.IDE_EXTENSIONS["roo-cline"],
                            "value": {
                                "accountId": "acct-2",
                                "expires": 4_102_444_800_000,
                            },
                        }
                    ],
                },
            },
        ]
        services = self.make_services(db_module)
        services.run_guarded = Mock()
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, services)

        with patch.object(tab, "refresh") as refresh:
            tab.on_ide_change()
        db_module.set_ide.assert_called_once_with("vscode")
        refresh.assert_called_once_with()

        db_module.set_ide.reset_mock()
        tab.refresh()
        gui_tabs.select_tree_item(tab.tree, "bob")
        tab.ide_var.set("antigravity")

        with patch.object(tab.tree, "focus_set") as focus_set:
            tab.on_ide_change()

        db_module.set_ide.assert_called_once_with("antigravity")
        self.assertEqual(tab.tree.selection(), ("bob",))
        focus_set.assert_not_called()

        with patch("vscode_inject.gui_tabs.ask_account_name", return_value=None):
            tab.on_save()
        services.run_guarded.assert_not_called()

        with patch("vscode_inject.gui_tabs.ask_account_name", return_value="alice"), patch.object(tab, "selected_exts", return_value=[]):
            tab.on_save()
        services.run_guarded.assert_not_called()

        with patch("vscode_inject.gui_tabs.ask_account_name", return_value="alice"), patch.object(
            tab, "selected_exts", return_value=["kilocode", "roo-cline"]
        ):
            tab.on_save()

        services.run_guarded.assert_called_once_with(
            db_module.save_ide_account,
            "alice",
            ["kilocode", "roo-cline"],
            success_msg="Saved 'alice' [kilocode+roo-cline]",
        )

    def test_on_import_clipboard_handles_clipboard_errors_and_success_path(self):
        db_module = self.make_db(running=False)
        services = self.make_services(db_module)
        services.run_guarded = Mock()
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, services)

        with patch.object(tab, "selected_exts", return_value=[]), patch("vscode_inject.gui_tabs.ask_ide_account_import") as ask_import:
            tab.on_import_clipboard()

        ask_import.assert_not_called()
        services.run_guarded.assert_not_called()

        services.run_guarded.reset_mock()
        with patch.object(tab, "selected_exts", return_value=["kilocode", "kilo-new"]), patch(
            "vscode_inject.gui_tabs.ask_ide_account_import", return_value=None
        ):
            tab.on_import_clipboard()

        services.run_guarded.assert_not_called()

        with patch.object(tab, "selected_exts", return_value=["kilocode", "kilo-new"]), patch(
            "vscode_inject.gui_tabs.ask_ide_account_import", return_value=("alice", '[{"access_token":"a"}]')
        ):
            tab.on_import_clipboard()

        services.run_guarded.assert_called_once_with(
            db_module.import_ide_account_from_json_string,
            '[{"access_token":"a"}]',
            "alice",
            ["kilocode", "kilo-new"],
            success_msg="Imported 'alice' [kilocode+kilo-new]",
        )

    def test_ide_account_import_dialog_starts_empty_and_returns_submit_result(self):
        services = self.make_services(self.make_db(running=False))
        dialog = gui_tabs.IdeAccountImportDialog(services)
        self.addCleanup(lambda: dialog.window.winfo_exists() and dialog.window.destroy())

        self.assertIn("access_token", dialog.example_text.get("1.0", "end-1c"))
        self.assertEqual(dialog.payload_text.get("1.0", "end-1c"), "")

        dialog.name_entry.insert(0, "alice")
        dialog.payload_text.insert("1.0", '[{"access_token":"prefill"}]')
        dialog.submit()

        self.assertEqual(dialog.result, ("alice", '[{"access_token":"prefill"}]'))

    def test_ide_account_import_dialog_paste_button_replaces_payload_from_clipboard(self):
        services = self.make_services(self.make_db(running=False))
        dialog = gui_tabs.IdeAccountImportDialog(services)
        self.addCleanup(lambda: dialog.window.winfo_exists() and dialog.window.destroy())

        dialog.payload_text.insert("1.0", "old text")
        with patch("vscode_inject.gui_tabs.clipboard_text_or_empty", return_value='[{"access_token":"from-clipboard"}]'):
            dialog.paste_from_clipboard()

        self.assertEqual(dialog.payload_text.get("1.0", "end-1c"), '[{"access_token":"from-clipboard"}]')

    def test_ide_account_import_dialog_paste_button_warns_when_clipboard_is_empty(self):
        services = self.make_services(self.make_db(running=False))
        dialog = gui_tabs.IdeAccountImportDialog(services)
        self.addCleanup(lambda: dialog.window.winfo_exists() and dialog.window.destroy())

        with patch("vscode_inject.gui_tabs.clipboard_text_or_empty", return_value=""), patch(
            "vscode_inject.gui_tabs.messagebox.showwarning"
        ) as showwarning:
            dialog.paste_from_clipboard()

        showwarning.assert_called_once_with("Import IDE Account", "Clipboard does not contain text.")

    def test_ide_account_import_dialog_validates_missing_fields(self):
        services = self.make_services(self.make_db(running=False))
        dialog = gui_tabs.IdeAccountImportDialog(services)
        self.addCleanup(lambda: dialog.window.winfo_exists() and dialog.window.destroy())

        with patch("vscode_inject.gui_tabs.messagebox.showwarning") as showwarning:
            dialog.submit()
        showwarning.assert_called_once_with("Import IDE Account", "Enter an account name.")

        showwarning.reset_mock()
        dialog.name_entry.insert(0, "alice")
        with patch("vscode_inject.gui_tabs.messagebox.showwarning") as showwarning:
            dialog.submit()
        showwarning.assert_called_once_with("Import IDE Account", "Paste the account JSON to import.")

    def test_on_use_delete_backup_refresh_and_run_handlers_cover_branches(self):
        db_module = self.make_db(running=False)
        services = self.make_services(db_module)
        services.run_guarded = Mock()
        services.set_status = Mock()
        services.refresh_all = Mock()
        notebook = ttk.Notebook(self.root)
        tab = IdeAccountsTab(notebook, services)

        with patch("vscode_inject.gui_tabs.selected_name", return_value=None):
            tab.on_use()
        services.run_guarded.assert_not_called()

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch.object(tab, "selected_exts", return_value=[]):
            tab.on_use()
        services.run_guarded.assert_not_called()

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch.object(
            tab, "selected_exts", return_value=["kilocode"]
        ), patch("vscode_inject.gui_tabs.messagebox.askyesno", return_value=False) as askyesno:
            tab.on_use()
        askyesno.assert_called_once_with("Switch IDE account", "Switch 'alice' [kilocode]?\nVSCode must stay closed until done.")
        services.run_guarded.assert_not_called()

        db_module.is_ide_running = lambda ide=None: True
        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch.object(
            tab, "selected_exts", return_value=["kilo-new"]
        ), patch.object(tab, "can_hot_swap_kilo_new", return_value=False), patch(
            "vscode_inject.gui_tabs.messagebox.showerror"
        ) as showerror:
            tab.on_use()
        showerror.assert_called_once_with(
            "VSCode and Antigravity running",
            "Close VSCode and Antigravity before switching accounts.",
        )
        services.run_guarded.assert_not_called()
        db_module.is_ide_running = lambda ide=None: False

        with patch("vscode_inject.gui_tabs.selected_name", return_value=None):
            tab.on_delete()
        services.set_status.assert_not_called()

        db_module.rename_saved_account.reset_mock()
        with patch("vscode_inject.gui_tabs.selected_name", return_value=None):
            tab.on_rename()
        db_module.rename_saved_account.assert_not_called()

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.ask_account_name", return_value=None
        ):
            tab.on_rename()
        db_module.rename_saved_account.assert_not_called()

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.ask_account_name", return_value="alice"
        ):
            tab.on_rename()
        db_module.rename_saved_account.assert_not_called()

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.ask_account_name", return_value="alice_renamed"
        ):
            tab.on_rename()
        db_module.rename_saved_account.assert_called_once_with("alice", "alice_renamed", expected_kind="ide")
        services.set_status.assert_called_once_with("Renamed 'alice' to 'alice_renamed'", True)
        services.refresh_all.assert_called_once_with()

        services.set_status.reset_mock()
        services.refresh_all.reset_mock()
        db_module.rename_saved_account.reset_mock()
        db_module.rename_saved_account.side_effect = RuntimeError("rename failed")
        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.ask_account_name", return_value="alice_renamed"
        ):
            tab.on_rename()
        services.set_status.assert_called_once_with("rename failed", False)
        services.refresh_all.assert_not_called()
        db_module.rename_saved_account.side_effect = None
        services.set_status.reset_mock()
        services.refresh_all.reset_mock()

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.messagebox.askyesno", return_value=False
        ) as askyesno, patch("vscode_inject.gui_tabs.delete_saved_account") as delete_saved_account:
            tab.on_delete()
        askyesno.assert_called_once_with("Delete", "Delete saved account 'alice'?")
        delete_saved_account.assert_not_called()

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.messagebox.askyesno", return_value=True
        ), patch("vscode_inject.gui_tabs.delete_saved_account") as delete_saved_account:
            tab.on_delete()
        delete_saved_account.assert_called_once_with(db_module, "alice", expected_kind="ide")
        services.set_status.assert_called_once_with("Deleted 'alice'", True)
        services.refresh_all.assert_called_once_with()

        services.set_status.reset_mock()
        services.refresh_all.reset_mock()
        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.messagebox.askyesno", return_value=True
        ), patch("vscode_inject.gui_tabs.delete_saved_account", side_effect=OSError("delete failed")):
            tab.on_delete()
        services.set_status.assert_called_once_with("delete failed", False)
        services.refresh_all.assert_not_called()

        services.run_guarded.reset_mock()
        with patch("vscode_inject.gui_tabs.selected_name", return_value=None):
            tab.on_refresh_selected()
        services.run_guarded.assert_not_called()

        db_module.refresh_saved_account.reset_mock()
        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"):
            tab.on_refresh_selected()
        refresh_call = services.run_guarded.call_args
        self.assertIsNotNone(refresh_call)
        self.assertEqual(refresh_call.args[1:], ())
        self.assertEqual(refresh_call.kwargs, {"log_prefix": "manual-refresh"})
        refresh_call.args[0]()
        db_module.refresh_saved_account.assert_called_once_with("alice", expected_kind="ide")

        services.run_guarded.reset_mock()
        tab.on_backup()
        services.run_guarded.assert_called_once_with(db_module.backup)

        services.set_status.reset_mock()
        services.refresh_all.reset_mock()
        tab.on_refresh()
        services.refresh_all.assert_called_once_with()
        services.set_status.assert_called_once_with("Reloaded view", True)

        services.set_status.reset_mock()
        with patch.object(tab, "refresh") as refresh:
            tab.on_run()
        services.set_status.assert_called_once_with("Started vscode", True)
        refresh.assert_called_once_with()

        services.set_status.reset_mock()
        db_module.launch_ide = lambda ide=None: (_ for _ in ()).throw(RuntimeError("launch failed"))
        with patch.object(tab, "refresh") as refresh, patch("vscode_inject.gui_tabs.messagebox.showerror") as showerror:
            tab.on_run()
        showerror.assert_called_once_with("Run IDE", "launch failed")
        services.set_status.assert_called_once_with("launch failed", False)
        refresh.assert_not_called()


class OmpOpenAITabTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def make_db(self):
        save_omp_openai_account = Mock(name="save_omp_openai_account")
        import_omp_openai_account_from_json_string = Mock(name="import_omp_openai_account_from_json_string")
        append_omp_openai_account_from_json_string = Mock(name="append_omp_openai_account_from_json_string")
        use_omp_openai_account = Mock(name="use_omp_openai_account")
        refresh_saved_account = Mock(name="refresh_saved_account")
        rename_saved_account = Mock(name="rename_saved_account")

        current_accounts = [
            {
                "key": "omp://openai",
                "accountId": "acct-omp-1234567890",
                "fingerprint": "refresh-omp",
                "expires": 123,
            }
        ]
        saved_records = [
            {
                "name": "alice",
                "kind": "omp",
                "readable": True,
                "data": {
                    "entries": [
                        {
                            "key": "omp://openai",
                            "value": {
                                "type": "openai-codex",
                                "accountId": "acct-omp-1234567890",
                                "refresh_token": "refresh-omp",
                                "expires": 123,
                            },
                        }
                    ]
                },
            }
        ]

        return SimpleNamespace(
            OMP_AGENT_DB_PATH="C:/Users/Test/.omp/agent/agent.db",
            OMP_OPENAI_KEY="omp://openai",
            account_fingerprint=lambda value: value.get("refresh_token") if isinstance(value, dict) else None,
            read_current_omp_openai_accounts=lambda: list(current_accounts),
            list_saved_accounts=lambda kind: list(saved_records) if kind == "omp" else [],
            save_omp_openai_account=save_omp_openai_account,
            import_omp_openai_account_from_json_string=import_omp_openai_account_from_json_string,
            append_omp_openai_account_from_json_string=append_omp_openai_account_from_json_string,
            use_omp_openai_account=use_omp_openai_account,
            refresh_saved_account=refresh_saved_account,
            rename_saved_account=rename_saved_account,
            delete_saved_account=Mock(name="delete_saved_account"),
        )

    def make_services(self, db_module):
        return GuiServices(
            root=self.root,
            db=db_module,
            bg="#1e1e2e",
            fg="#cdd6f4",
            btn_bg="#313244",
            btn_act="#45475a",
            sel_fg="#1e1e2e",
            run_guarded=Mock(),
            set_status=lambda *args, **kwargs: None,
        )

    def test_omp_tab_renders_and_marks_matching_profile_active(self):
        notebook = ttk.Notebook(self.root)
        services = self.make_services(self.make_db())
        tab = OmpOpenAITab(notebook, services)

        tab.refresh()

        row_values = tab.tree.item("alice", "values")
        self.assertEqual(row_values[0], "alice")
        self.assertEqual(row_values[4], "active")
        self.assertEqual(tab.current_value.cget("text"), "acct-omp...")

    def test_omp_tab_actions_call_backend_contracts(self):
        notebook = ttk.Notebook(self.root)
        db_module = self.make_db()
        services = self.make_services(db_module)
        tab = OmpOpenAITab(notebook, services)

        with patch("vscode_inject.gui_tabs.ask_account_name", return_value="saved-omp"):
            tab.on_save()
        services.run_guarded.assert_called_once_with(
            db_module.save_omp_openai_account,
            "saved-omp",
            success_msg="Saved OMP OpenAI account 'saved-omp'",
        )

        services.run_guarded.reset_mock()
        with patch("vscode_inject.gui_tabs.ask_omp_openai_import", return_value=("imported-omp", "{\"access_token\":\"a\"}")):
            tab.on_import_new()
        services.run_guarded.assert_called_once_with(
            db_module.import_omp_openai_account_from_json_string,
            '{"access_token":"a"}',
            "imported-omp",
            success_msg="Imported OMP OpenAI account 'imported-omp'",
        )

        services.run_guarded.reset_mock()
        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.ask_omp_openai_append_import",
            return_value=("alice", "{\"access_token\":\"b\"}"),
        ):
            tab.on_import_append()
        services.run_guarded.assert_called_once_with(
            db_module.append_omp_openai_account_from_json_string,
            '{"access_token":"b"}',
            "alice",
            success_msg="Added imported OMP OpenAI credential(s) to 'alice'",
        )

        services.run_guarded.reset_mock()
        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.messagebox.askyesno", return_value=True
        ):
            tab.on_use()
        services.run_guarded.assert_called_once_with(
            db_module.use_omp_openai_account,
            "alice",
            success_msg="Switched OMP OpenAI to 'alice'",
        )


class CodexTabTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def make_services(self, db_module):
        return GuiServices(
            root=self.root,
            db=db_module,
            bg="#1e1e2e",
            fg="#cdd6f4",
            btn_bg="#313244",
            btn_act="#45475a",
            sel_fg="#1e1e2e",
            run_guarded=Mock(),
            set_status=lambda *args, **kwargs: None,
        )

    def test_on_refresh_selected_runs_saved_account_refresh_for_codex_tab(self):
        db_module = SimpleNamespace(
            CODEX_AUTH_PATH="C:/Users/Test/.codex/auth.json",
            CODEX_KEY="codex://openai",
            read_current_codex_account=lambda: {},
            list_saved_accounts=lambda kind: [],
            account_fingerprint=lambda value: None,
            refresh_saved_account=Mock(name="refresh_saved_account"),
            rename_saved_account=Mock(name="rename_saved_account"),
        )
        services = self.make_services(db_module)
        notebook = ttk.Notebook(self.root)
        tab = CodexTab(notebook, services)

        self.assertEqual(tab.context_menu.entrycget(0, "label"), "Use selected")
        self.assertEqual(tab.context_menu.entrycget(1, "label"), "Rename")
        self.assertEqual(button_texts(tab.btn_frame)[:3], ["▶ Use selected Codex", "💾 Save current Codex", "📥 Import Codex auth"])
        self.assertIn("↻ Renew tokens", button_texts(tab.btn_frame))
        self.assertIn("⟳ Reload", button_texts(tab.btn_frame))

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"):
            tab.on_refresh_selected()

        refresh_call = services.run_guarded.call_args
        self.assertIsNotNone(refresh_call)
        self.assertEqual(refresh_call.args[1:], ())
        self.assertEqual(refresh_call.kwargs, {"log_prefix": "manual-refresh"})
        refresh_call.args[0]()
        db_module.refresh_saved_account.assert_called_once_with("alice", expected_kind="codex")

    def test_refresh_marks_expired_codex_rows_red_and_labels_them_expired(self):
        db_module = SimpleNamespace(
            CODEX_AUTH_PATH="C:/Users/Test/.codex/auth.json",
            CODEX_KEY="codex://openai",
            read_current_codex_account=lambda: {},
            list_saved_accounts=lambda kind: [
                {
                    "name": "expired_codex",
                    "data": {
                        "saved_at": "2026-05-15T10:00:00",
                        "entries": [
                            {
                                "key": "codex://openai",
                                "value": {
                                    "accountId": "acct-codex",
                                    "expires": 1_000,
                                },
                            }
                        ],
                    },
                }
            ],
            account_fingerprint=lambda value: None,
            refresh_saved_account=Mock(name="refresh_saved_account"),
            rename_saved_account=Mock(name="rename_saved_account"),
        )
        services = self.make_services(db_module)
        notebook = ttk.Notebook(self.root)
        tab = CodexTab(notebook, services)

        with patch("vscode_inject.gui_tabs.current_time_ms", return_value=2_000):
            tab.refresh()

        self.assertEqual(tab.tree.item("expired_codex", "tags"), (EXPIRED_ROW_TAG,))
        self.assertEqual(tab.tree.item("expired_codex", "values")[3], "expired")

    def test_refresh_marks_failed_codex_rows_with_error_status(self):
        db_module = SimpleNamespace(
            CODEX_AUTH_PATH="C:/Users/Test/.codex/auth.json",
            CODEX_KEY="codex://openai",
            read_current_codex_account=lambda: {},
            list_saved_accounts=lambda kind: [
                {
                    "name": "errored_codex",
                    "data": {
                        "saved_at": "2026-05-15T10:00:00",
                        "refresh_status": db.oauth_refresh.REFRESH_STATUS_ERROR,
                        "entries": [
                            {
                                "key": "codex://openai",
                                "value": {
                                    "accountId": "acct-codex",
                                    "expires": 4_102_444_800_000,
                                },
                            }
                        ],
                    },
                }
            ],
            account_fingerprint=lambda value: None,
            refresh_saved_account=Mock(name="refresh_saved_account"),
            rename_saved_account=Mock(name="rename_saved_account"),
        )
        services = self.make_services(db_module)
        notebook = ttk.Notebook(self.root)
        tab = CodexTab(notebook, services)

        tab.refresh()

        self.assertEqual(tab.tree.item("errored_codex", "tags"), (gui_tabs.REFRESH_ERROR_ROW_TAG,))
        self.assertEqual(tab.tree.item("errored_codex", "values")[5], "error")

    def test_refresh_marks_ok_codex_rows_green(self):
        db_module = SimpleNamespace(
            CODEX_AUTH_PATH="C:/Users/Test/.codex/auth.json",
            CODEX_KEY="codex://openai",
            read_current_codex_account=lambda: {},
            list_saved_accounts=lambda kind: [
                {
                    "name": "ok_codex",
                    "data": {
                        "saved_at": "2026-05-15T10:00:00",
                        "refresh_status": db.oauth_refresh.REFRESH_STATUS_OK,
                        "entries": [
                            {
                                "key": "codex://openai",
                                "value": {
                                    "accountId": "acct-codex",
                                    "expires": 4_102_444_800_000,
                                },
                            }
                        ],
                    },
                }
            ],
            account_fingerprint=lambda value: None,
            refresh_saved_account=Mock(name="refresh_saved_account"),
            rename_saved_account=Mock(name="rename_saved_account"),
        )
        services = self.make_services(db_module)
        notebook = ttk.Notebook(self.root)
        tab = CodexTab(notebook, services)

        tab.refresh()

        self.assertEqual(tab.tree.item("ok_codex", "tags"), (gui_tabs.REFRESH_OK_ROW_TAG,))
        self.assertEqual(tab.tree.item("ok_codex", "values")[5], "ok")

    def test_update_current_label_refresh_and_handlers_cover_remaining_codex_branches(self):
        db_module = SimpleNamespace(
            CODEX_AUTH_PATH="C:/Users/Test/.codex/auth.json",
            CODEX_KEY="codex://openai",
            read_current_codex_account=lambda: {
                "codex://openai": {"accountId": "acct-codex-1234567890", "fingerprint": "refresh-codex"}
            },
            list_saved_accounts=lambda kind: [
                {
                    "name": "alice",
                    "data": {
                        "saved_at": "2026-05-17T11:18:00",
                        "entries": [
                            {
                                "key": "codex://openai",
                                "value": {
                                    "accountId": "acct-codex-1234567890",
                                    "expires": 86_400_000,
                                    "refresh_token": "refresh-codex",
                                },
                            }
                        ],
                    },
                },
                {
                    "name": "skip",
                    "data": {"entries": [{"key": "other", "value": {"accountId": "acct-skip"}}]},
                },
            ],
            account_fingerprint=lambda value: value.get("refresh_token"),
            save_codex_account=Mock(name="save_codex_account"),
            import_codex_account=Mock(name="import_codex_account"),
            use_codex_account=Mock(name="use_codex_account"),
            refresh_saved_account=Mock(name="refresh_saved_account"),
            rename_saved_account=Mock(name="rename_saved_account"),
        )
        services = self.make_services(db_module)
        services.set_status = Mock()
        services.refresh_all = Mock()
        notebook = ttk.Notebook(self.root)
        tab = CodexTab(notebook, services)

        tab.refresh()

        self.assertTrue(tab.current_value.cget("text").endswith("..."))
        self.assertEqual(tab.tree.item("alice", "values")[4], "active")
        self.assertFalse(tab.tree.exists("skip"))

        tab.update_current_label({})
        self.assertEqual(tab.current_value.cget("text"), "-")

        error_db_module = SimpleNamespace(
            CODEX_AUTH_PATH="C:/Users/Test/.codex/auth.json",
            CODEX_KEY="codex://openai",
            read_current_codex_account=lambda: (_ for _ in ()).throw(RuntimeError("codex unavailable")),
            list_saved_accounts=db_module.list_saved_accounts,
            account_fingerprint=db_module.account_fingerprint,
            save_codex_account=db_module.save_codex_account,
            import_codex_account=db_module.import_codex_account,
            use_codex_account=db_module.use_codex_account,
            refresh_saved_account=db_module.refresh_saved_account,
            rename_saved_account=db_module.rename_saved_account,
        )
        error_tab = CodexTab(ttk.Notebook(self.root), self.make_services(error_db_module))
        error_tab.refresh()
        self.assertEqual(error_tab.current_value.cget("text"), "-")

        with patch("vscode_inject.gui_tabs.ask_account_name", return_value=None):
            tab.on_save()
        services.run_guarded.assert_not_called()

        with patch("vscode_inject.gui_tabs.ask_account_name", return_value="alice"):
            tab.on_save()
        services.run_guarded.assert_called_once_with(
            db_module.save_codex_account,
            "alice",
            success_msg="Saved Codex account 'alice'",
        )

        services.run_guarded.reset_mock()
        with patch("vscode_inject.gui_tabs.filedialog.askopenfilename", return_value=""):
            tab.on_import()
        services.run_guarded.assert_not_called()

        with patch("vscode_inject.gui_tabs.filedialog.askopenfilename", return_value="C:/tmp/auth.json"), patch(
            "vscode_inject.gui_tabs.ask_account_name", return_value=None
        ):
            tab.on_import()
        services.run_guarded.assert_not_called()

        with patch("vscode_inject.gui_tabs.filedialog.askopenfilename", return_value="C:/tmp/auth.json"), patch(
            "vscode_inject.gui_tabs.ask_account_name", return_value="alice"
        ):
            tab.on_import()
        services.run_guarded.assert_called_once_with(
            db_module.import_codex_account,
            "C:/tmp/auth.json",
            "alice",
            success_msg="Imported Codex account 'alice'",
        )

        services.run_guarded.reset_mock()
        with patch("vscode_inject.gui_tabs.selected_name", return_value=None):
            tab.on_use()
        services.run_guarded.assert_not_called()

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.messagebox.askyesno", return_value=False
        ):
            tab.on_use()
        services.run_guarded.assert_not_called()

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.messagebox.askyesno", return_value=True
        ):
            tab.on_use()
        services.run_guarded.assert_called_once_with(
            db_module.use_codex_account,
            "alice",
            success_msg="Switched Codex to 'alice'",
        )

        services.run_guarded.reset_mock()
        db_module.rename_saved_account.reset_mock()
        with patch("vscode_inject.gui_tabs.selected_name", return_value=None):
            tab.on_rename()
        db_module.rename_saved_account.assert_not_called()

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.ask_account_name", return_value=None
        ):
            tab.on_rename()
        db_module.rename_saved_account.assert_not_called()

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.ask_account_name", return_value="alice"
        ):
            tab.on_rename()
        db_module.rename_saved_account.assert_not_called()

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.ask_account_name", return_value="alice_renamed"
        ):
            tab.on_rename()
        db_module.rename_saved_account.assert_called_once_with("alice", "alice_renamed", expected_kind="codex")
        services.set_status.assert_called_once_with("Renamed 'alice' to 'alice_renamed'", True)
        services.refresh_all.assert_called_once_with()

        services.set_status.reset_mock()
        services.refresh_all.reset_mock()
        db_module.rename_saved_account.reset_mock()
        db_module.rename_saved_account.side_effect = RuntimeError("rename failed")
        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.ask_account_name", return_value="alice_renamed"
        ):
            tab.on_rename()
        services.set_status.assert_called_once_with("rename failed", False)
        services.refresh_all.assert_not_called()
        db_module.rename_saved_account.side_effect = None
        services.set_status.reset_mock()
        services.refresh_all.reset_mock()

        with patch("vscode_inject.gui_tabs.selected_name", return_value=None):
            tab.on_delete()
        services.set_status.assert_not_called()

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.messagebox.askyesno", return_value=False
        ) as askyesno, patch("vscode_inject.gui_tabs.delete_saved_account") as delete_saved_account:
            tab.on_delete()
        askyesno.assert_called_once_with("Delete", "Delete saved account 'alice'?")
        delete_saved_account.assert_not_called()

        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.messagebox.askyesno", return_value=True
        ), patch("vscode_inject.gui_tabs.delete_saved_account") as delete_saved_account:
            tab.on_delete()
        delete_saved_account.assert_called_once_with(db_module, "alice", expected_kind="codex")
        services.set_status.assert_called_once_with("Deleted 'alice'", True)
        services.refresh_all.assert_called_once_with()

        services.set_status.reset_mock()
        services.refresh_all.reset_mock()
        with patch("vscode_inject.gui_tabs.selected_name", return_value="alice"), patch(
            "vscode_inject.gui_tabs.messagebox.askyesno", return_value=True
        ), patch("vscode_inject.gui_tabs.delete_saved_account", side_effect=OSError("delete failed")):
            tab.on_delete()
        services.set_status.assert_called_once_with("delete failed", False)
        services.refresh_all.assert_not_called()

        services.run_guarded.reset_mock()
        with patch("vscode_inject.gui_tabs.selected_name", return_value=None):
            tab.on_refresh_selected()
        services.run_guarded.assert_not_called()

        services.set_status.reset_mock()
        services.refresh_all.reset_mock()
        tab.on_refresh()
        services.refresh_all.assert_called_once_with()
        services.set_status.assert_called_once_with("Reloaded view", True)


if __name__ == "__main__":
    unittest.main()
