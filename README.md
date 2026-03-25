# vscode-ext-accounts

[🇺🇦 Українська](README.uk.md)

A utility for managing VSCode extension accounts (Kilocode, Roo-Cline).
Reads and writes secrets from `state.vscdb` with v10 AES-256-GCM decryption (Windows DPAPI).

## Use Cases

**Multiple ChatGPT Plus accounts**

You have several paid accounts and want to switch between them in Kilocode or Roo-Cline:

1. Log into [codex.openai.com](https://codex.openai.com) with account A
2. Open GUI → **Import Codex** → give it a name (e.g. `account_a`)
3. Log into Codex with account B → **Import Codex** → `account_b`
4. Close VSCode
5. Select the account in the list → **Use selected** → open VSCode

**Use one login for both extensions**

You authenticated in Roo-Cline and want the same session in Kilocode (or vice versa):

1. Save the current account: **Save current** (select **Both** or the target extension)
2. Close VSCode
3. Select the saved account → set extension to **Kilocode** → **Use selected**

The token is automatically remapped to the correct extension slot even if it was originally saved under a different one.

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
- **Save current** — save the active VSCode account under a name
- **Import Codex** — import an OAuth token from `~/.codex/auth.json`
- **Use selected** — apply a saved account (VSCode must be closed)
- **Delete** — remove a saved account

The **Extension** selector at the top controls which extension to target:
- **Both** — both extensions at once
- **Kilocode** — only `kilocode.kilo-code`
- **Roo-Cline** — only `rooveterinaryinc.roo-cline`

> **Cross-extension:** if an account was saved for one extension (e.g. Roo),
> it can be applied to another (Kilocode) — the token is automatically remapped to the correct slot.

## CLI

### Account management

```bash
# Save current account
python parse_vscdb.py --save-account work

# Apply a saved account (VSCode must be closed)
python parse_vscdb.py --use-account work

# List saved accounts
python parse_vscdb.py --list-accounts

# Import from ~/.codex/auth.json
python parse_vscdb.py --import-codex auth.json --name work
python parse_vscdb.py --import-codex auth.json --name work --ext kilocode
```

`--ext` accepts: `kilocode`, `roo-cline` (default: both).

### Backup / Restore

```bash
# Backup all secrets to JSON
python parse_vscdb.py --backup
python parse_vscdb.py --backup my_backup.json

# Restore from backup
python parse_vscdb.py --restore my_backup.json
python parse_vscdb.py --restore my_backup.json --key openai-codex-oauth-credentials
```

### Inspect

```bash
# Print all found secrets to terminal
python parse_vscdb.py

# Export keys matching a pattern
python parse_vscdb.py --get openai-codex-oauth-credentials
python parse_vscdb.py --get kilocode --out kilocode_profile.json
```

## Location of state.vscdb

```
%APPDATA%\Code\User\globalStorage\state.vscdb
```

The encryption key is read from `%APPDATA%\Code\Local State` via Windows DPAPI — only works under the same Windows user.

## Keys stored in state.vscdb

| Key | Contents |
|-----|---------|
| `openai-codex-oauth-credentials` | ChatGPT Codex OAuth tokens (access + refresh) |
| `openAiApiKey` | OpenAI API key |
| `openRouterApiKey` | OpenRouter API key |
| `geminiApiKey` | Gemini API key |
| `roo_cline_config_api_config` | Roo-Cline provider configuration |
