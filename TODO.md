# Дедушка Вобжак — TODO / Что осталось доделать

## Статус проекта
**Версия**: v4.4.3 (рабочий бот с per-chat report_chat_id, мульти-админ веб-панель с привязкой к Telegram ID, автообновление, стелс-режим, Rich Messages с кликабельным нарушителем/модератором/веб-ссылкой, welcome-сообщение новому админу в ЛС с паролем под спойлером, **управление модераторами чатов через веб-панель (команды /addadmin, /deladmin — как fallback)**, Ephemeral-подтверждения модератору, стикеры в отчётах, команды !unwarn/!unban, удаление сообщения при !warn, self-service смена пароля)
**Aiogram**: 3.30.0 (поддерживает Bot API 10.2: `send_rich_message`, `receiver_user_id`)
**Архитектура репорт-чата**: per-chat override → default (chat_id=0) → disabled
**Стелс-режим**: нарушитель НИКОГДА не получает уведомлений от бота; ephemeral видят только модераторы
**Санкции**: !mute / !warn / !ban / !unmute / !unban / !unwarn [N] / !warns / !resetwarns
**Веб-панель**: SU (env WEB_PASSWORD) + мульти-админ (PBKDF2), автообновление каждые 15с, фильтры (action/revoked/sort), REVOKED-бейджи
**v4.4 web-админы**: создаются SU по TGID, профиль подтягивается из Telegram (`bot.get_chat`), пароль автогенерируется и показывается SU один раз, юзер сам меняет пароль через /dashboard
**v4.4.3 модераторы**: SU может добавлять/удалять модераторов чатов через `/admin/moderators` (SU-only). Команды `/addadmin`, `/deladmin` в боте остаются как fallback. Профиль модератора (имя, @username) подтягивается через `bot.get_chat` best-effort.

---

## ✅ Готово — v4.4 (привязка веб-юзеров к Telegram ID)

### 18 ✅ Создание админов через TGID с автозаполнением профиля

**Проблема v4.3**: при создании веб-админа SU приходилось вручную придумывать username и пароль, а потом передавать пароль юзеру по независимому каналу. Профиль админа (имя, @username) никак не был связан с Telegram, что приводило к путанице.

**Что сделано в `db.py`**:
- В модель `WebUser` добавлены 4 новые колонки:
  | Колонка | Тип | Назначение |
  |---------|-----|------------|
  | `tg_user_id` | BigInteger, nullable, UNIQUE | Telegram ID привязанного юзера (NULL для SU) |
  | `tg_first_name` | String(255), nullable | Имя из Telegram (snapshot на момент создания) |
  | `tg_last_name` | String(255), nullable | Фамилия из Telegram |
  | `tg_username` | String(255), nullable | @username из TG (без @, lowercase) |
- В `init_db()` добавлена идемпотентная миграция: 4 `ALTER TABLE` + `CREATE UNIQUE INDEX IF NOT EXISTS ix_web_users_tg_user_id` (partial index — `WHERE tg_user_id IS NOT NULL`, чтобы несколько записей могли иметь NULL).
- Логин `username` в WebUser = `@username` из TG (без @, lowercase). Это синхронно с полем `tg_username`.

**Что сделано в `web_app.py`**:
- `create_app(lifespan=None, bot=None)` — функция теперь принимает экземпляр `aiogram.Bot`. Он нужен эндпоинту создания, чтобы дёргать `bot.get_chat(user_id)`.
- Добавлены хелперы:
  - `_generate_password()` — `secrets.token_urlsafe(12)[:16]`, 16 chars base64url (~107 бит энтропии).
  - `_sign_flash(payload)` / `_verify_flash(token, max_age_seconds)` — HMAC-подписанный short-lived токен для передачи пароля в query string. Payload = `{u, p, tg, t}`, `t` — unix time. TTL по умолчанию 180 сек.
- `POST /admin/users/create` переписан:
  - Принимает единственное поле `tg_user_id` (число).
  - Валидирует TGID (положительное целое).
  - Дёргает `bot.get_chat(chat_id=tg_id)`. Если падает (юзер не общался с ботом) — flash с понятной подсказкой.
  - Извлекает `username`, `first_name`, `last_name` из Chat-объекта.
  - Если у юзера нет `@username` — отказ (логин не из чего строить).
  - Порядок проверок: `su` reserved → длина логина 5-32 → дубликат TGID → дубликат логина.
  - Генерирует пароль, создаёт `WebUser`, редиректит на `/admin/users?created=<signed_flash>`.
- `GET /admin/users` теперь принимает query-параметр `created`. Если токен валиден и свеж (<180 сек) — в контекст шаблона передаётся `created_info` с `{username, password, tg_user_id}`.
- Старый эндпоинт с ручным вводом `username`+`password` удалён.

**Что сделано в `bot.py`**:
- `app = create_app(lifespan=lifespan, bot=bot)` — бот прокинут в веб-приложение.

**Что сделано в шаблонах**:
- `templates/admin.html` полностью переписан:
  - Форма Create содержит одно поле `tg_user_id` (pattern `[0-9]+`).
  - После создания (если есть `created_info`) показывается зелёный блок с предупреждением "shown only once", таблицей (Login / Telegram ID / Password) и кнопкой copy-to-clipboard.
  - В таблице Existing admins добавлена колонка **Telegram** с именем, @username (ссылка на `t.me/<username>`) и TGID.
  - Кнопка Delete теперь подтверждается диалогом с пояснением "Telegram ID will be unbound and can be reused".
- `templates/dashboard.html`: внизу добавлен блок **Change my password** с формой (current / new / confirm) → `POST /me/password`. Для SU вместо формы — предупреждение "SU password is managed via WEB_PASSWORD env".
- `templates/login.html`: обновлена подсказка — "Admins: login = your @username from Telegram (without @)".

### 19 ✅ Self-service смена пароля (`POST /me/password`)

**Что сделано в `web_app.py`**:
- `POST /me/password` — доступен всем авторизованным юзерам. Принимает `old_password`, `new_password`, `confirm`.
- Логика:
  - SU → отказ ("SU password is managed via WEB_PASSWORD env").
  - `len(new_password) < 6` → flash "at least 6 chars".
  - `new_password != confirm` → flash "do not match".
  - `_verify_password(old, stored) == False` → flash "Current password is incorrect".
  - `_verify_password(new, stored) == True` (новый = старый) → flash "must differ".
  - Иначе — обновление `password_hash`, flash "Password changed successfully".
- Все редиректы идут на `/dashboard?pw_msg=<flash>`, который отображается в блоке Change my password.
- Логирование: `_req_logger.info("me_change_password: user=%s changed own password", ...)` — без записи самого пароля.

### 20 ✅ Удаление админов с очисткой привязки TGID

**Что сделано**:
- `POST /admin/users/{id}/delete` — без изменений в логике (SU удалить нельзя, остальные — `session.delete(wu)`).
- Поскольку `tg_user_id` — это колонка в `web_users`, при удалении строки привязка очищается автоматически. TGID становится доступен для повторного использования.
- Тест 7c (`test_v44_tgid_create.py`) явно проверяет: после удаления юзера с TGID=7770001, SU может создать нового админа с тем же TGID — флэша "already bound" не возникает.

### 21 ✅ Тесты v4.4

**`scripts/test_v44_tgid_create.py`** — 82 проверки, 8 секций:
1. DB-миграция: 4 новые колонки + уникальный индекс + идемпотентность + SU-seed.
2. Уникальность `tg_user_id` (дубликат → IntegrityError, NULL разрешён многократно).
3. `_generate_password`: длина 16, разные вызовы разные, base64url-безопасные символы, без padding.
4. `_sign_flash` / `_verify_flash`: round-trip, tamper (sig), expired (max_age), garbage input.
5. `POST /admin/users/create`: успешный путь, дубликат TGID, нет @username, get_chat упал, нечисловой TGID, отрицательный TGID, @username='su' reserved, второй админ, GET с created-токеном показывает пароль, GET с мусорным токеном не падает, non-SU rejected.
6. `POST /me/password`: неверный старый, new≠confirm, короткий новый, new=old, успешная смена, old больше не работает для логина, new работает, SU rejected, GET /dashboard?pw_msg= отображает, блок виден не-SU, SU видит предупреждение.
7. `POST /admin/users/{id}/delete`: удаление + проверка что row gone, SU не удаляется, повторное использование TGID после удаления.
8. `create_app(bot=None)` → flash "Bot instance not available".

**Результат**: 82/82 ✅.

### Технические заметки v4.4

**Почему `bot.get_chat(user_id)`, а не `bot.get_chat_member(chat_id, user_id)`?**
`get_chat` для приватного чата с `chat_id=user_id` возвращает `Chat` объект с `first_name`, `last_name`, `username`. Это работает только если юзер уже хоть раз писал боту (или состоит в чате, где бот админ). `get_chat_member` требует конкретный `chat_id` группы — не подходит для произвольного юзера.

**Стелс-режим сохранён**: бот не отправляет новому админу никаких сообщений (ни пароля, ни уведомления "вас добавили"). Пароль показывается только SU один раз, дальше передаётся юзеру по независимому каналу. Бот остаётся невидимым для юзеров, не являющихся модераторами чата.

**Почему signed-flash, а не сессионная переменная?**
Сессия у нас stateless — кука с HMAC, в ней только `{username, is_su, ts, nonce}`. Добавлять server-side session store ради одной фичи — overkill. Signed-flash в query string решает задачу: подпись HMAC гарантирует что SU не подделает пароль, TTL 180 сек гарантирует что ссылка не протухнет в истории браузера.

**Почему `tg_username` хранится отдельно от `username`?**
Сейчас они синхронны (`username` = `tg_username` = lowercase без @). Но в будущем @username в TG может поменяться (юзер сменил ник), и `tg_username` будет хранить snapshot, а `username` останется логином (не меняем, чтобы не сломать сессии). Сейчас это избыточно, но задел на будущее.

**Файловая структура v4.4** (без изменений относительно v4.3, только правки):
```
shadow-logger/
├── .dockerignore
├── .env.example
├── Dockerfile
├── requirements.txt          # aiogram==3.30.0 (без изменений)
├── bot.py                    # v4.4: create_app(lifespan=..., bot=bot)
├── bot_handlers.py           # без изменений
├── db.py                     # v4.4: 4 новые колонки в WebUser + миграция
├── web_app.py                # v4.4: create_app(bot=...), POST /admin/users/create по TGID, POST /me/password
├── templates/
│   ├── base.html             # без изменений
│   ├── dashboard.html        # v4.4: блок Change my password
│   ├── login.html            # v4.4: обновлённая подсказка про @username
│   ├── user.html             # без изменений
│   └── admin.html            # v4.4: переписан под TGID + блок с паролем
└── scripts/
    └── test_v44_tgid_create.py  # 82 теста
```

---

## ✅ Готово — v4.4.1 (Rich-отчёты: кликабельный нарушитель + модератор + веб-ссылка)

### 22 ✅ Кликабельное имя нарушителя в Rich-отчёте (`tg://user?id=…`)

**Проблема**: в v4.4 в репорт-чате нарушитель отображался как `👤 Иван Петров\n   @ivan_p\n   ID: 123456789` — голый текст. Чтобы открыть профиль нарушителя в Telegram, модератору приходилось копировать ID и вставлять его в `tg://user?id=` вручную. Особенно больно, когда у нарушителя **нет @username** — тогда кликабельной ссылки не было вообще.

**Что сделано в `bot_handlers.py`**:
- В импортах добавлены `RichTextUrl` и `RichTextBold` из `aiogram.types`.
- В `_send_report()` блок «Нарушитель» теперь строится как `list[str | RichTextUrl]`, а не как plain string:
  ```python
  offender_text = [
      "👤 ",
      RichTextUrl(text=display_name, url=f"tg://user?id={target.id}"),
  ]
  if target.username:
      offender_text.append(f"\n   @{target.username}")
  offender_text.append(f"\n   ID: {target.id}")
  ```
- `InputRichBlockParagraph(text=offender_text)` — Rich Messages принимают list[RichTextUnion], aiogram 3.30 корректно сериализует это в JSON для Bot API 10.2.
- Клик по имени нарушителя в репорт-чате открывает его профиль в Telegram — работает **даже если @username отсутствует** (это было главным требованием задачи).
- Добавлен хелпер `_user_display_name(user) -> str`: возвращает `"Имя Фамилия"` или `"id:<user_id>"` (без HTML-экранирования, для RichText).

### 23 ✅ Блок «Модератор» — кто применил санкцию

**Проблема**: в v4.4 в репорт-чате не было информации о том, **кто именно** из модераторов забанил/замьютил/выдал варн. Это затрудняло разбор спорных санкций.

**Что сделано в `bot_handlers.py`**:
- В `_send_report()` добавлен новый параметр `mod: types.User | None = None`.
- Если `mod` передан — после блока нарушителя добавляется новый Paragraph:
  ```python
  InputRichBlockParagraph(text=[
      "👮 Модератор: ",
      RichTextUrl(text=mod_name, url=f"tg://user?id={mod.id}"),
  ])
  ```
- Имя модератора кликабельно — открывает его профиль в Telegram.
- Обновлены **все 8 вызывающих мест** `_send_report` (7 ручных команд + 2 автосанкции в `_check_warn_threshold`): везде теперь передаётся `mod=mod`.
- Если `mod is None` — блок модератора пропускается (обратная совместимость).

### 24 ✅ Блок «Веб-профиль» — кликабельная ссылка на web-панель

**Проблема**: модератор в репорт-чате видел только текстовый ID нарушителя. Чтобы открыть его историю санкций в веб-панели, нужно было скопировать ID, открыть веб-панель, вставить ID в поиск. Лишние клики.

**Что сделано в `bot_handlers.py`**:
- Добавлен env `WEB_PUBLIC_URL` — публичный URL веб-панели (например `https://shadow-logs.example.com`). Если пустой — веб-блок не добавляется (обратная совместимость).
  ```python
  WEB_PUBLIC_URL = (os.getenv("WEB_PUBLIC_URL") or "").rstrip("/")
  ```
- Если `WEB_PUBLIC_URL` задан — после блока модератора добавляется Paragraph:
  ```python
  web_url = f"{WEB_PUBLIC_URL}/user/{target.id}"
  InputRichBlockParagraph(text=[
      "🌐 Веб-профиль: ",
      RichTextUrl(text=web_url, url=web_url),
  ])
  ```
- Ссылка ведёт на `/user/{user_id:int}` — существующий эндпоинт в `web_app.py:449` (страница истории санкций пользователя).
- Оба случая покрыты тестами: с `WEB_PUBLIC_URL` и без.

### 25 ✅ Plain-text fallback обновлён

**Что сделано в `bot_handlers.py`**:
- `_send_report_plain_fallback()` теперь принимает `mod_text: str | None` и `web_url: str | None`.
- Если Rich Message падает (например, Telegram не поддерживает Rich Messages в конкретном чате) — отправляется обычный текстовый отчёт:
  ```
  #test
  🔇 МУТ

  👤 Иван Петров
     @ivan_p
     ID: 123456789
  👮 Модератор: Админ Ботович @admin_bot (ID: 987654321)
  🌐 Веб-профиль: https://shadow-logs.example.com/user/123456789
  📝 Причина: спам
  ⏱ Длительность: 1ч
  🕐 26.07.2026 12:44 МСК | #test
  ```
- В plain text URL распознаётся Telegram автоматически — веб-ссылка остаётся кликабельной. `tg://user?id=…` в plain text **не** распознаётся, поэтому кликабельные упоминания нарушителя/модератора есть только в Rich-версии (это нормально: Rich Messages поддерживаются с Bot API 10.2 / aiogram 3.30).

### 26 ✅ Тесты v4.4.1

**`scripts/test_v44_rich_report.py`** — 29 проверок, 7 секций:
1. Сборка Rich-сообщения: send_rich_message вызван, chat_id передан, JSON-сериализация ок, ≥5 блоков, в блоке нарушителя есть `tg://user?id=…` + имя + @username, в блоке модератора есть `tg://user?id=…` + «Модератор» + имя, в блоке веб-профиля есть `WEB_PUBLIC_URL/user/<id>`.
2. `WEB_PUBLIC_URL=""` → веб-блока нет, но модератор есть.
3. `mod=None` → блока модератора нет, но нарушитель есть.
4. Нарушитель без @username → кликабельная ссылка работает, имя присутствует, @-строки нет.
5. Нарушитель без имени совсем → «(без имени)» в кликабельном тексте, ссылка работает.
6. Plain-text fallback (когда Rich падает): содержит имя, @username, ID нарушителя, модератора, веб-ссылку.
7. Дефолт `WEB_PUBLIC_URL = https://degraban.bothost.tech` (без env / без патча): `bot_handlers.WEB_PUBLIC_URL` зафиксирован, в Rich-сообщении используется дефолт-URL.

**Результат**: 29/29 ✅. Старые тесты `test_v44_tgid_create.py` — 82/82 ✅ (регрессии нет).

### Технические заметки v4.4.1

**Почему `RichTextUrl`, а не `RichTextTextMention`?**
В Bot API 10.2 есть два способа сделать кликабельное упоминание пользователя:
- `RichTextTextMention(text=..., user=User)` — требует **полный объект User** (с id, first_name, last_name, username и т.д.). Этот объект берётся из `bot.get_chat()` или из `message.from_user`.
- `RichTextUrl(text=..., url="tg://user?id=…")` — URL-схема, Telegram нативно открывает профиль юзера по ID. Не требует объекта User, только численный ID.

Мы используем `RichTextUrl` + `tg://user?id=…` — это надёжнее: не нужно тащить полный объект User в каждый отчёт, и это работает даже если у юзера изменились first_name/last_name со временем (ID неизменен).

**Почему `tg://user?id=…` вместо `@username`?**
- `@username` работает только если у юзера **есть** @username. У многих нарушителей его нет.
- `tg://user?id=…` работает для **любого** юзера Telegram, независимо от наличия @username.
- `tg://user?id=…` — это глубокая ссылка (deep link), Telegram открывает профиль нативно, без переключения в браузер.

**Почему веб-ссылка в plain-text fallback работает без HTML?**
В `bot.send_message(parse_mode=None)` Telegram **авто-распознаёт URL'ы** в тексте и делает их кликабельными. Это поведение по умолчанию для plain text. Поэтому веб-ссылка `https://shadow-logs.example.com/user/123456789` останется кликабельной даже в fallback. А `tg://user?id=…` в plain text **не распознаётся** — поэтому кликабельные упоминания tg-юзеров есть только в Rich-версии. Это приемлемый компромисс, т.к. Rich Messages поддерживаются везде, где есть Bot API 10.2.

**Файловая структура v4.4.1** (без изменений относительно v4.4, только правки):
```
shadow-logger/
├── bot_handlers.py           # v4.4.1: RichTextUrl для нарушителя/модератора/веб-ссылки, WEB_PUBLIC_URL env
└── scripts/
    ├── test_v44_tgid_create.py  # 82 теста (без изменений)
    └── test_v44_rich_report.py  # v4.4.1: 27 тестов нового _send_report
```

**Новый env**:
| Имя | Назначение | По умолчанию |
|-----|------------|--------------|
| `WEB_PUBLIC_URL` | Публичный URL веб-панели для кликабельных ссылок в Rich-отчётах. Формат: `https://shadow-logs.example.com` (без завершающего `/`). | `https://degraban.bothost.tech` (production Bothost) — переопределяется env только если деплой на другой домен |

---

## ✅ Готово — v4.4.2 (Welcome-сообщение новому админу в ЛС)

### 27 ✅ Автоматическая доставка учётных данных в Telegram

**Проблема**: в v4.4 после создания веб-админа пароль показывался только SU один раз в зелёном блоке на `/admin/users`. SU должен был **вручную** пересылать логин/пароль новому админу через любой безопасный канал (ЛС в Telegram, лично, и т.д.) — это лишний шаг, особенно если админов много.

**Решение**: раз `bot.get_chat(user_id)` сработал (значит юзер уже общался с ботом и диалог открыт) — бот может сам отправить приветствие с данными для входа. SU больше не нужен как «почтальон».

**Что сделано в `web_app.py`**:
- В импортах добавлены Rich Messages-типы: `InputRichMessage`, `InputRichBlockSectionHeading`, `InputRichBlockParagraph`, `InputRichBlockFooter`, `RichTextUrl`, `RichTextBold`, `RichTextSpoiler`. Также `TelegramBadRequest`.
- Добавлена константа `WEB_PUBLIC_URL` (дублирует `bot_handlers.py` намеренно — веб-слой не должен зависеть от модуля бота; дефолт тот же: `https://degraban.bothost.tech`).
- Добавлена функция `_send_admin_welcome(bot, tg_user_id, login, password, first_name=None) -> tuple[bool, str]`:
  - Строит Rich-сообщение со структурой:
    1. `SectionHeading` — `🎉 Доступ к веб-панели[, <first_name>]`
    2. `Paragraph` — текст + кликабельная ссылка `RichTextUrl(text=web_root_url, url=web_root_url)` на `https://degraban.bothost.tech/`
    3. `Paragraph` — `"Данные для входа (скрыты под спойлером):"`
    4. `Paragraph` с `RichTextSpoiler` — внутри спойлера: `Логин: <RichTextBold(login)>`, `Пароль: <RichTextBold(password)>`. Спойлер в Telegram выглядит как затемнённый текст, раскрываемый по клику — безопаснее чем plain text (если кто-то рядом смотрит на экран, пароль не виден сразу).
    5. `Paragraph` — `🔐 После первого входа смените пароль: раздел <RichTextBold("Dashboard")> → блок <RichTextBold("Change my password")> (нужно указать текущий пароль и новый).`
    6. `Footer` — `⏱ DD.MM.YYYY HH:MM МСК`
  - Отправляет через `bot.send_rich_message(chat_id=tg_user_id, rich_message=InputRichMessage(blocks=...))`.
  - Возвращает `(True, "ok")` при успехе, `(False, "<error>")` при ошибке.
  - Ловит `TelegramBadRequest` (юзер заблокировал бота) и любые другие `Exception` — не падает.

**Интеграция в `POST /admin/users/create`**:
- После `session.commit()` (юзер создан в БД) вызывается `_send_admin_welcome(bot, tg_id, login, password, tg_first_name)`.
- Результат добавляется в signed-flash payload как поле `w` (1 — отправлено, 0 — нет):
  ```python
  flash_token = _sign_flash({
      "u": login, "p": password, "tg": tg_id,
      "t": int(time.time()),
      "w": 1 if welcome_ok else 0,
  })
  ```
- Логирование: success → INFO, failure → WARNING с подсказкой "SU must deliver credentials manually".

**Обновлён `GET /admin/users`**:
- Из signed-flash извлекается `welcome_sent = bool(payload.get("w", 0))` и передаётся в шаблон.

**Обновлён `templates/admin.html`**:
- В зелёном блоке «Admin created» под таблицей с паролем появился новый блок со статусом доставки:
  - `welcome_sent=True` → зелёная надпись `✅ Welcome message with these credentials was sent to the user's Telegram private chat. The password is hidden under a spoiler there — the user clicks to reveal it.`
  - `welcome_sent=False` → красная надпись `⚠ Welcome message could not be delivered to the user's Telegram (the user may have blocked the bot). Please forward the credentials above to the user via another secure channel.`
- Так SU сразу видит: либо «всё ок, бот сам отправил», либо «юзер заблокировал бота — передавай пароль руками».

**Обратная совместимость**:
- Если `bot=None` (веб-панель запущена без бота) — `_send_admin_welcome` возвращает `(False, "bot is None")`, но `/admin/users/create` и так уже отказывает в работе при `bot=None` (раньше в п.2 эндпоинта).
- Если Rich Messages не поддерживаются (маловероятно при Bot API 10.2) — `TelegramBadRequest` ловится, SU видит предупреждение, пароль в зелёном блоке всё равно показан.

### 28 ✅ Тесты v4.4.2

**`scripts/test_v44_welcome.py`** — 38 проверок, 8 секций:
1. Базовая отправка: ok=True, err='ok', chat_id передан, Rich-сообщение сериализуется, есть ссылка на `degraban.bothost.tech`, логин, пароль, «Change my password», «Dashboard», «Логин:», «Пароль:», есть `RichTextSpoiler` (type=spoiler), имя `Иван` в заголовке, `🎉` в заголовке.
2. Без `first_name`: ok=True, заголовок без `, `, логин присутствует.
3. `bot=None`: ok=False, err='bot is None'.
4. `send_rich_message` падает: ok=False, err содержит 'TelegramBadRequest' и 'chat not found'.
5. `POST /admin/users/create` вызывает welcome: SU логинится, создание → 303, `_send_admin_welcome` вызван с правильным chat_id, логин присутствует в сообщении.
6. `GET /admin/users?created=<token>` показывает `✅` (welcome_sent=True).
7. `welcome_sent=False` когда бот не может отправить (`bot was blocked by the user`): в HTML есть `⚠` и «could not be delivered».
8. Структура Rich-сообщения: `RichTextSpoiler` найден, в нём есть логин и пароль через `RichTextBold`.

**Результат**: 38/38 ✅. Старые тесты — `test_v44_tgid_create.py` 82/82 ✅, `test_v44_rich_report.py` 29/29 ✅. **Итого 149/149 ✅.**

### Технические заметки v4.4.2

**Почему `RichTextSpoiler`, а не plain text для пароля?**
Спойлер в Telegram — это затемнённый текст, который раскрывается только по клику пользователя. Если кто-то рядом смотрит на экран получателя (в офисе, в метро) — пароль сразу не виден. Получатель должен осознанно кликнуть, чтобы увидеть его. Это заметно безопаснее чем plain text, который виден сразу в превью сообщения.

**Почему `RichTextBold` внутри спойлера?**
Логин и пароль выделены bold-стилем, чтобы их было удобно читать после раскрытия спойлера — глаз сразу цепляется за значения, а не за лейблы «Логин:»/«Пароль:».

**Почему бот отправляет, а не "стелс" уже нарушен?**
Стелс-режим бота означает: **обычные нарушители в группах не получают сообщений от бота** (бот не представляется, не пишет «вы забанены за спам», и т.д.). Это сохранено. Но новый админ — это не нарушитель. Он:
1. Уже знает про бота (он сам инициировал диалог, иначе `bot.get_chat()` не сработал бы).
2. Является модератором с доступом к веб-панели.
3. Нуждается в учётных данных для входа.

Отправка welcome-сообщения админу в ЛС — это часть онбординга модератора, а не нарушение стелса. Нарушители в группах по-прежнему не получают от бота ничего.

**Почему `_send_admin_welcome` в `web_app.py`, а не в `bot_handlers.py`?**
Логически это часть флоу создания веб-админа — оно инициируется из `/admin/users/create` (веб-слой). Бот здесь используется как «почтовый клиент» — ему всё равно что отправлять. Если вынести в `bot_handlers.py`, придётся тащить туда знание о структуре welcome-сообщения (HTML/Rich), что размывает ответственность. Сейчас `web_app.py` владеет всем UX веб-панели (включая welcome), а `bot_handlers.py` — логикой модерации.

**Файловая структура v4.4.2** (без изменений относительно v4.4.1, только правки):
```
shadow-logger/
├── web_app.py                 # v4.4.2: _send_admin_welcome(), WEB_PUBLIC_URL const, поле 'w' в flash
├── templates/admin.html       # v4.4.2: блок со статусом доставки welcome (✅/⚠)
└── scripts/
    ├── test_v44_tgid_create.py  # 82 теста (без изменений)
    ├── test_v44_rich_report.py  # 29 тестов (без изменений)
    └── test_v44_welcome.py      # v4.4.2: 38 тестов нового welcome-флоу
```

**Без новых env**: используется тот же `WEB_PUBLIC_URL` (дефолт `https://degraban.bothost.tech`).

---

## ✅ Готово — v4.4.3 (Управление модераторами через веб-панель)

### 21 ✅ Добавление/удаление модераторов чатов через `/admin/moderators`

**Проблема v4.4.2**: для добавления модератора в чат (пользователя, который может использовать команды `!mute`/`!warn`/`!ban` в конкретном чате, помимо глобального `ADMIN_IDS` env) существовала только бот-команда `/addadmin chat_id user_id` в ЛС бота. Это неудобно с телефона, требует помнить синтаксис, и не работает если веб-панель нужна для аудита списка модераторов.

**Что сделано в `web_app.py`**:
- 3 новых маршрута (все **SU-only**, как и `/admin/users`):
  | Метод | Путь | Назначение |
  |-------|------|------------|
  | `GET` | `/admin/moderators` | Страница с формой добавления + список существующих модераторов |
  | `POST` | `/admin/moderators/create` | Создание записи в `chat_admins` (поля `chat_id`, `user_id`) |
  | `POST` | `/admin/moderators/{id}/delete` | Удаление записи из `chat_admins` |
- Список модераторов собирается через `LEFT JOIN ChatAdmin → Moderator → ChatSettings`:
  - `Moderator.username` / `Moderator.first_name` — если модератор уже применял санкции (профиль подтянулся через `_upsert_moderator`).
  - `ChatSettings.hashtag` — если у чата задан хэштег.
  - `LEFT JOIN` (а не `INNER`) — потому что модератор мог быть добавлен, но ни разу не использовать бота.
- **Best-effort подтягивание профиля**: после сохранения `ChatAdmin`, если передан `bot`, дёргается `bot.get_chat(user_id)` и полученные `username` / `first_name` upsert-ятся в таблицу `Moderator`. Если `bot.get_chat` падает (юзер не общался с ботом, бот заблокирован и т.д.) — запись в `chat_admins` всё равно создаётся, профиль подтянется при первой же команде.
- **Валидация**:
  - `chat_id` и `user_id` должны быть числами (иначе flash "must be numbers").
  - `chat_id = 0` запрещён (это default-чат, не реальный) → flash "cannot be 0".
  - `user_id <= 0` запрещён → flash "must be positive".
  - Дубликат `(chat_id, user_id)` отклоняется → flash "already a moderator".
- **`Form("")` вместо `Form(...)`**: чтобы пустые строки доходили до нашего кода валидации, а не отбивались FastAPI как missing field (422). Так мы контролируем сообщение об ошибке.
- **`.first()` вместо `.scalar_one_or_none()`**: на уровне БД нет UNIQUE constraint на `(chat_id, user_id)`, поэтому теоретически может быть несколько строк (если `/addadmin` и web создали дубликат до проверки приложением). Нам достаточно знать, что хотя бы одна существует.

**Что сделано в `templates/admin_moderators.html`** (новый файл):
- Форма добавления: 2 поля (`chat_id`, `user_id`) с `pattern` для клиентской валидации.
- Блок **Known chats**: кнопки быстрого заполнения `chat_id` из существующих `ChatSettings` (исключая default `chat_id=0`). Клик вставляет значение в поле.
- Таблица модераторов: `#`, `Chat ID`, `Hashtag`, `User ID`, `Name` (с @username ссылкой на `t.me`), `Added by` (показывает `via bot` если `added_by` не None, или `via web` если None), `Added at`, `Actions` (кнопка Remove с `confirm()`).
- Блок **Fallback**: явное упоминание, что команды `/addadmin` и `/deladmin` в боте продолжают работать и пишут в ту же таблицу `chat_admins`. Записи, созданные через бот-команду, имеют `added_by = <TGID су>`; через веб — `added_by = None`.

**Что сделано в `templates/base.html`**:
- В навбар добавлена ссылка **Moderators** (видна только SU, рядом с Admins).
- Активный подсвет ссылки: `request.url.path.startswith('/admin/moderators')`.
- Ссылка **Admins** теперь подсвечивается только для `/admin/users` (а не для любого `/admin/*`).

**Паритет с бот-командой**:
- Записи, созданные через `/addadmin chat_id user_id` (в bot_handlers.py), и через `/admin/moderators/create` (в web_app.py), попадают в **одну и ту же таблицу** `chat_admins`.
- Удаление через `/deladmin` или через `/admin/moderators/{id}/delete` — также работает с одной и той же таблицей.
- При удалении записи из `chat_admins` профиль модератора в таблице `moderators` **сохраняется** — история его санкций не должна потеряться.

**Тесты** (`scripts/test_v44_moderators_web.py`, 65 проверок):
1. `GET /admin/moderators` — рендеринг с данными (LEFT JOIN с Moderator + ChatSettings).
2. `POST /admin/moderators/create` — успешное создание + best-effort подтягивание профиля через `bot.get_chat`.
3. Дубликат `(chat_id, user_id)` отклоняется, `bot.get_chat` не вызывается.
4. Валидация: нечисловые ID, `chat_id=0`, отрицательный `user_id`, `user_id=0`, пустые строки.
5. `bot=None` — запись создаётся, профиль не подтягивается (non-critical).
6. `bot.get_chat` падает — запись всё равно создаётся.
7. `POST /admin/moderators/{id}/delete` — успешное удаление, профиль модератора сохраняется.
8. Удаление несуществующего ID — silent 303 (no crash).
9. Non-SU admin не имеет доступа (redirect на `/dashboard`).
10. Неавторизованный — redirect на `/login`.
11. **Паритет**: web + `/addadmin` пишут в одну таблицу; web-проверка дубликата срабатывает даже если `/addadmin` уже создал запись.
12. Best-effort: профиль модератора **обновляется**, если он уже существует в `moderators` (например, изменил @username).
13. HTML: nav link "Moderators" виден SU и **не виден** обычным админам.

**Без новых env / БД-миграций**: используется существующая таблица `chat_admins` (была добавлена в одной из ранних версий для бот-команды `/addadmin`).

---

## ✅ Готово — v4.3 (опрятность панели + SU + мульти-админ + автообновление)

### 12 ✅ Визуальная полировка веб-панели (без смены палитры и текста)

**Проблема v4.2**: панель работала, но плотность и читаемость оставляли желать лучшего — таблицы без зебры, мелкие бейджи съезжали, не было якорей навигации, контент налезал на границы.

**Что сделано в `templates/base.html`**:
- Введены CSS-переменные для отступов (`--sp-1..--sp-7`) и радиусов (`--r-xs/--r-sm/--r-md`).
- Navbar стал `sticky` (остаётся при скролле), с blur-фоном, увеличен до 52px.
- Каждая секция получила `<a class="anchor">`-ссылку с `scroll-margin-top: 70px` (якорь не уходит под navbar).
- Таблицы обёрнуты в `.table-wrap` с `border-radius`, sticky `<thead>`, hover-зебра на `tbody tr:hover`, класс `.row-revoked` для снятых санкций (opacity 0.55 + line-through).
- Бейджи (`.badge`) переработаны: `inline-flex`, padding 3×8, border-radius, добавлены классы `.badge-unwarn`, `.badge-unban`, `.badge-revoked`, `.badge-active`.
- Карточки статистики (`.stat-card`) получили цветовые модификаторы `.value.danger/.warn/.info/.accent`, hover-эффект на border-color.
- Filter-tabs обрели border-radius и hover-фон.
- Search-bar — border-radius + focus-ring (3px glow).
- Pagination — border-radius на кнопках, `min-width: 32px` для единообразия.
- Добавлена `.anchor-nav` (in-page navigation bar) с быстрыми ссылками на секции.
- Добавлен `.autorefresh-bar` с пульсирующей точкой и кнопкой pause/resume.
- Профиль юзера вынесен в `.profile-table` с выделенной левой колонкой (uppercase, dim).
- Адаптивный брейкпоинт 800px: two-col → single-col, narrower cards.

---

### 13 ✅ Автообновление страниц dashboard / user

**Что сделано**:
- В `base.html` добавлен JS-блок: если шаблон передаёт `autorefresh=true`, запускается таймер на 15 секунд.
- При `document.hidden=true` (вкладка не активна) обновление откладывается до возврата фокуса.
- Кнопка `pause` / `resume` в `.autorefresh-bar` позволяет заморозить автообновление.
- Dashboard включает автообновление только на 1-й странице без фильтров (чтобы не сбрасывать состояние пользователя).
- User-страница включает автообновление без фильтров.
- Серверный endpoint `/api/dashboard` отдаёт JSON для будущей точечной подмены строк (без full reload).

---

### 14 ✅ SU + мульти-админ (WebUser model)

**Проблема v4.2**: была одна учётка веб-панели через `WEB_PASSWORD` env. Нельзя было раздать доступ нескольким модераторам или временно отключить одного.

**Что сделано в `db.py`**:
- Добавлены хелперы `_hash_password(password, salt=None) → "salt:hash"` и `_verify_password(password, stored) → bool` (PBKDF2-HMAC-SHA256, 200 000 итераций, соль 16 байт).
- Добавлена модель `WebUser`:
  | Поле | Тип | Назначение |
  |------|-----|------------|
  | `id` | Integer PK | — |
  | `username` | String(64) UNIQUE | Логин (lowercase) |
  | `password_hash` | String(255) NULL | NULL только для 'su' (пароль из env) |
  | `is_su` | Boolean | True только для username='su' |
  | `is_active` | Boolean | False = логин отклоняется |
  | `created_by` | String(64) | username создателя |
  | `last_login_at` | DateTime | обновляется при успешном входе |
- В `init_db()` добавлен seed: если `WebUser(username='su')` нет — создаётся автоматически с `is_su=True, is_active=True, password_hash=None`.

**Что сделано в `web_app.py`**:
- Токены сессии переписаны: вместо `<random_hex>:<signature>` теперь `JSON{u,s,t,n}:<hmac>`. В payload зашит username и is_su — после HMAC-подписи их нельзя подменить.
- `require_auth` не только проверяет подпись токена, но и сверяет, что аккаунт существует и активен в `web_users` (отзыв сессии при `is_active=False` или удалении аккаунта).
- Добавлена зависимость `require_su` — редирект не-SU на /dashboard.
- `/login` принимает `username` + `password`:
  - `username == "su"` → сверка с `WEB_PASSWORD` env.
  - Иначе — поиск в `web_users`, проверка `is_active` и `_verify_password`.
- `last_login_at` обновляется при успешном входе.
- Эндпоинты CRUD (только SU):
  - `GET  /admin/users` — список + форма создания.
  - `POST /admin/users/create` — создание (3–32 chars, не 'su', пароль ≥ 6).
  - `POST /admin/users/{id}/toggle` — вкл/выкл (SU нельзя отключить).
  - `POST /admin/users/{id}/reset` — сменить пароль.
  - `POST /admin/users/{id}/delete` — удалить (SU нельзя удалить).

**Что сделано в шаблонах**:
- `login.html`: добавлено поле `Username`, hint про `su` + env.
- `base.html`: navbar показывает `<span class="user-chip">` с username и SU-бейджем; ссылка `Admins` видна только SU.
- `admin.html` (новый): форма создания + таблица существующих админов с кнопками Disable/Reset pw/Delete.

---

### 15 ✅ Фильтры и сортировка в дашборде

**Что сделано в `web_app.py`**:
- `GET /dashboard` принимает `action`, `rev`, `sort` query-параметры.
- `action`: `mute|warn|ban|unmute|unwarn|unban` (или пусто = All).
- `rev`: `active|revoked` (или пусто = All).
- `sort`: `new|old|type|user`.
- Счётчики статистики теперь фильтруются по `is_revoked=False` (показываем только активные).
- `GET /api/dashboard` отдаёт те же данные в JSON для будущего точечного обновления.

**Что сделано в `dashboard.html`**:
- Три ряда filter-tabs: Action (All/Mute/Warn/Ban/Unmute/Unwarn/Unban), Revoked (All/Active/Revoked), Sort (Newest/Oldest/By type/By user).
- Активный фильтр подсвечен accent-цветом.
- Пагинация сохраняет все три параметра в URL.
- Добавлены 7-я и 8-я карточки статистики: Unwarns и Unbans.

---

### 16 ✅ REVOKED-бейдж и фильтр revoked

**Что сделано**:
- В таблицах дашборда и user-страницы для каждой строки с `punishment.is_revoked=True`:
  - На `<tr>` вешается класс `row-revoked` (opacity + line-through).
  - В колонке Action после бейджа типа выводится `<span class="badge badge-revoked">↩ revoked</span>`.
- В user-странице фильтр по `rev` переключает между All/Active/Revoked.
- В дашборде аналогично.

---

### 17 ✅ Тесты v4.3

**Новые и обновлённые тесты**:
- `scripts/test_v43_webuser.py` — 37 проверок: PBKDF2-хэширование, seed SU в init_db, HMAC-токены (SU/admin/мутация/мусор), CRUD через DB API, username uniqueness.
- `scripts/test_templates.py` — переписан под новый контекст (`auth_user`, `action_filter`, `rev_filter`, `sort`); 10 тестов покрывают все 4 шаблона (dashboard/user/admin/login) включая REVOKED-бейдж и autorefresh-бар.
- `scripts/test_e2e.py` — расширено до 25 проверок: SU-логин, неправильный пароль, несуществующий юзер, /admin/users (SU-only), создание/дубликат/'su'-reserved/логин/сменапароля/отключение/реактивация/удаление, невозможность удалить/отключить SU, /api/dashboard, все фильтры и сортировки.
- `scripts/test_send_report.py` — исправлен под v4.2: добавлен `bot.send_sticker = AsyncMock()` (стикеры в отчётах), `_send_ephemeral` принимает `recipient=` вместо `target=`.
- `scripts/test_v42_stickers_unwarn.py` — без изменений, проходит.
- `scripts/test_ephemeral_mod.py` — без изменений, проходит.
- `scripts/test_rich_blocks.py` — без изменений, проходит.

**Результат**: все 7 тестовых наборов ✅.

---

## ✅ Готово — v4.2 (стикеры в отчётах + снятие санкций + удаление сообщения при варне)

### 6 ✅ Стикеры в Rich-отчётах

**Проблема**: при наказании за стикер модератор видел только текстовое описание "🎭 [Стикер: 🤬]" в BlockQuotation, но не сам стикер.

**Решение**: Rich Messages API (Bot API 10.2) **не имеет** inline-блока для стикеров (есть только photo/video/animation/audio/voice_note). Поэтому стикер отправляется **отдельным сообщением** через `bot.send_sticker()` сразу после Rich-сообщения.

**Что сделано**:
- В `_send_report` добавлена переменная `sticker_file_id` — извлекается из `reply_to_message.sticker.file_id`.
- После успешной отправки Rich Message (или fallback) — если `sticker_file_id` задан, отправляется `bot.send_sticker(chat_id=report_dest, sticker=sticker_file_id)`.
- Ошибка отправки стикера логируется как WARNING (не блокирует остальную работу).
- Работает для всех типов стикеров: статичные (WebP), анимированные (TGS), видео-стикеры (WebM).

**Преимущества подхода**: не требует конвертации (TGS → PNG невозможно без Lottie-рендера), работает с любым стикером, минимальный код.

---

### 7 ✅ Удаление сообщения при !warn

**Что сделано**:
- В обработчике `!warn` добавлен `await message.reply_to_message.delete()` — удаляет сообщение нарушителя, за которое выдан варн.
- Удаление происходит ПОСЛЕ сохранения наказания в БД (чтобы `message_text` уже был сохранён).
- Удаление происходит ДО `_send_report` (чтобы rich-сообщение ушло после того, как сообщение нарушителя уже удалено из чата).
- Если удаление не удалось (например, у бота нет прав) — логируется WARNING, обработка продолжается.

**Логика**: варн без удаления сообщения лишён смысла — нарушитель видит, что его сообщение осталось, и не понимает, что получил варн. Удаление + отсутствие уведомления = "сообщение исчезло само", что усиливает стелс-эффект.

---

### 8 ✅ Команда !unwarn [N] — снятие N последних варнов

**Что сделано**:
- Добавлен regex `_CMD_UNWARN = re.compile(r"^!unwarn(?:\s+(\d+))?\s*$", re.IGNORECASE)`.
- По умолчанию N=1. Если N > 100 — ограничивается до 100 (защита от опечаток).
- Добавлен хелпер `_revoke_last_warns(session, user_id, chat_id, count, revoked_by_mod_id)` — помечает последние N активных варнов как `is_revoked=True`.
- Возвращается количество фактически снятых варнов (может быть меньше N, если активных варнов меньше).
- Создаётся запись в `punishments` с `action_type='unwarn'` (как отчёт о снятии).
- Отправляется Rich-отчёт в репорт-чат с заголовком "↩️ СНЯТИЕ ВАРНА".
- Модератор получает ephemeral: `✅ Снято N варн(а/ов) с <mention>. Варнов всего: M`.
- `_count_warns` обновлён — игнорирует снятые варны (`is_revoked=False`).

**Примеры**:
- `!unwarn` (reply) — снять 1 последний варн
- `!unwarn 3` (reply) — снять 3 последних варна

---

### 9 ✅ Команда !unban — разбан

**Что сделано**:
- Добавлен regex `_CMD_UNBAN = re.compile(r"^!unban\s*$", re.IGNORECASE)`.
- Использует `bot.unban_chat_member(chat_id, user_id, only_if_banned=True)` — **безопасный разбан**: не разбанит того, кто не забанен (иначе можно использовать для обхода кика).
- Помечает последний активный бан как `is_revoked=True` через `_revoke_last_action(session, user_id, chat_id, "ban", mod.id)`.
- Создаёт запись в `punishments` с `action_type='unban'`.
- Отправляет Rich-отчёт с заголовком "🎉 РАЗБАН".
- Модератор получает ephemeral: `✅ Разбанен <mention>.`

**Примечание**: Telegram автоматически не уведомляет разбаненного юзера. Юзер сможет снова войти в чат, если он публичный, или по инвайт-ссылке.

---

### 10 ✅ Soft-revoke в БД (is_revoked / revoked_at / revoked_by_mod_id)

**Схема** (новые колонки в `punishments`):
| Колонка | Тип | Default | Назначение |
|---------|-----|---------|------------|
| `is_revoked` | BOOLEAN | FALSE | True = санкция снята, не учитывать в счётчиках |
| `revoked_at` | DATETIME | NULL | Когда снята |
| `revoked_by_mod_id` | BIGINT | NULL | Кто из модераторов снял |

**Миграция** в `init_db()`: три новые колонки добавляются через `ALTER TABLE` если их нет (идемпотентная миграция, совместима со старыми БД).

**Хелперы**:
- `_revoke_last_warns(session, user_id, chat_id, count, mod_id)` → `int` (для !unwarn)
- `_revoke_last_action(session, user_id, chat_id, action_type, mod_id)` → `bool` (для !unmute / !unban — помечает последний активный mute/ban)

**Каскадное обновление**: `!unmute` теперь не только выдаёт права, но и помечает последний активный mute как `is_revoked=True`. Аналогично `!unban` помечает последний активный ban.

**`_count_warns` обновлён**: фильтр `Punishment.is_revoked.is_(False)` — снятые варны не учитываются в счётчике (важно для `_check_warn_threshold` — автомьют/автобан больше не сработает повторно на уже снятом варне).

**Совместимость со старым `!resetwarns`**: старая команда обнуляет `duration_seconds=0`, что тоже работает с новым `_count_warns` (сумма станет 0). Новая `!unwarn` предпочтительнее — она помечает конкретные варны как снятые, сохраняя историю.

---

### 11 ✅ Action labels для новых типов

В `_send_report` обновлены `action_labels`:
- `"unban": "🎉 РАЗБАН"`
- `"unwarn": "↩️ СНЯТИЕ ВАРНА"`

`total_warns` теперь показывается и в `unwarn`-отчётах (чтобы модератор видел, сколько варнов осталось после снятия).

---

**Тесты**: `scripts/test_v42_stickers_unwarn.py` — 40 проверок (стикеры, удаление при варне, regex для !unwarn/!unban, action_labels, DB-миграция, функциональный тест с in-memory SQLite на логику soft-revoke). Все ✅. Прошлые тесты v4.1 (`test_ephemeral_mod.py`) обновлены под 8 ephemeral-вызовов — все ✅.

---

## ✅ Готово — v4.1 (патч: ephemeral → модераторам вместо нарушителей)

### 3.1 ✅ Ephemeral-уведомления перенаправлены модераторам

**Проблема v4.0**: ephemeral-сообщения отправлялись нарушителю (`receiver_user_id=target.id`), что раскрывало существование бота — нарушитель видел "🔇 Дедушка Вобжак замьютил тебя…", хотя бот должен оставаться скрытым.

**Что изменено в v4.1**:
- Хелпер `_send_ephemeral(*, bot, chat_id, recipient, text)` — параметр переименован `target` → `recipient`, теперь это **получатель**, а не нарушитель. Добавлен `parse_mode="HTML"` для поддержки кликабельных упоминаний.
- Добавлен хелпер `_user_mention_html(user)` — строит HTML-mention `<a href="tg://user?id=…">Имя</a>` с экранированием (`html.escape`) для защиты от HTML-инъекций в именах.
- В `!mute` — модератор получает ephemeral: `✅ Замьютил <target_mention> на <dur>[ за: <reason>].`
- В `!warn` — модератор получает: `✅ Варн выдан <target_mention>[ за: <reason>]. Варнов всего: N`
- В `!ban` — модератор получает: `✅ Забанен <target_mention>[ за: <reason>].`
- В `!unmute` — модератор получает: `✅ Размьючен <target_mention>.`
- В `_check_warn_threshold` (авто-санкции) добавлены ephemeral модератору:
  - Автобан: `🤖 Автобан: <target_mention> (N варнов).`
  - Автомьют: `🤖 Автомьют: <target_mention> (N варнов, <dur>).`
- Причина (`reason`) экранируется через `html.escape` перед вставкой в HTML-текст.
- Docstring в шапке `bot_handlers.py` обновлён: явно зафиксировано, что нарушитель НЕ получает уведомлений, а ephemeral видят только модераторы.
- Все 4 старых текста "тебя/тебе" удалены; вместо них — подтверждения модератору.

**Стелс-гарантия**: нарушитель не получает ни ephemeral, ни reply, ни DM от бота. Бот по-прежнему молча удаляет команду модератора и применяет санкцию.

**Тесты**: `scripts/test_ephemeral_mod.py` — 24 проверки (6 вызовов используют `recipient=mod`, ни один не содержит `target=target`, `_user_mention_html` корректно экранирует `<script>`, все новые тексты присутствуют, старые отсутствуют). Все ✅.

---

## ✅ Готово — v4

### 1. ✅ Убрать env fallback для REPORT_CHAT_ID

**Что сделано**:
- В `_get_report_chat_id()`: убран шаг "3. Env fallback" — теперь только DB (per-chat → default) → disabled
- В `bot.py`: убрано чтение/проверка `REPORT_CHAT_ID` из env при старте. Вместо этого проверяется глобальный default из DB (chat_id=0).
- В `web_app.py`: убран `env_report_chat_id` из контекста шаблонов dashboard/user. Вместо него добавлен `default_report_chat_id` (из DB chat_id=0).
- В шаблонах `dashboard.html` и `user.html`: убран fallback на `env_report_chat_id` в строке `resolved_rc`. Content-колонка больше не ссылается на `report_message_id`.
- В `.env.example`: убран/закомментирован `REPORT_CHAT_ID`, добавлены комментарии про `/setreport`.
- В `bot_handlers.py`: убрана глобальная `REPORT_CHAT_ID = int(os.getenv(...))`.

**Результат**: Система работает как per-chat → global default (chat_id=0 в ChatSettings) → disabled.

---

### 2. ✅ Переписать `_send_report()` на Rich Messages (Bot API 10.2)

**Что сделано**:
- `_send_report()` теперь строит `InputRichMessage` с блоками:
  1. `SectionHeading` — 🔇 МУТ / 🚫 БАН / ⚠️ ВАРН / 🔊 РАЗМУТ
  2. `Paragraph` — Нарушитель (имя, @username, ID)
  3. `Paragraph` — Причина (если есть)
  4. `BlockQuotation` — Текст/caption сообщения нарушителя (с text-content)
  5. `Photo` / `Video` / `Animation` / `Audio` / `VoiceNote` — inline-медиа (вместо `forward_message`)
  6. `Details` — Доп. инфо: Chat ID, длительность, варнов всего (сворачиваемое)
  7. `Footer` — Время МСК + хэштег чата
- Убран `forward_message` — медиа теперь inline в rich message.
- Убран `_has_media()` — больше не нужен.
- Добавлен хелпер `_build_media_block()` — фабрика inline-блоков для разных медиа-типов.
- Добавлен `_send_report_plain_fallback()` — резервный plain-text отчёт, если `send_rich_message` упадёт (например, в чате без поддержки Rich Messages).
- `report_message_id` в Punishment — soft-deprecated: колонка сохраняется в DB для старых записей, новые записи не пишут.
- `_send_report()` теперь возвращает `None` (раньше — `int | None`).
- Все вызовы `_send_report()` в `handle_group_command` обновлены: убраны `report_msg_id = ...` и `report_message_id=report_msg_id` в `_save_punishment()`.

**Ограничения Rich Messages (учтены в коде)**:
- 32768 символов / 500 блоков / 16 вложений — лимиты Bot API.
- Поддержаны inline-блоки: photo, video, animation, audio, voice_note.
- НЕ поддержаны inline-блоки: sticker, document, video_note, contact, location, poll, dice — для них контент показывается как текст в BlockQuotation (через `_get_message_content_desc`).

**Тесты**: `scripts/test_rich_blocks.py`, `scripts/test_send_report.py` — все блоки сериализуются корректно, send_rich_message вызывается с правильным rich_message, media_block выбирает самый большой photo.

---

### 3. ✅ Добавить Ephemeral Messages (шёпот-предупреждения)

**Что сделано**:
- Добавлен хелпер `_send_ephemeral(bot, chat_id, target, text)`.
- Использует `bot.send_message(chat_id=..., text=..., receiver_user_id=target.id)` — ephemeral видно ТОЛЬКО указанному юзеру.
- При `!warn` — нарушитель получает ephemeral: "⚠️ Дедушка Вобжак выдал тебе варн за: <причина>. Варнов всего: N".
- При `!mute` — нарушитель получает ephemeral: "🔇 Дедушка Вобжак замьютил тебя на <длительность> за: <причина>."
- При `!ban` — нарушитель получает ephemeral: "🚫 Дедушка Вобжак забанил тебя за: <причина>."
- При `!unmute` — нарушитель получает ephemeral: "🔊 Дедушка Вобжак снял мьют. Добро пожаловать обратно!"
- Если ephemeral не доставлен (юзер ограничил их или заблокировал бота) — ошибка логируется как INFO и работа продолжается (нарушитель не должен видеть ошибку).

**Зависимость**: `aiogram==3.30.0` поддерживает `receiver_user_id` в `send_message` (проверено тестами).

**Важно**: `send_rich_message` НЕ поддерживает `receiver_user_id` — поэтому ephemeral реализован через обычный `send_message` (plain text). Это нормально — ephemeral-сообщения обычно короткие.

---

### 4. ✅ Обновить веб-панель под новую модель (без env fallback)

**Что сделано**:
- В `dashboard.html`: убрана колонка/строка `env_report_chat_id`, контекст использует `default_report_chat_id` (из DB chat_id=0).
- В таблице санкций: Content-колонка больше не ссылается на `report_message_id` — показывается только `message_text` (с тултипом на полную строку).
- Добавлена новая секция **"Chat settings"** в дашборде (после Top offenders/moderators, перед Recent sanctions):
  - Таблица с колонками: Chat ID, Scope, Hashtag, Report chat, Warns → mute, Warns → ban, Mute duration.
  - Первая строка — `default` (chat_id=0), помечена бейджем GLOBAL, показывает глобальный report_chat_id или "not set (reports disabled)".
  - Дальше — per-chat настройки (все ChatSettings с chat_id≠0).
  - Если per-chat override не задан — показывается "→ default" (fallback indicator).
  - Если никаких настроек нет — показывается подсказка с командой `/setreport default <chat_id>`.
  - Если есть только default, но нет per-chat overrides — показывается подсказка с командой `/setreport <chat_id> <report_chat_id>`.
- В `user.html`: убран `env_report_chat_id`, контент-колонка показывает только `message_text`.

**Тесты**: `scripts/test_templates.py`, `scripts/test_e2e.py` — все три состояния (default + per-chat, default only, empty) рендерятся корректно.

---

### 5. ✅ Пересоздать архив

**Что сделано**:
- Архив `shadow-logger-dedushka-vobzhak.tar.gz` пересоздан в `/home/z/my-project/download/`.
- `requirements.txt` содержит актуальную версию `aiogram==3.30.0` (поддерживает `send_rich_message` и `receiver_user_id`).

---

## Приоритет выполнения
1. ✅ TODO 1 (убрать env) — сделано
2. ✅ TODO 2 (Rich Messages) — сделано
3. ✅ TODO 3 (Ephemeral) — сделано
4. ✅ TODO 4 (веб-панель) — сделано
5. ✅ TODO 5 (архив) — сделано

---

## Технические заметки

### Aiogram 3.30 и Bot API 10.2
- `aiogram==3.30.0` в requirements.txt — поддерживает и `send_rich_message`, и `receiver_user_id` (проверено инспекцией API).
- `send_rich_message(chat_id, rich_message: InputRichMessage, ...)` — отправляет Rich Message.
- `send_message(..., receiver_user_id: int)` — отправляет ephemeral (только указанному юзеру).
- `send_rich_message` НЕ поддерживает `receiver_user_id` — поэтому ephemeral реализован через обычный `send_message`.
- Bot API 10.2 changelog: https://core.telegram.org/bots/api#bot-api-10-2

### Структура Rich Message blocks (фактический API)
```python
from aiogram.types import (
    InputRichMessage,
    InputRichBlockSectionHeading,    # text: str | RichText..., size: int (1-3)
    InputRichBlockParagraph,          # text: str | RichText...
    InputRichBlockBlockQuotation,     # blocks: list[...], credit: Optional[RichText...]
    InputRichBlockDetails,            # summary: str, blocks: list[...], is_open: Optional[bool]
    InputRichBlockFooter,             # text: str | RichText...
    InputRichBlockPhoto,              # photo: InputMediaPhoto, caption: Optional[RichBlockCaption]
    InputRichBlockVideo,              # video: InputMediaVideo, caption: Optional[RichBlockCaption]
    InputRichBlockAnimation,          # animation: InputMediaAnimation, ...
    InputRichBlockAudio,              # audio: InputMediaAudio, ...
    InputRichBlockVoiceNote,          # voice_note: InputMediaVoiceNote, ...
    InputMediaPhoto,                  # media: str (file_id) | InputFile
    InputMediaVideo,
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaVoiceNote,
)

blocks = [
    InputRichBlockSectionHeading(text="🔇 МУТ", size=2),
    InputRichBlockParagraph(text=f"👤 {full_name} @{username}\n   ID: {user_id}"),
    InputRichBlockParagraph(text=f"📝 Причина: {reason}"),
    InputRichBlockBlockQuotation(
        blocks=[InputRichBlockParagraph(text=message_text)]
    ),
    InputRichBlockPhoto(photo=InputMediaPhoto(media=photo_file_id)),  # inline медиа
    InputRichBlockDetails(
        summary="Доп. инфо",
        blocks=[InputRichBlockParagraph(text=f"Чат: {chat_id}\nДлительность: {dur}")],
    ),
    InputRichBlockFooter(text=f"🕐 {time_str} МСК | {hashtag}"),
]
rich_msg = InputRichMessage(blocks=blocks)
await bot.send_rich_message(chat_id=report_dest, rich_message=rich_msg)
```

### DB-миграция (report_message_id)
- Колонка `report_message_id` оставлена в таблице `punishments` (soft deprecation).
- Новые записи не пишут в неё (значение NULL).
- Старые записи сохраняются для истории.
- Миграция в `init_db()` оставлена как есть — на случай создания БД с нуля.

### Ephemeral messages — ограничения
- `receiver_user_id` работает только в группах и супергруппах (не в личке).
- Сообщение может не дойти, если модератор офлайн или ограничил ephemeral-сообщения.
- `send_message` с `receiver_user_id` поддерживает `parse_mode="HTML"` — поэтому ephemeral модератору использует HTML-mention нарушителя (`<a href="tg://user?id=…">`).
- В v4.1 ephemeral отправляются **только модератору** (`recipient=mod`), нарушитель их не получает — стелс-режим сохраняется.
- Причина во всех ephemeral экранируется через `html.escape(reason, quote=False)`.

---

## Файловая структура (актуальная)
```
shadow-logger/
├── .dockerignore
├── .env.example              # REPORT_CHAT_ID убран, добавлены комменты про /setreport, WEB_PUBLIC_URL
├── Dockerfile
├── requirements.txt          # aiogram==3.30.0
├── bot.py                    # lifespan проверяет default report_chat_id из DB
├── bot_handlers.py           # Rich Messages + Ephemeral + clickable violator/mod/web link + WEB_PUBLIC_URL
├── db.py                     # tg_user_id/tg_first_name/tg_last_name/tg_username в web_users
├── web_app.py                # v4.4.3: +/admin/moderators (CRUD); SU-only; best-effort bot.get_chat
├── scripts/
│   ├── test_v44_rich_report.py      # 29 проверок: Rich-отчёт с кликабельными ссылками
│   ├── test_v44_tgid_create.py      # 82 проверки: TGID-создание админа + смена пароля + удаление
│   ├── test_v44_welcome.py          # 38 проверок: welcome-сообщение новому админу в ЛС
│   └── test_v44_moderators_web.py   # 65 проверок: веб-управление модераторами чатов
└── templates/
    ├── base.html             # + nav link Moderators (SU-only)
    ├── admin.html            # SU → создание/удаление веб-админов по TGID
    ├── admin_moderators.html # v4.4.3: SU → CRUD модераторов чатов
    ├── dashboard.html        # + Chat settings + Change my password
    ├── login.html
    └── user.html
```
