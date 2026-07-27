# Дедушка Вобжак — TODO / Что осталось доделать

## Статус проекта
**Версия**: v4.4.10 (рабочий бот с **объединённой ролевой моделью SU/admin/moderator** (одна сущность WebUser, админ = права во всех публичных чатах, модератор = права только в привязанных чатах), **авто-обнаружение чатов** (bot создаёт chat_settings при добавлении в чат + DM SU), **toggles для чатов** (is_enabled / is_private / is_report_chat — через карточки в /admin/chats), **привязка TG ID к SU** (для получения DM о новых чатах), **edit-chats у модератора** (мультивыбор чатов — не только при создании, но и постфактум), **change-role moderator↔admin** (с авто-очисткой chat_admins при повышении), **удалённая отдельная вкладка Moderators** (объединена с Admins в единую Users), автообновление, стелс-режим, Rich Messages, welcome-DM новому админу/модератору, Ephemeral-подтверждения, стикеры в отчётах, команды !unwarn/!unban, удаление сообщения при !warn, self-service смена пароля, очистка тестовых данных из БД через кнопку в веб-панели SU, **v4.4.8 фикс: бот больше не удаляет обычные ответы модератора в чате (только реальные команды)**, **v4.4.8 disable-chat middleware: при is_enabled=False бот полностью игнорирует ВСЁ в чате (команды, catchall, авто-обнаружение) — как будто бота там нет**, **v4.4.8 delete-chat: кнопка Delete в /admin/chats — бот сам ливает из чата + чистит chat_settings / chat_admins / punishments; chat_id=0 защищён от удаления**, **v4.4.9: при !warn нарушитель получает ephemeral-уведомление (видно только ему через receiver_user_id) с причиной, текущим кол-вом варнов и порогами мьюта/бана — варн перестал быть невидимой санкцией**, **v4.4.10 редизайн отчёта в репорт-чате: структура SectionHeading → Divider → List (нарушитель/причина/веб-профиль) → Divider → Details «📎 Показать медиа» (медиа под сворачиваемым спойлером) → Divider → Details «Доп. инфо» → Divider → Footer; модератор перенесён в Footer (кликабельное имя без приписки «Модератор:»); длинный URL веб-профиля спрятан под «Открыть профиль →» через RichTextUrl; ID нарушителя оформлен как inline-код (моноширинный); Divider'ы разделяют секции — на мобиле больше не «стена текста»**)
**Aiogram**: 3.30.0 (поддерживает Bot API 10.2: `send_rich_message`, `receiver_user_id`, `my_chat_member`)
**Архитектура репорт-чата**: per-chat override → is_report_chat flag → default (chat_id=0) → disabled
**Стелс-режим**: нарушитель НИКОГДА не получает уведомлений от бота; ephemeral видят только модераторы
**Санкции**: !mute / !warn / !ban / !unmute / !unban / !unwarn [N] / !warns / !resetwarns
**Веб-панель**: SU (env WEB_PASSWORD) + мульти-юзер (PBKDF2), автообновление каждые 15с, фильтры (action/revoked/sort), REVOKED-бейджи
**v4.4 web-админы**: создаются SU по TGID, профиль подтягивается из Telegram (`bot.get_chat`), пароль автогенерируется и показывается SU один раз, юзер сам меняет пароль через /dashboard
**v4.4.3 модераторы**: SU может добавлять/удалять модераторов чатов через `/admin/moderators` (SU-only). Команды `/addadmin`, `/deladmin` в боте остаются как fallback. Профиль модератора (имя, @username) подтягивается через `bot.get_chat` best-effort.

---

## ✅ Готово — v4.4.7 (объединённая ролевая модель + авто-обнаружение чатов)

### 28 ✅ Объединение Admins + Moderators в единую вкладку Users

**Проблема v4.4.6**: вкладки "Admins" (`/admin/users`) и "Moderators" (`/admin/moderators`) дублировали функционал и создавали путаницу. Создавались две разные сущности — веб-логин (WebUser) и TG-модератор чата (ChatAdmin), хотя по сути это один и тот же человек с разными правами.

**Что сделано**:
- Удалены роуты `/admin/moderators/*` (GET, POST create, POST delete) и файл `templates/admin_moderators.html`.
- Вкладка "Admins" переименована в "Users" (в навигации SU).
- При создании веб-юзера с ролью `moderator` — добавлен **мультивыбор чатов** (чекбоксы). Для каждого выбранного чата создаётся запись в `chat_admins`, дающая модератору право на `!warn/!mute/!ban` в этих чатах.
- При создании с ролью `admin` — список чатов игнорируется (админ имеет права во всех публичных чатах автоматически).
- Добавлен **edit-chats** — инлайн-форма на странице Users для изменения списка чатов модератора в любой момент (не только при создании).
- Добавлен **change-role** moderator↔admin одной кнопкой. При повышении moderator→admin — автоматически удаляются все его записи из `chat_admins` (они больше не нужны). При понижении admin→moderator — `chat_admins` не трогаются (модератор должен будет сам указать чаты через edit-chats).
- На странице Users появилась отдельная секция "TG-only moderators" — показывает записи `chat_admins` без веб-аккаунта (старый сценарий `/addadmin`). Чтобы дать им веб-доступ — SU создаёт юзера с тем же TG ID, и они автоматически связываются.
- `user_chats` словарь подгружается для каждого moderator-юзера (LEFT JOIN chat_admins + chat_settings) — отображается прямо в таблице юзеров.

### 29 ✅ Унификация прав _is_admin (role × private × enabled)

**Старая логика** (`v4.4.6`):
```python
async def _is_admin(session, chat_id, user_id):
    if user_id in ADMIN_IDS: return True
    return db.is_chat_admin(chat_id, user_id)  # только chat_admins
```

**Новая логика** (`v4.4.7`):
```python
async def _is_admin(session, chat_id, user_id):
    if user_id in ADMIN_IDS: return True            # env глобальные супер-админы
    settings = await _get_chat_settings(session, chat_id)
    if not settings.is_enabled: return False         # чат выключен — никто не модерит
    wu = await get_web_user_by_tg_id(user_id)
    if wu and wu.is_active:
        if wu.role == "su":       return True                    # SU — везде
        if wu.role == "admin":    return not settings.is_private # админ не лезет в private
        if wu.role == "moderator": return db.is_chat_admin(...)  # только привязка
    # Fallback: TG-only модератор (chat_admins без веб-аккаунта) — обратная совместимость
    return db.is_chat_admin(chat_id, user_id)
```

Тесты покрывают все 10 комбинаций: SU×{public, private, disabled} + admin×{public, private, disabled} + moderator×{привязанный, непривязанный, disabled} + moderator без чатов.

### 30 ✅ Авто-обнаружение чатов (my_chat_member + stealth catchall)

**Проблема**: раньше SU должен был вручную добавить чат в `chat_settings` (через `/sethashtag` или веб-панель) — иначе бот работал, но в `/admin/chats` чат не отображался.

**Что сделано в `bot_handlers.py`**:
- Добавлен обработчик `@router.my_chat_member()` — срабатывает, когда бота добавляют/удаляют из чата или повышают/понижают права. При добавлении — создаётся `chat_settings` (если ещё нет) с `title` из TG, флаг `is_enabled=True`, `is_private=False`.
- Дополнительно: `stealth_catchall_group` (catchall для всех сообщений в группах) теперь тоже проверяет наличие `chat_settings` и создаёт при первом сообщении. Это страховка на случай если `my_chat_member` не пришёл (зависит от прав бота).
- Если у SU привязан TG ID — после обнаружения нового чата бот отправляет ему DM: "🆕 Бот добавлен в новый чат: <title> (id). Настройте его в веб-панели."
- `_ensure_chat_settings(session, chat_id, title)` — хелпер, идемпотентный. Возвращает `(settings, created)`. Обновляет `title` если чат переименовали.

**В `bot.py`**: `allowed_updates=["message", "my_chat_member"]` — чтобы Telegram присылал обновления `my_chat_member` на вебхук.

### 31 ✅ Toggles для чатов (is_enabled / is_private / is_report_chat)

**Новые колонки в `chat_settings`**:
| Колонка | Тип | Default | Назначение |
|---------|-----|---------|-----------|
| `title` | String(255), nullable | NULL | Название чата из TG (snapshot) |
| `is_enabled` | Boolean, NOT NULL | True | False = бот полностью игнорирует чат (никакие команды) |
| `is_private` | Boolean, NOT NULL | False | True = закрытый чат (админ-уровень туда не имеет доступа, только SU и привязанные модераторы) |
| `is_report_chat` | Boolean, NOT NULL | False | True = этот чат используется как склад отчётов по умолчанию |

**Новый роут** `POST /admin/chats/{chat_id_str}/toggle` — принимает form-поле `field=enabled|private|report_chat`. Для `report_chat` — автоматически снимает флаг с других чатов (репорт-чат может быть только один).

**Переделанный UI `/admin/chats`**: вместо таблицы с инлайн-формами — сетка карточек. На каждой карточке: название + ID + бейджи (DISABLED / PRIVATE / REPORT + hashtag), статистика (кол-во наказаний + модераторов), 4 кнопки:
- ■ Disable / □ Enable (toggle is_enabled)
- 🔒 Private / 🔓 Public (toggle is_private)
- ★ Report chat / ☆ Make report (toggle is_report_chat)
- ✎ Settings (раскрывает инлайн-форму для hashtag / report_chat_id / thresholds)

Подсказка "⚠ New chat — configure it" для неполностью настроенных чатов.

### 32 ✅ Привязка TG ID к SU

**Проблема**: SU был веб-юзером без TG ID — бот не мог отправлять ему DM о новых чатах.

**Что сделано**:
- Новый роут `POST /admin/users/{user_id}/bind-tg` — принимает form-поле `tg_user_id`. Работает для любого юзера (включая SU).
- Если `tg_user_id` пустой — отвязывает (TG ID = NULL).
- Best-effort: `bot.get_chat(tg_id)` подтягивает `first_name / last_name / @username` и обновляет профиль.
- Проверка уникальности: TG ID не может быть привязан к двум юзерам.
- На странице Users у SU появляется кнопка "Bind TG" (вместо disable/reset/delete).

### 33 ✅ Удаление REPORT_CHAT_ID env зависимости

Раньше приоритет репорт-чата был: per-chat override → default (chat_id=0) → env REPORT_CHAT_ID.
Теперь: per-chat override → any chat with `is_report_chat=True` → default (chat_id=0) → disabled.
Env `REPORT_CHAT_ID` больше не нужен (но остаётся как legacy fallback в коде — на случай если кто-то не мигрировал).

### 34 ✅ Тесты v4.4.7

**Новый файл**: `scripts/test_v44_users_web.py` (64 checks):
1. GET /admin/users — страница содержит radio buttons + chatsBlock
2. POST /admin/users/create — moderator с chat_ids (создаёт WebUser + 2 chat_admins)
3. POST /admin/users/create — admin (chat_ids игнорируется)
4. POST /admin/users/{id}/edit-chats — обновление списка чатов модератора
5. POST /admin/users/{id}/role — moderator→admin чистит chat_admins
6. POST /admin/users/{id}/bind-tg — привязка TG к SU
7. Старые роуты /admin/moderators/* возвращают 404
8. Nav: только SU видит Users
9. /admin/users access control (SU OK; admin/moderator → redirect)
10. TG-only модераторы отображаются отдельной секцией
11. POST /admin/users/{id}/toggle — disable/enable
12. SU нельзя заблокировать через toggle
13. /admin/chats/{id}/toggle — enabled/private/report_chat (со снятием флага с других)
14. _is_admin — унифицированная проверка прав (10 комбинаций role × private × enabled)
15. Welcome DM отправляется новому moderator

**Обновлённые тесты**:
- `test_v44_roles.py` — поправлен nav (теперь без Moderators), `Edit` → `Settings`.
- `test_v44_cleanup_web.py` — INSERT в chat_settings добавлены новые NOT NULL колонки.
- `test_v44_moderators_web.py` — удалён (функционал перенесён в `test_v44_users_web.py`).

**Итого**: 354 проверки, 0 ошибок (67 cleanup_web + 29 rich_report + 67 roles + 82 tgid_create + 64 users_web + 38 welcome + 7 cleanup_test_data pytest).

---

## ✅ Готово — v4.4.8 (фикс удаления обычных ответов модератора)

### 35 ✅ Guard-проверка `_is_moderation_command` перед удалением

**Проблема v4.4.7**: в `handle_group_command` бот удалял сообщение модератора **до того**, как проверял, что это вообще команда. Поэтому любой ответ модератора в чате бесследно исчезал. Симптом: «Бот удаляет ВСЕ сообщения если модератор кому то отвечает».

**Что сделано**:
- Добавлена функция `_is_moderation_command(text)` — быстрая проверка, что текст является одной из 8 команд бота (`!mute`, `!warn`, `!ban`, `!unmute`, `!unban`, `!unwarn`, `!warns`, `!resetwarns`).
- Использует те же precompiled regex-ы, что и основной обработчик — рассинхронизация невозможна.
- Short-circuit по `!` в начале строки — для не-команд return происходит мгновенно, без regex-матчей.
- В `handle_group_command` добавлена ранняя guard-проверка: `if not _is_moderation_command(text): return` — ПЕРЕД удалением сообщения.
- Добавлены 26 unit-кейсов + интеграционный тест (mock-сообщения): обычный ответ модератора НЕ удаляется, реальная команда `!warn` — удаляется как и раньше.

**Затронутые файлы**:
- `bot_handlers.py`: добавлены `_ALL_MOD_COMMANDS` tuple и функция `_is_moderation_command`, изменён `handle_group_command`.

---

## ✅ Готово — v4.4.8 (disable-chat middleware + delete-chat)

### 36 ✅ `_DisabledChatMiddleware` — бот полностью игнорирует disabled-чаты

**Проблема**: при установке метки Disable (is_enabled=False) бот формально переставал выполнять модераторские команды (`_is_admin` возвращал False), но продолжал «воспринимать» чат: `stealth_catchall_group` создавал/обновлял `chat_settings`, логировал, и т.д. Это не соответствовало ожиданиям пользователя: «бот ПЕРЕСТАЁТ ВОСПРИНИМАТЬ ВООБЩЕ ВСЁ происходящее в чате».

**Что сделано**:
- Добавлен класс `_DisabledChatMiddleware(BaseMiddleware)` в `bot_handlers.py`.
- Зарегистрирован как `router.message.outer_middleware(...)` — выполняется ДО любых фильтров и хэндлеров.
- Для group/supergroup сообщений проверяет `chat_settings.is_enabled`:
  - Если settings нет — пропускает (catchall создаст).
  - Если `is_enabled=False` — молча `return` (short-circuit), handler не вызывается.
  - Если `is_enabled=True` — пропускает к handler'ам.
- Личные сообщения не фильтруются (у них нет chat_settings).
- `my_chat_member` updates НЕ затрагиваются (важно по-прежнему ловить добавление бота обратно в чат).
- Fail-open: при ошибке БД логируем warning и пропускаем сообщение (не кладём бота).

**Затронутые файлы**:
- `bot_handlers.py`: добавлен `BaseMiddleware` в import, класс `_DisabledChatMiddleware`, регистрация middleware.

### 37 ✅ Кнопка Delete в `/admin/chats` — бот ливает + чистит БД

**Проблема**: в `/admin/chats` нельзя было удалить тестовые чаты. Они копились в БД (создавались ботом автоматически при добавлении в тестовые чаты) и засоряли список.

**Что сделано**:
- Добавлен маршрут `POST /admin/chats/{chat_id}/delete` в `web_app.py` (доступ: `require_admin`).
- Логика:
  1. Валидация `chat_id` (должен быть числом).
  2. Защита: `chat_id=0` (глобальные дефолтные настройки) нельзя удалять — redirect с flash `Cannot delete default settings`.
  3. Если chat_settings не найден — redirect с flash `not found`.
  4. Считаем кол-во удаляемых punishments и chat_admins (для лога/flash).
  5. Удаляем из БД: `punishments`, `chat_admins`, `chat_settings` для этого chat_id.
  6. Best-effort: `await bot.leave_chat(chat_id=chat_id)`. Если падает (бота уже кикнули / нет прав) — логируем warning, но БД всё равно чистится.
- В шаблон `admin_chats.html` добавлена кнопка `✕ Delete` (красная, с `confirm()` dialog, показывающим сколько punishments/admins будет удалено).
- Кнопка не показывается для `chat_id=0` (внутри `{% if c.chat_id != 0 %}` блока).
- Обновлён описательный текст вверху страницы: уточнено, что Disable = «bot completely ignores everything», добавлено описание Delete.

**Затронутые файлы**:
- `web_app.py`: добавлен маршрут `admin_chats_delete`.
- `templates/admin_chats.html`: добавлена кнопка Delete + обновлён help-текст.

**Тесты**: `scripts/test_v448_disable_delete.py` — 16 проверок (5 middleware + 5 `_is_moderation_command` regression + 6 delete route). Все 16 PASS.

---

## ✅ Готово — v4.4.9 (уведомление нарушителю при !warn)

### 38 ✅ Ephemeral-уведомление нарушителю при `!warn` (видно только ему)

**Проблема v4.4.8 и ранее**: при `!warn` нарушитель вообще не получал никаких уведомлений — ни публичных, ни приватных. Модератор видел ephemeral «✅ Варн выдан …», в репорт-чат летел подробный отчёт, а сам нарушитель оставался в неведении. Это делало варн **бессмысленной санкцией**: юзер не знал, что его предупредили, и не мог скорректировать поведение.

**Что сделано**:
- Добавлена функция `_send_user_warn_notification()` в `bot_handlers.py` — отправляет ephemeral-сообщение **самому нарушителю** через `receiver_user_id=target.id` (Bot API 10.2 / aiogram 3.30).
- Сообщение видно **только target-юзеру**, остальные участники чата его не видят. Это та же механика, что используется для ephemeral-подтверждений модераторам, но в обратную сторону — адресат теперь наказанный.
- Содержимое сообщения:
  - Заголовок «⚠️ Вам выдано предупреждение»
  - Причина (HTML-escaped)
  - Текущее кол-во варнов
  - Если настроены пороги `warns_to_mute`/`warns_to_ban` — показываем их («Лимиты: мьют при 3, бан при 5.»)
  - Если юзер подошёл к границе в 1 шаг до мьюта/бана — дополнительное предупреждение («⚠️ Следующий варн — бан.»)
  - Если уже превысил — «Вы превысили лимит варнов — возможен бан.»
- Вызов встроен в обработчик `!warn` между отправкой репорта и ephemeral-подтверждением модератору. Порядок: удалить сообщение нарушителя → репорт в склад → **уведомить нарушителя** → подтвердить модератору → проверить пороги.
- Если отправка не удалась (юзер ограничил ephemeral-сообщения / заблокировал бота) — тихо логируем и продолжаем. Варн в БД уже сохранён.
- **Стелс-режим сохраняется** для всех кроме наказанного: остальные участники чата по-прежнему не видят от бота никаких сообщений и не догадываются о его существовании. Только сам нарушитель теперь знает, что его предупредили.

**Затронутые файлы**:
- `bot_handlers.py`: добавлена функция `_send_user_warn_notification`, изменён обработчик `!warn` (добавлен вызов + получение `chat_settings` для порогов), обновлён docstring в шапке модуля (описано v4.4.9 исключение из стелс-режима).

**Тесты**: `scripts/test_v449_warn_notify.py` — проверяет что при `!warn`:
  1. Вызывается `bot.send_message` с `receiver_user_id=target.id` (нарушитель получает уведомление).
  2. Также вызывается `bot.send_message` с `receiver_user_id=mod.id` (модератор получает подтверждение).
  3. В тексте нарушителю есть причина и кол-во варнов.
  4. Если пороги настроены — в тексте есть информация о лимитах.
  5. При `!mute` и `!ban` уведомление нарушителю НЕ отправляется (только модератору).

---

## ✅ Готово — v4.4.10 (редизайн отчёта в репорт-чате)

### 39 ✅ Редизайн Rich-отчёта под мобильную версию Telegram

**Проблема v4.4.9**: модераторы жаловались, что отчёт в репорт-чате «выглядит странно» на мобильной версии Telegram. По скриншоту выяснили 4 конкретные проблемы:
1. Длинный URL `https://degraban.bothost.tech/user/95354253` ломался посередине на мобиле, образуя «лесенку».
2. Эмодзи-маркеры (👤, 👮, 🌐, 📝) не были выровнены по левому краю — каждый `Paragraph` был отдельной строкой, и из-за разной длины labels иконки визуально «плыли».
3. `BlockQuotation` (контент нарушителя) «прибит» к фото без разделителя — на мобиле они склеивались в один визуальный блок.
4. `Details` (Доп. инфо) выглядел orphan-блоком без визуальной связи с остальным контентом.

Дополнительное требование: **прятать все изображения под спойлер** — в сообщениях нарушителей бывает шок-контент, который модераторы не хотят видеть сразу при открытии репорт-чата.

**Что сделано (Вариант B — «Список с разделителями»)**:

Полностью переписана функция `_send_report()` в `bot_handlers.py`. Новая структура блоков:

```
SectionHeading   — 🔇 МУТ / 🚫 БАН / ⚠️ ВАРН / 🔊 РАЗМУТ
Divider          — горизонтальная линия
List             — список ключевых полей (нативные буллеты):
  • 👤 Иван Бацуев @VoronVan  ID: 95354253    ← RichTextUrl + RichTextCode
  • 📝 <причина>
  • 🌐 Открыть профиль →                       ← короткий текст вместо URL
Divider
Details          — «📎 Показать медиа» (is_open=False):
  BlockQuotation — текст/caption нарушителя (если есть)
  Photo/Video/Animation — медиа (под спойлером)
Divider
Details          — «Доп. инфо» (чат/длительность/варнов всего)
Divider
Footer           — 🕐 время | #хэштег | Gleb   ← кликабельное имя модератора
```

**Конкретные изменения**:
1. **`InputRichBlockList` + `InputRichBlockListItem`** — основной контент теперь список, а не набор отдельных параграфов. Эмодзи-маркеры выровнены самим Telegram (нативный буллет), больше не «плывут».
2. **`InputRichBlockDivider`** — горизонтальные линии между секциями. Визуально разделяют «Заголовок / Тело / Медиа / Доп. инфо / Футер», на мобиле больше не «стена текста».
3. **`RichTextCode` для ID** — `ID: 95354253` оформлен как inline-код (моноширинный). Выделяется визуально, легко копируется долгим тапом на мобильном.
4. **`RichTextUrl(text="Открыть профиль →")`** — длинный URL веб-профиля спрятан под коротким текстом. URL больше не ломается посередине, не занимает 2-3 строки. (URL берётся из env `WEB_PUBLIC_URL`, например `https://degraban.bothost.tech/user/<id>`.)
5. **`InputRichBlockDetails(summary="📎 Показать медиа", is_open=False)`** — все медиа (фото/видео/гиф) обёрнуты в сворачиваемый блок. По умолчанию **свёрнут** — модератор не видит шок-контент пока не тапнет. Text_content нарушителя идёт внутри Details как `BlockQuotation`, медиа — следующим блоком.
6. **Модератор перенесён в Footer** — раньше был отдельный параграф с эмодзи 👮 и припиской «Модератор:». Теперь просто кликабельное имя (`RichTextUrl(text=mod_name, url="tg://user?id=…")`) в Footer'е рядом со временем и хэштегом. Экономит место, выглядит чище. Если `mod=None` (автоматическая санкция) — Footer просто без модератора.
7. **Plain-text fallback обновлён** — модератор идёт в самом конце (после времени), без приписки, просто имя + @username если есть. URL веб-профиля тоже под короткой подписью «🌐 Открыть профиль:».

**Импорты добавлены**:
- `InputRichBlockList`, `InputRichBlockListItem` — для основного списка полей.
- `InputRichBlockDivider` — для разделителей между секциями.
- `RichTextCode` — для моноширинного ID нарушителя.

**Что НЕ сделано (намеренно)**:
- Кнопки быстрых действий (Разбанить/Снять варн/Скопировать ID) — пользователь явно сказал «кнопки никакие не нужны».
- Настоящий Telegram-спойлер через `has_spoiler=True` — в Rich Messages нет параметра спойлера для inline-медиа, поэтому используется сворачиваемый `Details` (это и есть «спойлер» в смысле Bot API 10.2 rich-сообщений: скрыт по умолчанию, разворачивается по тапу).
- Стикеры оставлены как отдельное сообщение после rich-отчёта (без `has_spoiler`) — стикеры редко бывают шок-контентом, и у Rich Messages нет inline-блока для стикеров.

**Затронутые файлы**:
- `bot_handlers.py`: переписана функция `_send_report()` (строки ~698-950), обновлён `_send_report_plain_fallback()` (строки ~953-997), обновлён docstring в шапке модуля (v4.4.10 changelog entry), добавлены 3 новых импорта.

**Тесты**: `scripts/test_v4410_report_redesign.py` — проверяет что:
  1. Структура блоков: `[section_heading, divider, list, divider, details, divider, details, divider, footer]`.
  2. В List ровно 3 ListItem'а (нарушитель/причина/веб-профиль) если есть причина и WEB_PUBLIC_URL.
  3. ListItem нарушителя содержит RichTextUrl (кликабельное имя) и RichTextCode (моноширинный ID).
  4. ListItem веб-профиля содержит RichTextUrl с коротким текстом «Открыть профиль →» (НЕ полный URL).
  5. Details «📎 Показать медиа» имеет `is_open=False` (свёрнут по умолчанию).
  6. Внутри Details есть media_block (если было медиа) и BlockQuotation (если был text_content).
  7. Footer содержит RichTextUrl с `tg://user?id=<mod_id>` (кликабельное имя модератора).
  8. Footer НЕ содержит приписку «Модератор:».
  9. Если `mod=None` — Footer без RichTextUrl (просто время + хэштег).
  10. Если `reply_to_message=None` — Details «📎 Показать медиа» отсутствует (медиа нет).
  11. Plain-text fallback: модератор идёт в самом конце, после времени, через `|`.

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

## ✅ Готово — v4.4.4 (Скрипт очистки тестовых данных)

### 30 ✅ `scripts/cleanup_test_data.py` — безопасная очистка БД от тестового мусора

**Проблема**: при тестировании бота в реальной БД накапливаются тестовые нарушители, варны/мьюты/баны, тестовые доп. админы чатов. Удалять вручную через `sqlite3` опасно — можно случайно снести модераторов или веб-админов.

**Что сделано в `scripts/cleanup_test_data.py`**:
- Standalone Python-скрипт (без зависимостей от проекта — только stdlib `sqlite3`, `shutil`, `argparse`).
- Работает прямо с SQLite-файлом (через `DB_PATH` env, по умолчанию `/app/data/shadow_logs.db`).

**Логика удаления**:
| Таблица | Действие | Почему |
|---------|----------|--------|
| `punishments` | **УДАЛИТЬ ВСЕ** | Тестовые варны/мьюты/баны/unmute/unwarn/unban |
| `users` | **Удалить тех, кто НЕ в `moderators`** | Тестовые нарушители; модератор, случайно попавший в users, сохраняется |
| `chat_admins` | **Опционально** (флаг `--include-chat-admins`) | По умолчанию настройки чатов не трогаем |
| `moderators` | **СОХРАНИТЬ** | Модераторы Telegram |
| `web_users` | **СОХРАНИТЬ** | SU + веб-админы/модераторы |
| `chat_settings` | **СОХРАНИТЬ** | Per-chat конфиги (хэштег, пороги, report-chat) |

**Безопасность**:
1. **Dry-run по умолчанию**: без флага `--apply` скрипт только показывает что было бы удалено.
2. **Бэкап**: перед реальным удалением создаётся `<db>.backup-YYYYMMDD-HHMMSS.db` в той же папке.
3. **Защита от пустой БД**: если в `moderators` И `web_users` нет ни одной записи — скрипт отказывается работать (защита от случайного запуска на свежей БД).
4. **VACUUM** после удаления — файл БД сжимается.
5. **Foreign keys ON**: удаления идут в правильном порядке (сначала `punishments`, потом `users`).

**Использование**:
```bash
# Dry-run (БЕЗ изменений):
python scripts/cleanup_test_data.py

# Реальная очистка:
python scripts/cleanup_test_data.py --apply

# Также очистить chat_admins:
python scripts/cleanup_test_data.py --apply --include-chat-admins

# Свой путь к БД:
DB_PATH=/path/to/shadow_logs.db python scripts/cleanup_test_data.py --apply
```

**Восстановление из бэкапа**:
```bash
cp /app/data/shadow_logs.db.backup-20260726-153000.db /app/data/shadow_logs.db
```

**Тесты** (`scripts/test_cleanup_test_data.py`, 7 проверок):
1. Dry-run не меняет данные.
2. Apply очищает `punishments` полностью + тестовых `users` (но сохраняет модератора, попавшего в `users`).
3. `--include-chat-admins` очищает `chat_admins`.
4. Бэкап создаётся с правильным именем.
5. VACUUM срабатывает (по сообщению в stdout).
6. На пустой БД (нет модераторов и веб-юзеров) — отказ с exit code 3.
7. На отсутствующей БД — exit code 2.

**Запуск в Docker** (если бот крутится в контейнере):
```bash
# Узнать имя контейнера:
docker ps | grep degrab

# Dry-run:
docker exec -it <container> python /app/scripts/cleanup_test_data.py

# Apply:
docker exec -it <container> python /app/scripts/cleanup_test_data.py --apply
```
Бэкап создаётся внутри контейнера по пути `/app/data/shadow_logs.db.backup-*.db`. Чтобы вытащить его на хост: `docker cp <container>:/app/data/shadow_logs.db.backup-XXXX.db ./`

**Не требует перезапуска бота** — скрипт работает с SQLite-файлом напрямую (WAL-режим позволяет конкурентный доступ).

---

## ✅ Готово — v4.4.5 (Очистка тестовых данных через веб-панель SU)

### 31 ✅ Кнопка "Cleanup" в веб-панели (SU-only)

**Контекст**: предыдущая версия добавила standalone-скрипт `scripts/cleanup_test_data.py`, но пользователю удобнее иметь кнопку прямо в админке — не нужно заходить по SSH в контейнер.

**Что сделано**:

1. **`templates/admin_cleanup.html`** (новый, 167 строк):
   - Раздел **Preview**: live-таблица с текущими счётчиками всех 6 таблиц, для каждой — действие (`DELETE ALL` / `DELETE non-moderators` / `PRESERVED`) и количество затронутых строк.
   - Раздел **Apply**: форма с чекбоксом `include_chat_admins` (по умолчанию off) и кнопкой подтверждения.
   - Раздел **Result** (после POST): зелёный блок со статистикой (`Punishments removed: N`, `Test users removed: N`, `Backup file: <name>`) и инструкцией по восстановлению.
   - Раздел **Safety**: подробная справка что сохраняется, что удаляется, операционные заметки (WAL, FK, VACUUM).
   - JavaScript `confirm()` в `onsubmit` с подробным текстом что произойдёт — защита от случайного клика.
   - Если `moderators=0 AND web_users=0` — кнопка задизейблена, форма semi-transparent, показан red warning "Refusing to apply".

2. **`web_app.py`** (+197 строк):
   - Импорты `sqlite3`, `shutil` добавлены.
   - Импортирован `DB_PATH` из `db.py`.
   - Хелпер `_cleanup_counts(conn)` — возвращает словарь счётчиков (7 ключей, включая `users_to_delete`).
   - `GET /admin/cleanup` (SU-only):
     - Открывает SQLite напрямую, считает live-счётчики.
     - Рендерит `admin_cleanup.html` с превью.
     - Если файла БД нет — рендерит с нулями (dev-режим).
   - `POST /admin/cleanup` (SU-only):
     1. Проверяет что БД существует.
     2. Pre-flight: считает счётчики ДО.
     3. **Safety**: если `moderators == 0 AND web_users == 0` → отказ с flash, без бэкапа и удаления.
     4. **Backup**: `shutil.copy2(DB_PATH, "<DB>.backup-YYYYMMDD-HHMMSS-microsec.db")` — timestamp с микросекундами против коллизий при быстрых повторных вызовах.
     5. **Delete** (в одной транзакции):
        - `DELETE FROM punishments` (все)
        - `DELETE FROM users WHERE user_id NOT IN (SELECT mod_id FROM moderators)` (сохраняет модераторов)
        - Если `include_chat_admins` — `DELETE FROM chat_admins`
     6. **VACUUM** (вне транзакции — SQLite требует).
     7. Post-counts, логирование, рендер страницы с блоком результата.

3. **`templates/base.html`** (+1 строка):
   - Nav link `Cleanup` добавлен, виден только SU (внутри `{% if auth_user.is_su %}`).

4. **`scripts/cleanup_test_data.py`** (правка):
   - Timestamp в имени бэкапа теперь `%Y%m%d-%H%M%S-%f` (с микросекундами) — для консистентности с веб-версией.

**Безопасность**:
| Мера | Где |
|------|-----|
| SU-only (require_su) | Оба эндпоинта |
| `confirm()` JS перед сабмитом | Форма |
| Чекбокс `include_chat_admins` off по умолчанию | Форма |
| Бэкап SQLite-файла перед удалением | POST handler |
| Refuse на пустой БД (no mods + no web_users) | POST handler |
| Foreign keys ON — удаления в правильном порядке | POST handler |
| VACUUM после удаления | POST handler |
| Бот продолжает работать во время очистки (WAL) | Архитектурно |

**Что удаляется / что сохраняется**:
| Таблица | Действие | Логика |
|---------|----------|--------|
| `punishments` | **DELETE ALL** | Все тестовые варны/мьюты/баны |
| `users` | **DELETE non-moderators** | `WHERE user_id NOT IN (SELECT mod_id FROM moderators)` — модератор, случайно попавший в `users`, сохраняется |
| `chat_admins` | **Опционально DELETE ALL** | Только если чекбокс `include_chat_admins` отмечен |
| `moderators` | **PRESERVED** | Все модераторы Telegram |
| `web_users` | **PRESERVED** | SU + все админы/модераторы веб-панели |
| `chat_settings` | **PRESERVED** | Per-chat конфиги (хэштег, пороги, report-chat) |

**Тесты** (`scripts/test_v44_cleanup_web.py`, 67 проверок):
1. GET /admin/cleanup — страница SU с превью, содержит все элементы (form, checkbox, confirm, backup, VACUUM, PRESERVED, DELETE ALL).
2. POST без `include_chat_admins` — удаляет punishments + test users, сохраняет moderators/web_users/chat_settings/chat_admins.
3. POST с `include_chat_admins=1` — также очищает chat_admins.
4. Backup создаётся: 1 новый `.backup-*.db` файл, содержит snapshot данных ДО удаления (punishments=3), имя содержит timestamp.
5. Защита: на полностью пустой БД (no moderators + no web_users) → POST без auth редиректит на /login; с auth (только SU, без модераторов) → cleanup выполняется (т.к. web_users > 0).
6. Non-SU → 303 redirect на /dashboard (как GET, так и POST). Данные не меняются.
7. Неавторизованный → 303 redirect на /login (как GET, так и POST).
8. HTML: nav link "Cleanup" виден SU, не виден non-SU.
9. VACUUM — файл БД уменьшается после очистки (500 filler-rows + VACUUM).
10. HTML: форма содержит `onsubmit="return confirm(...)"` с текстом "Apply cleanup".
11. Идемпотентность: повторный POST на уже очищенной БД не ломает структуру, moderators сохраняются.
12. Backup — валидный SQLite, `PRAGMA integrity_check = 'ok'`, содержит все 6 таблиц.

**Запуск из браузера**:
1. SU логинится в https://degraban.bothost.tech/login
2. Кликает "Cleanup" в навбаре
3. Видит live-превью (какие таблицы и сколько строк будут затронуты)
4. (Опционально) отмечает "Also clear chat_admins"
5. Жмёт "⚠ Apply cleanup" → JS confirm с подробным текстом
6. Видит результат: сколько удалено, какой бэкап создан, как восстановиться

**Восстановление из бэкапа** (если что-то пошло не так):
```bash
# На сервере:
docker cp <container>:/app/data/shadow_logs.db.backup-20260726-153000-123456.db ./
docker cp ./shadow_logs.db.backup-20260726-153000-123456.db <container>:/app/data/shadow_logs.db
docker restart <container>
```

**Standalone-скрипт остаётся** как fallback для SSH-доступа (без веб-панели), см. v4.4.4 выше.

---

## ✅ Готово — v4.4.6 (Ролевая модель SU / admin / moderator + управление чатами)

### 32 ✅ Три уровня доступа в веб-панели

**Контекст**: до v4.4.6 в веб-панели было только 2 уровня — SU (через env WEB_PASSWORD) и "админ" (созданный через /admin/users). Все админы имели одинаковые права. Пользователь захотел:
- **SU** — весь доступ (как сейчас)
- **admin** — управление чатами (хэштеги, пороги), модераторами чатов, без управления админами
- **moderator** — только просмотр логов в веб-панели

**Что сделано**:

1. **`db.py`** (+30 строк):
   - В модель `WebUser` добавлена колонка `role` (`String(16)`, default `'admin'`).
   - В `init_db()` миграция: `ALTER TABLE web_users ADD COLUMN role VARCHAR(16) NOT NULL DEFAULT 'admin'`, затем `UPDATE web_users SET role='su' WHERE is_su=1`.
   - При seed SU — явно `role='su'`. На случай если SU существовал до миграции — `if existing_su.role != "su": existing_su.role = "su"; commit`.

2. **`web_app.py`** (правки в auth + новый раздел /admin/chats):
   - Токен сессии расширен полем `r` (role). Старые токены (без `r`) — fallback: `is_su=True → role='su'`, иначе `'admin'`.
   - `AuthUser` получил поле `role` (`__slots__` расширен).
   - `require_auth` теперь берёт роль **из БД** (а не из токена) — если SU понизил роль пользователю, следующий запрос пользователя увидит новую роль без перелогина.
   - **`require_su`** — только `role='su'` (для `/admin/users`, `/admin/cleanup`).
   - **`require_admin`** (новый dependency) — `role in ('su', 'admin')`. Moderator → 303 redirect на `/dashboard`.
   - `/admin/moderators*` (GET, POST create, POST delete) — сменён с `require_su` на `require_admin`. Теперь admin может управлять модераторами чатов.
   - `/admin/users/create` — добавлен параметр `role: str = Form("admin")`. Валидируется: `'admin'` или `'moderator'`. Невалидное значение → flash, WebUser не создаётся.
   - `_send_admin_welcome(bot, tg_user_id, login, password, first_name, role='admin')` — добавлен параметр `role`. Текст приветствия адаптируется:
     - **admin**: `"🎉 Доступ к веб-панели (админ)"` + `"Ваши права: управление модераторами чатов и настройками чатов (хэштег, пороги варнов), а также просмотр логов."`
     - **moderator**: `"🔎 Доступ к веб-панели (модератор)"` + `"Ваши права: только просмотр логов нарушителей (раздел Dashboard). Управление админами, чатами и модераторами недоступно."`
   - Логирование при создании: добавлено `role=%s`.

3. **`templates/admin.html`** (формa + список):
   - В форму `/admin/users/create` добавлены 2 radio-кнопки (admin/moderator) с описаниями. `admin` отмечен по умолчанию.
   - Кнопка переименована с "Create admin" на "Create user".
   - В таблице существующих юзеров колонка "Role" теперь показывает: `super-user` (для SU), `admin` (дефолт), `moderator` (с цветом warn).

4. **`templates/admin_chats.html`** (новый, 175 строк):
   - Таблица всех `chat_settings` (включая default `chat_id=0`).
   - Колонки: Chat ID, Hashtag, Report chat, Warns→mute, Mute duration (с human-readable суффиксом), Warns→ban, Punishments (count из `punishments`), Updated.
   - Кнопка "Edit" раскрывает inline-форму с 5 полями (hashtag, report_chat_id, warns_to_mute, mute_duration_seconds, warns_to_ban) + Save / Cancel.
   - Раздел "Help" с пояснением каждого поля и логики report-chat (empty/0/specific ID).

5. **`/admin/chats` (GET) + `/admin/chats/{chat_id_str}/update` (POST)** — новые эндпоинты (`require_admin`):
   - GET — список всех `chat_settings` + кол-во наказаний по каждому чату.
   - POST — валидация: `hashtag` (max 64 chars, auto-prefix `#`), `report_chat_id` (пусто → NULL, иначе число ≥ -10¹⁵ — TG supergroup IDs отрицательные), `warns_to_mute`/`mute_duration_seconds`/`warns_to_ban` (≥ 0).
   - `chat_id_str` строкой — Starlette `int`-конвертер не парсит минус, парсим вручную.
   - Логирование: `admin_chats_update: chat_id=%s updated by=%s (hashtag=%s, ...)`.

6. **`templates/base.html`** (навигация):
   - Nav link `Chats` добавлен, виден SU + admin (внутри `role in ('su', 'admin')`).
   - Nav links `Admins`, `Cleanup` — только SU (`role == 'su'`).
   - Nav links `Moderators`, `Chats` — SU + admin.
   - User-chip теперь показывает роль: `SU` (синий), `ADMIN` (зелёный), `MOD` (жёлтый).

**Ролевая матрица доступа**:

| Endpoint | SU | admin | moderator |
|----------|----|----|-----------|
| `/dashboard`, `/user/{id}`, `/api/*`, `/me/password` | ✅ | ✅ | ✅ |
| `/admin/moderators` (GET, POST create, POST delete) | ✅ | ✅ | ❌ → /dashboard |
| `/admin/chats` (GET), `/admin/chats/{id}/update` (POST) | ✅ | ✅ | ❌ → /dashboard |
| `/admin/users` (GET, POST create, toggle, reset, delete) | ✅ | ❌ → /dashboard | ❌ → /dashboard |
| `/admin/cleanup` (GET, POST) | ✅ | ❌ → /dashboard | ❌ → /dashboard |

**Тесты** (`scripts/test_v44_roles.py`, 70 проверок):
1. Миграция: SU→`role='su'`, старый admin→`role='admin'` после `init_db`.
2. AuthUser: role из БД, не из токена.
3. `require_su`: SU OK, admin/moderator → /dashboard.
4. POST `/admin/users/create` с `role=moderator` — создаётся moderator.
5. POST с `role=admin` (явно и по умолчанию) — создаётся admin.
6. POST с невалидной `role='superadmin'` — flash, WebUser не создаётся.
7. HTML форма содержит radio buttons (admin checked по умолчанию).
8. GET `/admin/chats` — SU+admin OK, moderator → /dashboard. HTML содержит #Test, Edit, form action.
9. POST `/admin/chats/{id}/update` — обновляет hashtag/report_chat_id/warns_to_mute/mute_duration/warns_to_ban.
10. POST update от moderator — rejected.
11. POST update с невалидным `warns_to_mute='abc'` — flash error.
12. Welcome DM: для moderator — "модератор" + "только просмотр"; для admin — "админ" + "управление модераторами".
13. Nav visibility: SU видит Dashboard/Moderators/Chats/Admins/Cleanup; admin видит Dashboard/Moderators/Chats (НЕ Admins/Cleanup); moderator видит только Dashboard.
14. `/admin/users` список показывает role каждого юзера.
15. Role change: если SU понижает admin → moderator, следующий запрос этого пользователя увидит новую роль (т.к. require_auth читает из БД).

**Адаптированы существующие тесты**:
- `test_v44_moderators_web.py` test_non_su_access — теперь проверяет что **admin имеет** доступ, а **moderator не имеет** (раньше проверял что non-SU не имеет).
- `test_v44_moderators_web.py` test_nav_link_present — добавлены проверки admin видит Moderators, moderator не видит Moderators/Admins/Cleanup.
- `test_v44_cleanup_web.py` _seed_test_data — добавлено `role='admin'` в INSERT web_users.
- `test_cleanup_test_data.py` (pytest) — схема `web_users` расширена колонкой `role`.

**Backward compatibility**:
- Старые сессии (токены без `r`) — валидны, role вычисляется из `is_su`/`s` поля.
- Старые записи web_users без `role` — миграция проставит `'admin'` (для не-SU) или `'su'` (для SU).
- Все существующие тесты проходят без правок кода (только schema/seed правки).

**Welcome DM для модераторов теперь работает**: когда SU создаёт модератора через веб-панель, бот отправляет в ЛС Rich-сообщение с паролем под спойлером + пояснением "только просмотр логов". Это и было задачей A из прошлой сессии (DM новому админу), расширенное до модераторов.

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
