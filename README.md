# vscode-ext-accounts

[🇺🇦 Українська](README.uk.md)

A utility for managing VSCode/Antigravity extension accounts (Kilocode, Roo-Cline, Kilo New).
It reads and writes account data stored in `state.vscdb` (AES-256-GCM via Windows DPAPI) and `~/.local/share/kilo/auth.json`.

## Use Cases

**Multiple ChatGPT Plus accounts**

You have several paid accounts and want to switch between them in Kilocode or Roo-Cline:

1. Log into [codex.openai.com](https://codex.openai.com) with account A
2. Open GUI → **Import Codex** → give it a name (e.g. `account_a`)
3. Log into Codex with account B → **Import Codex** → `account_b`
4. Close VSCode / Antigravity
5. Select the account in the list → **Use selected** → open the IDE again

**Use one login for both extensions**

You authenticated in Roo-Cline and want the same session in Kilocode (or vice versa):

1. Save the current account: **Save current** (select **Both** or the target extension)
2. Close the IDE
3. Select the saved account → set extension to **Kilocode** → **Use selected**

The token is automatically remapped to the correct extension slot, even if it was originally saved under a different one.

**Use the same account in Kilo New (Antigravity)**

Kilo New stores tokens in `~/.local/share/kilo/auth.json` — a completely separate file from `state.vscdb`.
The tool handles format conversion automatically:

1. Save the account with any extension (e.g. **Kilocode**)
2. Close Antigravity
3. Select the saved account → set extension to **Kilo New** → **Use selected**

## Requirements

```bash
pip install cryptography
```

## GUI

```bash
python gui.py
```

![VSCode Account Manager](1.png)

The GUI provides:
- **Save current** — save the active account
- **Import Codex** — import an OAuth token from `~/.codex/auth.json`
- **Use selected** — apply a saved account (the selected IDE must be closed)
- **Delete** — remove a saved account

The **IDE** selector at the top chooses which IDE the GUI shows and targets (VSCode / Antigravity).

The **IDE Accounts** tab uses extension checkboxes to control which IDE slots are read or written:
- **Kilocode** — only `kilocode.kilo-code` (`state.vscdb`)
- **Roo-Cline** — only `rooveterinaryinc.roo-cline` (`state.vscdb`)
- **Kilo New** — `~/.local/share/kilo/auth.json` (new Kilocode engine inside Antigravity)

The **Active** column shows where each account is currently applied: `VS` (VSCode), `AG` (Antigravity), `KN` (Kilo New).

The **Codex** tab is separate because Codex stores its token set in `~/.codex/auth.json` and requires `id_token`.

## GUI Workflow

```bash
python gui.py
```

### IDE Accounts tab

- Choose **VSCode** or **Antigravity** at the top.
- Tick one or more extension checkboxes: **Kilocode**, **Roo-Cline**, **Kilo New**.
- Use **Save current** to store the currently active account state under a name.
- Use **Use selected** to apply a saved account to the checked targets.
- Use **Full backup** to create a JSON backup of matched secrets from the selected IDE storage.

`Kilo New` always reads from and writes to `~/.local/share/kilo/auth.json`, even though it is controlled from the IDE tab.

### Codex tab

- **Save current Codex** saves the current `~/.codex/auth.json` as a named account.
- **Import Codex auth** imports an existing Codex `auth.json` into the saved account list.
- **Use selected Codex** writes a saved Codex account back to `~/.codex/auth.json`.

Codex is intentionally isolated from IDE account switching. `IDE -> Codex` import/apply is not supported.

`parse_vscdb.py` is now a backend module for the GUI. Run `python gui.py` instead.

If you launch `python parse_vscdb.py` directly, it exits immediately with a short GUI-only message.

## Storage locations

| Storage | Path |
|---------|------|
| VSCode secrets | `%APPDATA%\Code\User\globalStorage\state.vscdb` |
| Antigravity secrets | `%APPDATA%\Antigravity\User\globalStorage\state.vscdb` |
| Kilo New auth | `~/.local/share/kilo/auth.json` |

`state.vscdb` encryption key is read from `Local State` via Windows DPAPI — only works under the same Windows user.

## Keys stored in state.vscdb

| Key | Contents |
|-----|---------|
| `openai-codex-oauth-credentials` | ChatGPT Codex OAuth tokens (access + refresh) |
| `openAiApiKey` | OpenAI API key |
| `openRouterApiKey` | OpenRouter API key |
| `geminiApiKey` | Gemini API key |
| `roo_cline_config_api_config` | Roo-Cline provider configuration |
