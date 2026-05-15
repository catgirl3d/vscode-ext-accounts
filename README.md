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

## Token Refresh

Saved profiles in `accounts/*.json` can be refreshed independently from the live IDE/Codex storage.

- **Refresh selected** refreshes only the saved snapshot in `accounts/*.json`.
- It does **not** rewrite `state.vscdb`, `~/.local/share/kilo/auth.json`, or `~/.codex/auth.json` until you explicitly apply the profile with **Use selected** / **Use selected Codex**.
- **Auto-refresh** runs only while the app window is open.
- Auto-refresh tracks only tokens that are still valid and will expire soon (default threshold: `10 minutes`).
- Saved profiles that already contain expired tokens are shown as `expired` in red and are skipped by auto-refresh.
- You can still try **Refresh selected** manually for a saved profile with expired tokens, but older refresh tokens may fail with `401` / `invalid_grant`.
- If upstream returns a terminal auth error (`invalid_grant`, `already been used`, `revoked`, `sign in again`, etc.), auto-refresh is disabled for that refresh-token group until app restart.
- Console logs use `[manual-refresh]` and `[auto-refresh]` prefixes.

## Requirements

```bash
pip install cryptography
```

## GUI

```bash
python main.py
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
- **Refresh selected** — refresh the saved IDE snapshot in `accounts/*.json`
- **Delete** — remove a saved IDE account
- **Refresh** — reload current state and saved accounts
- **Full backup** — create a real ZIP snapshot of the app storages (`state.vscdb`, `Local State`, Kilo New auth, Codex auth)

The **Active** column shows where each account is currently applied: `VS` (VSCode), `AG` (Antigravity), `KN` (Kilo New).
The **Expires** column shows `expired` in red when a saved profile already contains an expired token.

The **Codex** tab is separate because Codex stores its token set in `~/.codex/auth.json` and requires `id_token`.

The **Codex** tab provides:
- **Save current Codex** — save the current `~/.codex/auth.json`
- **Import Codex auth** — import another Codex auth file into saved accounts
- **Use selected Codex** — write a saved Codex account to `~/.codex/auth.json`
- **Refresh selected** — refresh the saved Codex snapshot in `accounts/*.json`
- **Delete** — remove a saved Codex account
- **Refresh** — reload current Codex state and saved accounts

### Notes

- Choose **VSCode** or **Antigravity** at the top of **IDE Accounts**.
- Tick one or more extension checkboxes before **Save current** or **Use selected**.
- The target IDE must stay closed while **Use selected** is applying changes.
- Saved accounts are stored in the local `accounts/` directory.
- Before the app writes to IDE/Kilo New/Codex storage, it creates an automatic pre-write ZIP backup of the affected files.
- `Full backup` warns only when required files for the current IDE are missing, reports other absent storages as skipped/optional, and fails if no target files exist at all.
- Auto-refresh does not touch saved profiles that already contain expired tokens.
- Manual refresh updates only the saved profile until you apply it back with **Use selected** / **Use selected Codex**.

`Kilo New` always reads from and writes to `~/.local/share/kilo/auth.json`, and that file is used by Kilo New in both VSCode and Antigravity.

Codex is intentionally isolated from IDE account switching. `IDE -> Codex` import/apply is not supported.

`parse_vscdb.py` is now a backend module inside `src/vscode_inject/`. Launch the app with `python main.py`.

## Storage locations

| Storage | Path |
|---------|------|
| VSCode secrets | `%APPDATA%\Code\User\globalStorage\state.vscdb` |
| Antigravity secrets | `%APPDATA%\Antigravity\User\globalStorage\state.vscdb` |
| Kilo New auth | `~/.local/share/kilo/auth.json` |
| Codex auth | `~/.codex/auth.json` |
| Saved account profiles | `accounts/*.json` |

`state.vscdb` encryption key is read from `Local State` via Windows DPAPI — only works under the same Windows user.
