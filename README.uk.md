# vscode-ext-accounts

[🇬🇧 English](README.md)

Утиліта для керування акаунтами розширень VSCode/Antigravity (Kilocode, Roo-Cline, Kilo New).
Вона читає та записує дані акаунтів, що зберігаються у `state.vscdb` (AES-256-GCM через Windows DPAPI) та `~/.local/share/kilo/auth.json`.

## Сценарії використання

**Кілька платних акаунтів ChatGPT Plus**

Є кілька платних акаунтів і потрібно перемикатися між ними в Kilocode або Roo-Cline:

1. Залогінитись на [codex.openai.com](https://codex.openai.com) акаунтом A
2. Відкрити GUI → **Import Codex** → дати ім'я (наприклад `account_a`)
3. Залогінитись в Codex акаунтом B → **Import Codex** → `account_b`
4. Закрити VSCode / Antigravity
5. Вибрати акаунт у списку → **Use selected** → знову відкрити IDE

**Використати один логін у обох розширеннях**

Авторизувались у Roo-Cline і хочете ту саму сесію в Kilocode (або навпаки):

1. Зберегти поточний акаунт: **Save current** (вибрати **Both** або потрібне розширення)
2. Закрити IDE
3. Вибрати збережений акаунт → встановити розширення **Kilocode** → **Use selected**

Токен автоматично переписується у правильний слот, навіть якщо спочатку був збережений під іншим.

**Використати той самий акаунт у Kilo New (Antigravity)**

Kilo New зберігає токени в `~/.local/share/kilo/auth.json` — окремий файл, не `state.vscdb`.
Конвертація формату відбувається автоматично:

1. Зберегти акаунт будь-яким розширенням (наприклад **Kilocode**)
2. Закрити Antigravity
3. Вибрати збережений акаунт → встановити розширення **Kilo New** → **Use selected**

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
- **Save current** — зберегти поточний акаунт
- **Import Codex** — імпортувати OAuth-токен з `~/.codex/auth.json`
- **Use selected** — застосувати збережений акаунт (обрана IDE має бути закрита)
- **Delete** — видалити збережений акаунт

Перемикач **IDE** вгорі визначає, яку IDE GUI показує і куди застосовує зміни (VSCode / Antigravity).

Вкладка **IDE Accounts** використовує галочки extension-слотів, щоб визначити що саме читати або записувати:
- **Kilocode** — тільки `kilocode.kilo-code` (`state.vscdb`)
- **Roo-Cline** — тільки `rooveterinaryinc.roo-cline` (`state.vscdb`)
- **Kilo New** — `~/.local/share/kilo/auth.json` (новий движок Kilocode всередині Antigravity)

Колонка **Active** показує де акаунт зараз активний: `VS` (VSCode), `AG` (Antigravity), `KN` (Kilo New).

Вкладка **Codex** винесена окремо, тому що Codex зберігає токени в `~/.codex/auth.json` і потребує `id_token`.

## Робота через GUI

```bash
python gui.py
```

### Вкладка IDE Accounts

- Оберіть **VSCode** або **Antigravity** зверху.
- Відмітьте одну чи кілька галочок: **Kilocode**, **Roo-Cline**, **Kilo New**.
- **Save current** зберігає поточний активний стан акаунтів під заданим ім'ям.
- **Use selected** застосовує вибраний збережений акаунт до відмічених слотів.
- **Full backup** створює JSON-бекап знайдених секретів для вибраного IDE-сховища.

`Kilo New` завжди читається і записується через `~/.local/share/kilo/auth.json`, хоча керується з IDE-вкладки.

### Вкладка Codex

- **Save current Codex** зберігає поточний `~/.codex/auth.json` як окремий акаунт.
- **Import Codex auth** імпортує існуючий Codex `auth.json` у список збережених акаунтів.
- **Use selected Codex** записує збережений Codex-акаунт назад у `~/.codex/auth.json`.

Codex навмисно ізольований від IDE-перемикань. Сценарій `IDE -> Codex` не підтримується.

`parse_vscdb.py` тепер є backend-модулем для GUI. Використовуйте `python gui.py`.

Якщо запустити `python parse_vscdb.py` напряму, скрипт одразу завершиться коротким повідомленням про GUI-only режим.

## Місця зберігання

| Сховище | Шлях |
|---------|------|
| VSCode секрети | `%APPDATA%\Code\User\globalStorage\state.vscdb` |
| Antigravity секрети | `%APPDATA%\Antigravity\User\globalStorage\state.vscdb` |
| Kilo New авторизація | `~/.local/share/kilo/auth.json` |

Ключ шифрування `state.vscdb` береться з `Local State` через Windows DPAPI — працює тільки під тим самим користувачем Windows.

## Що зберігається в state.vscdb

| Ключ | Вміст |
|------|-------|
| `openai-codex-oauth-credentials` | OAuth токени ChatGPT Codex (access + refresh) |
| `openAiApiKey` | OpenAI API key |
| `openRouterApiKey` | OpenRouter API key |
| `geminiApiKey` | Gemini API key |
| `roo_cline_config_api_config` | Конфігурація провайдерів Roo-Cline |
