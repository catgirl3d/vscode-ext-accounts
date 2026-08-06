# vscode-ext-accounts

[🇬🇧 English](README.md)

![VSCode Account Manager](1.png)

Windows GUI-утиліта для керування OpenAI OAuth-сесіями для Kilocode, Roo-Cline і Kilo New, обліковими даними OMP OpenAI, а також окремо для Codex.
Вона читає та записує дані акаунтів, що зберігаються у `state.vscdb` (AES-256-GCM через Windows DPAPI), `~/.local/share/kilo/auth.json`, `~/.omp/agent/agent.db` та `~/.codex/auth.json`.

> [!IMPORTANT]
> **Інструмент насамперед призначений для перемикання між збереженими OpenAI-акаунтами в підтримуваних клієнтах.**
> Як провайдер авторизації підтримується лише OpenAI (`auth.openai.com`).


## Сценарії використання

**Перемикання між кількома IDE-акаунтами**

Є кілька акаунтів і потрібно перемикати їх у Kilocode, Roo-Cline або Kilo New:

1. Увійти в потрібний акаунт всередині розширення.
2. Відкрити **IDE Accounts** → відмітити слоти, які потрібно зберегти → **Save current**.
3. Повторити для інших акаунтів.
4. Зазвичай закрити VSCode / Antigravity перед застосуванням.
5. Вибрати збережений акаунт → відмітити цільові слоти → **Use selected**.

**Використати один логін у обох розширеннях**

Авторизувались у Roo-Cline і хочете ту саму сесію в Kilocode (або навпаки):

1. Зберегти поточний акаунт у **IDE Accounts**.
2. Закрити IDE.
3. Вибрати збережений акаунт → відмітити **Kilocode** та/або **Roo-Cline** → **Use selected**.

Токен автоматично переписується у правильний слот, навіть якщо спочатку був збережений під іншим.

**Використати той самий акаунт у Kilo New**

Kilo New зберігає токени в `~/.local/share/kilo/auth.json` — окремий файл, не `state.vscdb`.
Цей auth-файл спільний для Kilo New незалежно від того, чи ви використовуєте його у VSCode або Antigravity.
Конвертація формату відбувається автоматично:

1. Зберегти акаунт будь-яким розширенням (наприклад **Kilocode**).
2. Закрити IDE, які зараз можуть використовувати Kilo New, або явно підтвердити experimental live-write prompt для Kilo New-only перемикання.
3. Вибрати збережений акаунт → відмітити **Kilo New** → **Use selected**.

**Керування обліковими даними OMP OpenAI**

OMP зберігає активні OpenAI OAuth-облікові дані в `~/.omp/agent/agent.db`:

1. Відкрити вкладку **OMP OpenAI**.
2. Використати **Save current**, щоб зберегти активний набір OMP, або **Import account**, щоб створити збережений набір із JSON.
3. Використати **Add to selected**, щоб додати облікові дані до вже збереженого набору.
4. Вибрати збережений набір → **Use selected**, щоб замінити активний набір OMP OpenAI у `agent.db`.

Збережений OMP-набір може містити кілька OpenAI-облікових даних. Імпорт приймає JSON-об'єкт або масив об'єктів; обов'язкові поля: `access_token` і `refresh_token`, необов'язкові: `account_id`, `email`, `expires` та `id_token` (`expires` може бути декодований із access token).

**Імпорт IDE-акаунта з JSON**

Якщо у вас уже є готовий token bundle і потрібно зберегти його напряму для IDE-слотів:

1. Відкрити **IDE Accounts**.
2. Відмітити цільові слоти.
3. Натиснути **Import account**.
4. Ввести назву акаунта.
5. Вставити в діалог JSON-об'єкт або JSON-масив з одним елементом.

Обов'язкові поля: `access_token`, `refresh_token`, `id_token` (мають бути валідними токенами OpenAI OAuth).
Необов'язкові поля: `account_id`, `expires`.

**Окреме керування Codex**

Codex не вважається IDE-extension-слотом. Для нього є окрема вкладка і окремий auth-файл:

1. Відкрити вкладку **Codex**.
2. Використати **Save current**, щоб зберегти поточний `~/.codex/auth.json`, або **Import Codex auth**, щоб імпортувати інший Codex auth-файл.
3. Вибрати збережений Codex-акаунт → **Use selected**, щоб записати його назад у `~/.codex/auth.json`.

Сценарій `IDE -> Codex` навмисно не підтримується, тому що Codex потребує `id_token`.

## Оновлення токенів

Збережені профілі в `accounts/*.json` можна оновлювати окремо від live-сховищ IDE, OMP і Codex через ендпоінт авторизації OpenAI (`https://auth.openai.com/oauth/token`).

- **Renew tokens** оновлює тільки збережений snapshot у `accounts/*.json`.
- Кнопка **не** переписує `state.vscdb`, `~/.local/share/kilo/auth.json`, `~/.omp/agent/agent.db` або `~/.codex/auth.json`, доки ви явно не застосуєте профіль через **Use selected**.
- **Auto-refresh** працює лише поки відкрите вікно застосунку.
- Auto-refresh відстежує тільки токени, які ще валідні, але скоро втратять чинність (дефолтний поріг: `2 дні`).
- Профілі, які вже прострочені, показуються як `expired` червоним кольором і пропускаються auto-refresh'ем.
- Для простроченого snapshot усе ще можна вручну натиснути **Renew tokens**, але старі refresh token часто падають з `401` / `invalid_grant`.
- Якщо upstream повертає terminal auth error (`invalid_grant`, `already been used`, `revoked`, `sign in again` тощо), auto-refresh вимикається для цієї refresh-token-group навіть після перезапуску застосунку, доки ви не оновите її вручну або не заміните збережені токени.
- Консольні логи мають префікси `[manual-refresh]` і `[auto-refresh]`.

## Ліміти використання

- Колонка **Limits** доступна у вкладках **IDE Accounts**, **OMP OpenAI** і **Codex**.
- **Fetch** оновлює вибраний збережений акаунт; **Fetch all** оновлює всі збережені акаунти поточної вкладки.
- Ліміти запитуються з OpenAI usage endpoint за допомогою access token кожного збереженого акаунта і кешуються у збереженому профілі. Автоматичного опитування лімітів немає.
- Стандартні п'ятигодинне та тижневе вікна показуються як `remaining% / remaining%` (наприклад, `16% / 95%`). Інші вікна містять свою тривалість, наприклад `95% [30d]`.
- Для OMP-профілів із кількома обліковими даними показується окремий summary для кожного. Якщо під час часткового запиту один запит завершується помилкою, попередній кешований snapshot цього облікового запису зберігається.

## Локальний запуск

Вимоги:

- Windows
- Python 3.10+
- `tkinter` (зазвичай входить до стандартної Windows-збірки Python)
- `cryptography`

Запускати з кореня репозиторію:

```bash
python -m pip install cryptography
python main.py
```

`main.py` додає `src/` у `sys.path` і запускає Tk GUI.

У застосунку є три вкладки: **IDE Accounts**, **OMP OpenAI** і **Codex**.

Перемикач **IDE** вгорі визначає, яку IDE GUI показує і куди застосовує зміни (VSCode / Antigravity).

Вкладка **IDE Accounts** використовує галочки extension-слотів, щоб визначити що саме читати або записувати:
- **Kilocode** — тільки `kilocode.kilo-code` (`state.vscdb`)
- **Roo-Cline** — тільки `rooveterinaryinc.roo-cline` (`state.vscdb`)
- **Kilo New** — `~/.local/share/kilo/auth.json` (спільна Kilo New авторизація, не `state.vscdb`)

Вкладка **IDE Accounts** надає:
- **Use selected** — застосувати збережений IDE-акаунт до відмічених цілей
- **Save current** — зберегти поточний стан акаунтів для вибраних IDE/Kilo New слотів
- **Import account** — відкрити діалог і імпортувати IDE-акаунт із вставленого JSON
- **Export** — експортувати виділений збережений IDE-акаунт у JSON в обраному форматі
- **Fetch** — отримати ліміти використання для вибраного збереженого IDE-акаунта
- **Fetch all** — отримати ліміти використання для всіх збережених IDE-акаунтів
- **Renew tokens** — оновити збережений IDE-snapshot у `accounts/*.json`
- **Rename** — перейменувати збережений IDE-акаунт
- **Delete** — видалити збережений IDE-акаунт
- **Reload** — перечитати поточний стан і список збережених акаунтів без зміни токенів
- **Full backup** — створити справжній ZIP-знімок сховищ застосунку (`state.vscdb`, `Local State`, Kilo New auth, OMP `agent.db`, Codex auth)

`Import account` очікує JSON-об'єкт або масив з одним елементом. Обов'язкові поля: `access_token`, `refresh_token`, `id_token`. Необов'язкові поля: `account_id`, `expires`.

`Export` відкриває діалог для експорту виділеного IDE-акаунта в один із 5 підтримуваних форматів: `Full tokens` (формат Agent Identity / auth.json), `Session JSON (Sub2API)`, `accessToken only`, `personal_access_token` або `refresh_token only`. Результат можна скопіювати в буфер обміну або зберегти у `.json` файл.

Колонка **Active** показує де акаунт зараз активний: `VS` (VSCode), `AG` (Antigravity), `KN` (Kilo New).
Колонка **Expires** показує `expired` червоним кольором, якщо збережений snapshot уже прострочений.

Вкладка **Codex** винесена окремо, тому що Codex зберігає токени в `~/.codex/auth.json` і потребує `id_token`.

Вкладка **Codex** надає:
- **Use selected** — записати збережений Codex-акаунт у `~/.codex/auth.json`
- **Save current** — зберегти поточний `~/.codex/auth.json`
- **Import Codex auth** — імпортувати інший Codex auth-файл у список збережених акаунтів
- **Fetch** — отримати ліміти використання для вибраного збереженого Codex-акаунта
- **Fetch all** — отримати ліміти використання для всіх збережених Codex-акаунтів
- **Renew tokens** — оновити збережений Codex-snapshot у `accounts/*.json`
- **Rename** — перейменувати збережений Codex-акаунт
- **Delete** — видалити збережений Codex-акаунт
- **Reload** — перечитати поточний Codex-стан і список збережених акаунтів без зміни токенів

Вкладка **OMP OpenAI** надає:
- **Use selected** — замінити активний набір OMP OpenAI у `~/.omp/agent/agent.db`
- **Save current** — зберегти активний набір OMP OpenAI
- **Import account** — створити збережений OMP-набір з одного або кількох JSON-облікових даних
- **Add to selected** — додати вставлені облікові дані до вибраного збереженого OMP-набору
- **Fetch** — отримати ліміти використання для вибраного збереженого OMP-набору
- **Fetch all** — отримати ліміти використання для всіх збережених OMP-наборів
- **Renew tokens** — оновити збережений OMP-snapshot у `accounts/*.json`
- **Rename** — перейменувати збережений OMP-набір
- **Delete** — видалити збережений OMP-набір
- **Reload** — перечитати поточний OMP-стан і збережені набори без зміни токенів

### Нотатки

- Обирайте **VSCode** або **Antigravity** у верхній частині вкладки **IDE Accounts**.
- Перед **Save current** або **Use selected** потрібно відмітити хоча б одну галочку extension-слоту.
- Цільова IDE зазвичай має залишатися закритою під час застосування через **Use selected**.
- Якщо ви перемикаєте тільки спільну авторизацію **Kilo New**, GUI може запропонувати experimental live-write confirmation замість обов'язкового закриття IDE.
- Збережені акаунти лежать у локальній директорії `accounts/`.
- Перед записом у IDE/Kilo New/Codex застосунок автоматично створює ZIP-бекап файлів, які будуть змінені.
- Перед записом в OMP-сховище застосунок автоматично створює ZIP-бекап `agent.db` і наявних SQLite-файлів WAL/SHM.
- `Full backup` показує warning лише коли відсутні required-файли поточної IDE, інші відсутні сховища рахує як skipped/optional, і падає, якщо не існує жодного target-файлу.
- Auto-refresh не чіпає saved snapshots, які вже прострочені.
- **Renew tokens** оновлює тільки збережений профіль, доки ви не застосуєте його назад через **Use selected**.
- Ліміти оновлюються лише через **Fetch** / **Fetch all** і залишаються кешованими у збереженому профілі до наступного успішного запиту.

`Kilo New` завжди читається і записується через `~/.local/share/kilo/auth.json`, і цей файл використовується Kilo New як у VSCode, так і в Antigravity.

OMP OpenAI завжди читає і записує `~/.omp/agent/agent.db`. Застосування збереженого OMP-набору замінює активний набір OpenAI-облікових даних у цій базі.

Codex навмисно ізольований від IDE-перемикань. Сценарій `IDE -> Codex` не підтримується.

## Місця зберігання

| Сховище | Шлях |
|---------|------|
| VSCode секрети | `%APPDATA%\Code\User\globalStorage\state.vscdb` |
| Antigravity секрети | `%APPDATA%\Antigravity\User\globalStorage\state.vscdb` |
| Kilo New авторизація | `~/.local/share/kilo/auth.json` |
| OMP OpenAI база | `~/.omp/agent/agent.db` (і необов'язкові файли `-wal` / `-shm`) |
| Codex авторизація | `~/.codex/auth.json` |
| Збережені профілі акаунтів | `accounts/*.json` |

Ключ шифрування `state.vscdb` береться з `Local State` через Windows DPAPI — працює тільки під тим самим користувачем Windows.
