# tools/

## userbot_member_dump.py — дамп участников чатов

Bot API не отдаёт список участников: бот видит только тех, кто написал
хоть раз. Ночному CAS/LOLS-свипу (`cas.py`) из-за этого нечего проверять
у люркеров. Дампер решает это в обход — читает участников **личным**
аккаунтом (Telethon) и складывает по CSV на чат; дальше CSV уходит боту
документом в личку от юзера из `ADMIN_IDS`, и `handle_members_import`
раскладывает строки в `chat_members_seen` (см. changelog v5.5.0).

Скрипт **самостоятельный**: он не импортируется ботом и его зависимости
(`telethon`, `pyyaml`, `python-dotenv`) намеренно не в `pyproject.toml` —
в контейнер прода они не едут.

```bash
uv run --with telethon --with pyyaml --with python-dotenv \
    python tools/userbot_member_dump.py --dry-run   # проверка прав и счёт
uv run --with telethon --with pyyaml --with python-dotenv \
    python tools/userbot_member_dump.py             # все чаты из config.yaml
#   --resume         пропустить чаты, снятые в прошлый раз (.progress.json)
#   --only main-chat только указанные slug'и
```

### Настройка

1. `cp tools/config.example.yaml tools/config.yaml` и вписать чаты.
   Юзербот должен быть **админом** каждого чата.
2. `tools/.env` с `TG_API_ID` / `TG_API_HASH` — берутся на
   https://my.telegram.org/apps.
3. Первый запуск спросит телефон, код и пароль 2FA — появится
   `tools/<session>.session`, дальше запуск неинтерактивный.

### Что нельзя коммитить

`*.session` (это полный доступ к личному TG-аккаунту), `tools/dumps/`
(персональные данные тысяч участников) и `tools/config.yaml` (id чатов,
логин/пароль прокси) — все три в `.gitignore`. В репозитории живут
только сам скрипт и `config.example.yaml`.

### Чего скрипт не умеет

`GetParticipants` с постраничным `offset` Telegram обрезает примерно на
**10 000 участников** на чат — для чата крупнее дамп молча получится
неполным (в логе это видно как `collected/count` заметно меньше 100 %).
Обходится только перебором поисковых запросов (`ChannelParticipantsSearch`
по буквам/префиксам); сейчас это не сделано, потому что чаты меньше.
Сверяйся с итоговой строкой лога `done — N/M`.

Логи прогонов и `.progress.json` (для `--resume`) лежат в `tools/dumps/`
рядом с CSV, то есть тоже вне репозитория.

### Колонки CSV

`chat_id, user_id, username, first_name, last_name, is_bot, is_deleted,
status, dumped_at`, где `status` — `member | admin | owner | banned |
left`. Импорт в боте скипает `banned` (эти уже наказаны) и не трогает
существующие пары `(chat_id, user_id)`: реальный `last_seen` важнее даты
импорта.

## Остальное

- `run_tests.py` — раннер сюиты (см. CLAUDE.md, запускать только через него).
- `cleanup_test_data.py` — очистка тестовых данных из БД.
- `legacy/` — одноразовые кодмоды v4.8.8, не запускать.
