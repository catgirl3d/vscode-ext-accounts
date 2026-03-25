# vscode_inject

Утилита для управления аккаунтами VSCode-расширений (Kilocode, Roo-Cline).
Читает и пишет секреты из `state.vscdb`, поддерживает расшифровку v10 AES-256-GCM (Windows DPAPI).

## Требования

```bash
pip install cryptography
```

## Запуск GUI

```bash
python gui.py
```

GUI позволяет:
- **Save current** — сохранить текущий аккаунт из VSCode под именем
- **Import Codex** — импортировать OAuth-токен из `~/.codex/auth.json`
- **Use selected** — применить сохранённый аккаунт (VSCode должен быть закрыт)
- **Delete** — удалить сохранённый аккаунт

Переключатель **Extension** (вверху) задаёт для какого расширения сохранять/применять:
- **Both** — оба расширения сразу
- **Kilocode** — только `kilocode.kilo-code`
- **Roo-Cline** — только `rooveterinaryinc.roo-cline`

> **Cross-extension:** если аккаунт сохранён для одного расширения (например Roo),
> его можно применить для другого (Kilocode) — токен будет автоматически переписан в нужный слот.

## CLI

### Аккаунты

```bash
# Сохранить текущий аккаунт
python parse_vscdb.py --save-account work

# Применить сохранённый аккаунт (VSCode должен быть закрыт)
python parse_vscdb.py --use-account work

# Список сохранённых аккаунтов
python parse_vscdb.py --list-accounts

# Импорт из ~/.codex/auth.json
python parse_vscdb.py --import-codex auth.json --name work
python parse_vscdb.py --import-codex auth.json --name work --ext kilocode
```

`--ext` принимает: `kilocode`, `roo-cline` (по умолчанию — оба).

### Бэкап / Restore

```bash
# Бэкап всех секретов в JSON
python parse_vscdb.py --backup
python parse_vscdb.py --backup my_backup.json

# Восстановить из бэкапа
python parse_vscdb.py --restore my_backup.json
python parse_vscdb.py --restore my_backup.json --key openai-codex-oauth-credentials
```

### Просмотр

```bash
# Вывод всех найденных секретов в терминал
python parse_vscdb.py

# Экспорт ключей по паттерну
python parse_vscdb.py --get openai-codex-oauth-credentials
python parse_vscdb.py --get kilocode --out kilocode_profile.json
```

## Где лежит state.vscdb

```
%APPDATA%\Code\User\globalStorage\state.vscdb
```

Ключ шифрования берётся из `%APPDATA%\Code\Local State` через Windows DPAPI — работает только под тем же пользователем Windows.

## Что хранится в state.vscdb

| Ключ | Содержимое |
|------|-----------|
| `openai-codex-oauth-credentials` | OAuth токены ChatGPT Codex (access + refresh) |
| `openAiApiKey` | OpenAI API key |
| `openRouterApiKey` | OpenRouter API key |
| `geminiApiKey` | Gemini API key |
| `roo_cline_config_api_config` | Конфигурация провайдеров Roo-Cline |
