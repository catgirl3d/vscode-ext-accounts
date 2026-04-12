# vscode-ext-accounts

[🇺🇦 Українська](README.uk.md)

A GUI utility for managing saved accounts for VSCode/Antigravity extensions and standalone Codex.
It reads and writes account data stored in `state.vscdb` (AES-256-GCM via Windows DPAPI), `~/.local/share/kilo/auth.json`, and `~/.codex/auth.json`.

## Use Cases

**Switch between multiple IDE accounts**

You have several accounts and want to switch them in Kilocode, Roo-Cline, or Kilo New:

1. Sign in inside the target extension.
2. Open **IDE Accounts** → tick the slots you want to save → **Save current**.
3. Repeat for your other accounts.
4. Close VSCode / Antigravity before applying.
5. Select the saved account → tick the target slots → **Use selected**.

**Use one login for both extensions**

You authenticated in Roo-Cline and want the same session in Kilocode (or vice versa):

1. Save the current account in **IDE Accounts**.
2. Close the IDE
3. Select the saved account → tick **Kilocode** and/or **Roo-Cline** → **Use selected**

The token is automatically remapped to the correct extension slot, even if it was originally saved under a different one.

**Use the same account in Kilo New**

Kilo New stores tokens in `~/.local/share/kilo/auth.json` — a completely separate file from `state.vscdb`.
That auth file is shared for Kilo New regardless of whether you use it from VSCode or Antigravity.
The tool handles format conversion automatically:

1. Save the account with any extension (e.g. **Kilocode**)
2. Close the IDEs that may currently use Kilo New
3. Select the saved account → tick **Kilo New** → **Use selected**

**Manage Codex separately**

Codex is not treated like an IDE extension slot. It has its own tab and its own auth file:

1. Open the **Codex** tab.
2. Use **Save current Codex** to snapshot the current `~/.codex/auth.json`, or **Import Codex auth** to import another Codex auth file.
3. Select a saved Codex account → **Use selected Codex** to write it back to `~/.codex/auth.json`.

`IDE -> Codex` import/apply is intentionally not supported because Codex requires `id_token`.

## Requirements

```bash
pip install cryptography
```

## GUI

```bash
python gui.py
```

![VSCode Account Manager](1.png)

The app has two tabs: **IDE Accounts** and **Codex**.

The **IDE** selector at the top chooses which IDE the GUI shows and targets (VSCode / Antigravity).

The **IDE Accounts** tab uses extension checkboxes to control which IDE slots are read or written:
- **Kilocode** — only `kilocode.kilo-code` (`state.vscdb`)
- **Roo-Cline** — only `rooveterinaryinc.roo-cline` (`state.vscdb`)
- **Kilo New** — `~/.local/share/kilo/auth.json` (shared Kilo New auth, not `state.vscdb`)

The **IDE Accounts** tab provides:
- **Save current** — save the selected IDE/Kilo New account state
- **Use selected** — apply a saved IDE account to the checked targets
- **Delete** — remove a saved IDE account
- **Refresh** — reload current state and saved accounts
- **Full backup** — create a real ZIP snapshot of the app storages (`state.vscdb`, `Local State`, Kilo New auth, Codex auth)

The **Active** column shows where each account is currently applied: `VS` (VSCode), `AG` (Antigravity), `KN` (Kilo New).

The **Codex** tab is separate because Codex stores its token set in `~/.codex/auth.json` and requires `id_token`.

The **Codex** tab provides:
- **Save current Codex** — save the current `~/.codex/auth.json`
- **Import Codex auth** — import another Codex auth file into saved accounts
- **Use selected Codex** — write a saved Codex account to `~/.codex/auth.json`
- **Delete** — remove a saved Codex account
- **Refresh** — reload current Codex state and saved accounts

### Notes

- Choose **VSCode** or **Antigravity** at the top of **IDE Accounts**.
- Tick one or more extension checkboxes before **Save current** or **Use selected**.
- The target IDE must stay closed while **Use selected** is applying changes.
- Saved accounts are stored in the local `accounts/` directory.
- Before the app writes to IDE/Kilo New/Codex storage, it creates an automatic pre-write ZIP backup of the affected files.
- `Full backup` warns only when required files for the current IDE are missing, reports other absent storages as skipped/optional, and fails if no target files exist at all.

`Kilo New` always reads from and writes to `~/.local/share/kilo/auth.json`, and that file is used by Kilo New in both VSCode and Antigravity.

Codex is intentionally isolated from IDE account switching. `IDE -> Codex` import/apply is not supported.

`parse_vscdb.py` is now a backend module for the GUI. Run `python gui.py` instead.

If you launch `python parse_vscdb.py` directly, it exits immediately with a short GUI-only message.

## Storage locations

| Storage | Path |
|---------|------|
| VSCode secrets | `%APPDATA%\Code\User\globalStorage\state.vscdb` |
| Antigravity secrets | `%APPDATA%\Antigravity\User\globalStorage\state.vscdb` |
| Kilo New auth | `~/.local/share/kilo/auth.json` |
| Codex auth | `~/.codex/auth.json` |
| Saved account profiles | `accounts/*.json` |

`state.vscdb` encryption key is read from `Local State` via Windows DPAPI — only works under the same Windows user.
