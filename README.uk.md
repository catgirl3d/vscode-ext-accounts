# vscode_inject

[🇬🇧 English](README.md)

Утиліта для керування акаунтами VSCode-розширень (Kilocode, Roo-Cline).
Читає та записує секрети з `state.vscdb`, підтримує розшифровку v10 AES-256-GCM (Windows DPAPI).

## Сценарії використання

**Кілька платних акаунтів ChatGPT Plus**

Є кілька платних акаунтів і потрібно перемикатися між ними в Kilocode або Roo-Cline:

1. Залогінитись на [codex.openai.com](https://codex.openai.com) акаунтом A
2. Відкрити GUI → **Import Codex** → дати ім'я (наприклад `account_a`)
3. Залогінитись в Codex акаунтом B → **Import Codex** → `account_b`
4. Закрити VSCode
5. Вибрати акаунт у списку → **Use selected** → відкрити VSCode

**Використати один логін у обох розширеннях**

Авторизувались у Roo-Cline і хочете ту саму сесію в Kilocode (або навпаки):

1. Зберегти поточний акаунт: **Save current** (вибрати **Both** або потрібне розширення)
2. Закрити VSCode
3. Вибрати збережений акаунт → встановити розширення **Kilocode** → **Use selected**

Токен автоматично переписується у правильний слот розширення, навіть якщо був збережений під іншим.

## Вимоги

```bash
pip install cryptography
```

## GUI

```bash
python gui.py
```

![VSCode Account Manager](1.png)

GUI надає:
- **Save current** — зберегти поточний акаунт з VSCode під іменем
- **Import Codex** — імпортувати OAuth-токен з `~/.codex/auth.json`
- **Use selected** — застосувати збережений акаунт (VSCode має бути закритий)
- **Delete** — видалити збережений акаунт

Перемикач **Extension** (вгорі) визначає для якого розширення зберігати/застосовувати:
- **Both** — обидва розширення одразу
- **Kilocode** — тільки `kilocode.kilo-code`
- **Roo-Cline** — тільки `rooveterinaryinc.roo-cline`

> **Cross-extension:** якщо акаунт збережено для одного розширення (наприклад Roo),
> його можна застосувати для іншого (Kilocode) — токен автоматично переписується у потрібний слот.

## CLI

### Керування акаунтами

```bash
# Зберегти поточний акаунт
python parse_vscdb.py --save-account work

# Застосувати збережений акаунт (VSCode має бути закритий)
python parse_vscdb.py --use-account work

# Список збережених акаунтів
python parse_vscdb.py --list-accounts

# Імпорт з ~/.codex/auth.json
python parse_vscdb.py --import-codex auth.json --name work
python parse_vscdb.py --import-codex auth.json --name work --ext kilocode
```

`--ext` приймає: `kilocode`, `roo-cline` (за замовчуванням — обидва).

### Бекап / Відновлення

```bash
# Бекап усіх секретів у JSON
python parse_vscdb.py --backup
python parse_vscdb.py --backup my_backup.json

# Відновити з бекапу
python parse_vscdb.py --restore my_backup.json
python parse_vscdb.py --restore my_backup.json --key openai-codex-oauth-credentials
```

### Перегляд

```bash
# Вивести всі знайдені секрети в термінал
python parse_vscdb.py

# Експорт ключів за патерном
python parse_vscdb.py --get openai-codex-oauth-credentials
python parse_vscdb.py --get kilocode --out kilocode_profile.json
```

## Де знаходиться state.vscdb

```
%APPDATA%\Code\User\globalStorage\state.vscdb
```

Ключ шифрування береться з `%APPDATA%\Code\Local State` через Windows DPAPI — працює тільки під тим самим користувачем Windows.

## Що зберігається в state.vscdb

| Ключ | Вміст |
|------|-------|
| `openai-codex-oauth-credentials` | OAuth токени ChatGPT Codex (access + refresh) |
| `openAiApiKey` | OpenAI API key |
| `openRouterApiKey` | OpenRouter API key |
| `geminiApiKey` | Gemini API key |
| `roo_cline_config_api_config` | Конфігурація провайдерів Roo-Cline |
