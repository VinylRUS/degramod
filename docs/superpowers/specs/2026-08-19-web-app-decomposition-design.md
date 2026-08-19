# Декомпозиция `create_app()` — дизайн (Task 10)

**Дата:** 19 августа 2026
**Статус:** дизайн утверждён, реализация не начата
**Задача:** Task 10 плана `docs/superpowers/plans/2026-08-16-degramod-stabilization.md`
**Базовая версия:** v4.9.0 (`4e3d5b2`)

---

## 1. Проблема

`web_app.py:772` — функция `create_app()` длиной 4135 строк, внутри которой
объявлены 47 роутов. Все они замкнуты на локальные переменные `bot` и
`templates`, поэтому вынести их «просто так» нельзя: снаружи замыкания этих
имён не существует.

Файл целиком — 4906 строк, то есть на `create_app()` приходится 84%.

Декомпозиция начата в v4.8.9–v4.8.10: создан пакет `web/`, вынесено 7 роутов
из 54. Оставшиеся 47 — предмет этой спеки.

### Почему это откладывалось

План (§ Self-Review) фиксировал причину прямо: точные швы декомпозиции не
видны, пока поведение не зафиксировано тестами, и «писать сейчас конкретные
сигнатуры для 4000 строк роутов означало бы выдумывать их». Условие снято:
сюита из 65 файлов зелёная целиком, `known_failing.txt` пуст.

---

## 2. Что уже вынесено (v4.8.9–v4.8.10)

| Модуль | Роуты | Версия |
|---|---|---|
| `web/health.py` | `GET /health` | v4.8.9 |
| `web/auth.py` | `GET /logout` | v4.8.9 |
| `web/me.py` | `GET /`, `GET /avatar/{tg_user_id}` | v4.8.10 |
| `web/api.py` | `GET /api/presets`, `GET /api/automute-count` | v4.8.10 |

`web/deps.py` — фасад, реэкспортирующий из `web_app.py`: `AuthUser`,
`APP_VERSION`, `COOKIE_NAME`, `require_auth`, `require_su`, `require_admin`,
`require_csrf_auth`, `require_csrf_su`, `require_csrf_admin`.

Выносились роуты, не требующие ни `templates`, ни `bot`, — то есть именно те,
где замыкание не мешало.

---

## 3. Инвентаризация: 47 роутов

Замер по коду на `4e3d5b2`. Строки — от декоратора до начала следующего.

### 3.1. Сводка

| Целевой модуль | Роутов | Строк | Нужен `templates` | Нужен `bot` |
|---|---:|---:|---:|---:|
| `web/admin_chats.py` | 7 | 1082 | 1 | 3 |
| `web/admin_presets.py` | 8 | 601 | 1 | 0 |
| `web/admin_users.py` | 8 | 562 | 1 | 2 |
| `web/me.py` (+3) | 5 | 449 | 3 | 1 |
| `web/admin_settings.py` | 6 | 414 | 1 | 0 |
| `web/api.py` (+2) | 4 | 302 | 0 | 1 |
| `web/admin_keywords.py` | 4 | 197 | 1 | 0 |
| `web/admin_cleanup.py` | 2 | 163 | 0 | 0 |
| `web/admin_bans.py` | 1 | 134 | 1 | 0 |
| `web/auth.py` (+2) | 2 | 94 | 2 | 0 |
| **Итого** | **47** | **3998** | **11** | **7** |

36 роутов из 47 не трогают ни `templates`, ни `bot` — для них перенос
сводится к смене отступа.

### 3.2. Поимённо

Формат: метод, путь, имя функции, строка в `web_app.py` на `4e3d5b2`, размер,
зависимости.

#### `web/auth.py` (+2 к существующему `/logout`)

| Метод | Путь | Функция | Стр. | Размер | Зависимости |
|---|---|---|---:|---:|---|
| GET | `/login` | `login_page` | 909 | 5 | `templates` |
| POST | `/login` | `login_submit` | 914 | 89 | `templates`, `_login_attempts` |

`_login_attempts` — module-level словарь rate-limit, остаётся в `web_app.py`;
пять тестовых файлов импортируют его как `from web_app import _login_attempts`.

#### `web/admin_bans.py` (новый)

| Метод | Путь | Функция | Стр. | Размер | Зависимости |
|---|---|---|---:|---:|---|
| GET | `/admin/bans` | `admin_bans_page` | 4581 | 134 | `templates` |

#### `web/admin_cleanup.py` (новый)

| Метод | Путь | Функция | Стр. | Размер | Зависимости |
|---|---|---|---:|---:|---|
| GET | `/admin/cleanup` | `admin_cleanup_page_legacy` | 3817 | 14 | — |
| POST | `/admin/cleanup` | `admin_cleanup_apply` | 3831 | 149 | `_cleanup_counts`, `_wal_checkpoint` |

Сюда же переезжает вложенный хелпер `_cleanup_counts` (`web_app.py:3148`,
20 строк) — он становится модульной функцией `web/admin_cleanup.py`.
Его импортируют ещё два роута из других модулей (см. §5.3).

#### `web/admin_keywords.py` (новый)

| Метод | Путь | Функция | Стр. | Размер | Зависимости |
|---|---|---|---:|---:|---|
| GET | `/admin/keywords` | `admin_keywords_page` | 2971 | 39 | `templates` |
| POST | `/admin/keywords/add` | `admin_keywords_add` | 3010 | 66 | — |
| POST | `/admin/keywords/{keyword_id:int}/delete` | `admin_keywords_delete` | 3076 | 31 | — |
| POST | `/admin/keywords/{keyword_id:int}/toggle-ban-night` | `admin_keywords_toggle_ban_night` | 3107 | 61 | `_cleanup_counts` |

#### `web/api.py` (+2 к существующим)

| Метод | Путь | Функция | Стр. | Размер | Зависимости |
|---|---|---|---:|---:|---|
| GET | `/api/dashboard` | `api_dashboard` | 1217 | 79 | — |
| GET | `/api/search` | `api_search` | 1296 | 31 | — |
| POST | `/api/unban` | `api_unban` | 4715 | 131 | `bot` |
| POST | `/api/reset-automute-count` | `api_reset_automute_count` | 4846 | 61 | — |

`api_unban` — тот самый роут, где `bot is None` даёт 503, а `_SU_WEB_MOD_ID`
обрабатывает встроенного `su` (инвариант из `CLAUDE.md`, чинился в v4.8.11).
Логика переносится дословно.

#### `web/me.py` (+3 к существующим)

| Метод | Путь | Функция | Стр. | Размер | Зависимости |
|---|---|---|---:|---:|---|
| GET | `/dashboard` | `dashboard` | 1003 | 147 | `templates` |
| GET | `/user/{user_id:int}` | `user_page` | 1150 | 67 | `templates` |
| POST | `/me/password` | `me_change_password` | 3168 | 70 | — |
| GET | `/me` | `me_profile` | 3238 | 39 | `templates` |
| POST | `/me/avatar/refresh` | `me_avatar_refresh` | 3277 | 126 | `bot`, `_bot_info`, `_fetch_and_save_avatar` |

`/dashboard` и `/user/{id}` — семантически «страницы пользователя», поэтому
идут в `me.py`, как и планировалось в `web/__init__.py`.

#### `web/admin_settings.py` (новый)

| Метод | Путь | Функция | Стр. | Размер | Зависимости |
|---|---|---|---:|---:|---|
| GET | `/admin/settings` | `admin_settings_page` | 3403 | 87 | `templates`, `_bot_info`, `_cleanup_counts` |
| POST | `/admin/settings/backup` | `admin_settings_backup` | 3490 | 36 | `_wal_checkpoint` |
| POST | `/admin/settings/vacuum` | `admin_settings_vacuum` | 3526 | 63 | `_load_github_settings_row` |
| GET | `/admin/settings/github` | `admin_settings_github_get` | 3589 | 27 | `_load_github_settings_row` |
| POST | `/admin/settings/github` | `admin_settings_github_post` | 3616 | 99 | `_load_github_settings_row` |
| POST | `/admin/settings/github/test` | `admin_settings_github_test` | 3715 | 102 | `_load_github_settings_row` |

Сюда переезжают два вложенных хелпера: `_bot_info` (`web_app.py:3325`, 78
строк) и `_load_github_settings_row` (`web_app.py:3575`, 14 строк).

#### `web/admin_users.py` (новый)

| Метод | Путь | Функция | Стр. | Размер | Зависимости |
|---|---|---|---:|---:|---|
| GET | `/admin/users` | `admin_users_page` | 1327 | 81 | `templates` |
| POST | `/admin/users/create` | `admin_users_create` | 1408 | 194 | `bot`, `_fetch_and_save_avatar` |
| POST | `/admin/users/{user_id:int}/toggle` | `admin_users_toggle` | 1602 | 17 | — |
| POST | `/admin/users/{user_id:int}/reset` | `admin_users_reset` | 1619 | 19 | — |
| POST | `/admin/users/{user_id:int}/role` | `admin_users_change_role` | 1638 | 52 | — |
| POST | `/admin/users/{user_id:int}/edit-chats` | `admin_users_edit_chats` | 1690 | 64 | — |
| POST | `/admin/users/{user_id:int}/bind-tg` | `admin_users_bind_tg` | 1754 | 96 | `bot`, `_fetch_and_save_avatar` |
| POST | `/admin/users/{user_id:int}/delete` | `admin_users_delete` | 1850 | 39 | — |

#### `web/admin_presets.py` (новый)

| Метод | Путь | Функция | Стр. | Размер | Зависимости |
|---|---|---|---:|---:|---|
| GET | `/admin/presets` | `admin_presets_page` | 3980 | 63 | `templates` |
| POST | `/admin/presets/create` | `admin_presets_create` | 4043 | 121 | `_bot_module` (lazy, стр. 4140) |
| POST | `/admin/presets/{preset_id:int}/edit` | `admin_presets_edit` | 4164 | 129 | `_bot_module` (lazy, стр. 4283) |
| POST | `/admin/presets/{preset_id:int}/delete` | `admin_presets_delete` | 4293 | 50 | `_bot_module` (lazy, стр. 4326) |
| POST | `/admin/presets/words/add` | `admin_presets_words_add` | 4343 | 90 | — |
| POST | `/admin/presets/words/{word_id:int}/delete` | `admin_presets_words_delete` | 4433 | 40 | — |
| POST | `/admin/presets/links/add` | `admin_presets_links_add` | 4473 | 66 | — |
| POST | `/admin/presets/links/{link_id:int}/delete` | `admin_presets_links_delete` | 4539 | 42 | — |

Три места (`web_app.py:4140, 4283, 4326` — `create`, `edit`, `delete`) делают
`import bot as _bot_module`
внутри функции и зовут `_bot_module._invalidate_day_default_cache()`. Это
намеренный late import против циклической зависимости (`bot.py` импортирует
`web_app`). Переносится дословно — см. §5.5.

#### `web/admin_chats.py` (новый, самый крупный)

| Метод | Путь | Функция | Стр. | Размер | Зависимости |
|---|---|---|---:|---:|---|
| GET | `/admin/chats` | `admin_chats_page` | 1889 | 74 | `templates` |
| POST | `/admin/chats/{chat_id_str}/update` | `admin_chats_update` | 1963 | 328 | `app_state` (lazy) |
| POST | `/admin/chats/{chat_id_str}/toggle` | `admin_chats_toggle` | 2291 | 184 | `bot` |
| POST | `/admin/chats/{chat_id_str}/delete` | `admin_chats_delete` | 2475 | 119 | `bot` |
| POST | `/admin/chats/{chat_id_str}/sync-admins` | `admin_chats_sync_admins` | 2594 | 250 | `bot` |
| POST | `/admin/chats/{chat_id_str}/sanitary/add` | `admin_chats_sanitary_add` | 2844 | 67 | — |
| POST | `/admin/chats/{chat_id_str}/sanitary/{idx_str}/delete` | `admin_chats_sanitary_delete` | 2911 | 60 | — |

`admin_chats_update` (328 строк) — самый большой роут проекта. Внутри него
`web_app.py:2355, 2377` делают `from app_state import get_exit_night_mode` /
`get_exit_sanitary_day` — service locator, введённый в v4.8.9. Переносится
как есть.

---

## 4. Целевая архитектура

### 4.1. Связывание: `app.state` + `Depends`

`create_app()` кладёт зависимости в состояние приложения:

```python
app.state.templates = templates
app.state.bot = bot
```

`web/deps.py` получает два провайдера:

```python
def get_templates(request: Request) -> Jinja2Templates:
    """Jinja2Templates с CSRF-обёрткой, собранный в create_app()."""
    return request.app.state.templates


def get_bot(request: Request):
    """Экземпляр aiogram.Bot или None (тесты зовут create_app() без бота)."""
    return getattr(request.app.state, "bot", None)
```

Роуты объявляют их как зависимости:

```python
@router.post("/admin/chats/{chat_id_str}/delete")
async def admin_chats_delete(
    chat_id_str: str,
    bot=Depends(get_bot),
    _auth: AuthUser = Depends(require_su),
):
    ...
    await bot.leave_chat(chat_id=chat_id)
```

**Почему `app.state`, а не модульные синглтоны.** Тесты зовут `create_app()`
многократно в одном процессе (только `test_v460_granular_perms.py` — 15 раз).
Модульный синглтон означал бы, что второй вызов перетирает состояние первого,
а CSRF-обёртка над `TemplateResponse` навешивалась бы повторно на один и тот
же объект. `app.state` даёт каждому экземпляру своё состояние.

**Почему не фабрики `build_router(templates, bot)`.** Диff был бы меньше, но
замыкание осталось бы паттерном: файлы разделены, связанность нет. План
формулирует цель прямо — «`bot` передаётся через `app.state.bot` вместо
замыкания».

### 4.2. `bot is None` продолжает работать

Пять роутов проверяют `if bot is None` и возвращают 503 либо деградируют.
`get_bot` возвращает `None`, когда `create_app()` вызван без бота, — поведение
сохраняется дословно, отдельной обработки не требуется.

### 4.3. Итоговый `create_app()`

Остаётся ~137 строк:

1. проверка `SESSION_SECRET` (+ обход через `WEB_ALLOW_NO_SECRET`);
2. `app = FastAPI(...)`;
3. middleware `log_requests`;
4. сборка `templates` + CSRF-обёртка + `csrf_field` в globals;
5. `os.makedirs(AVATARS_DIR)`;
6. `app.state.templates` / `app.state.bot`;
7. десять `app.include_router(...)`;
8. `return app`.

---

## 5. Правила переноса

### 5.1. Позднее связывание хелперов — обязательно

Модули `web/` обращаются к хелперам `web_app` через модуль, а не через имя:

```python
# ПРАВИЛЬНО
import web_app
...
ok = await web_app._fetch_and_save_avatar(bot, tg_id)

# НЕПРАВИЛЬНО
from web_app import _fetch_and_save_avatar
```

Причина конкретная: `tests/test_v45_dashboard.py:547,563` патчат
`web_app._fetch_and_save_avatar` и дёргают `POST /me/avatar/refresh`. При
`from ... import` имя привязывается в момент импорта, патч перестаёт
действовать, тесты краснеют.

Проект уже наступал на эти грабли с `mod_commands.py` — в `CLAUDE.md` записано:
«модуль импортирует хелперы из `bot_handlers` по именам, поэтому в тестах
патчить надо `mod_commands.X`». Здесь выбирается противоположная стратегия,
чтобы существующие патчи продолжали работать без правки тестов.

### 5.2. Module-level хелперы остаются в `web_app.py`

`_avatar_path` (682), `_fetch_and_save_avatar` (711), `_wal_checkpoint` (194),
`_csrf_token_from_request` (404), `_login_attempts` — не переезжают. Их
импортируют тесты и уже вынесенные роутеры.

### 5.3. Вложенные хелперы переезжают вместе с профильными роутами

| Хелпер | Строка | Куда | Кто ещё зовёт |
|---|---:|---|---|
| `_bot_info` | 3325 | `web/admin_settings.py` | `web/me.py` (`/me/avatar/refresh`) |
| `_cleanup_counts` | 3148 | `web/admin_cleanup.py` | `admin_settings.py`, `admin_keywords.py` |
| `_load_github_settings_row` | 3575 | `web/admin_settings.py` | только там |

Перекрёстные вызовы — обычным импортом между модулями `web/`
(`from web.admin_cleanup import _cleanup_counts`). Циклов не возникает:
`admin_cleanup` не зависит ни от `admin_settings`, ни от `admin_keywords`.

### 5.4. CSRF-обёртка не трогается

`_template_response_with_csrf` и `_csrf_field` остаются внутри `create_app()`
и навешиваются на `templates` до записи в `app.state`. Роутеры получают уже
обёрнутый объект и о CSRF не знают — как сейчас.

Монкипатч метода `templates.TemplateResponse` выглядит грязно, но переделка —
отдельный риск, не относящийся к декомпозиции. Явно вне объёма.

### 5.5. Late imports переносятся дословно

`import bot as _bot_module` (3 места в `admin_presets`) и
`from app_state import get_*` (2 места в `admin_chats`) остаются внутри
функций. Первый — защита от цикла `bot.py` → `web_app.py`; второй — уже
принятый в проекте service locator.

`CLAUDE.md` требует не возвращать `from bot import ...` в пользу `app_state`.
Три места с `_invalidate_day_default_cache` формально под это правило
подпадают, но их перевод — смысловая правка, а не перенос. Выносится в
отдельную задачу, здесь фиксируется как известный долг.

### 5.6. Порядок регистрации роутеров сохраняется

Коллизий путей сейчас нет: конкретные пути отличаются либо префиксом, либо
конвертером (`{user_id:int}`). Порядок `include_router` повторяет текущий
порядок объявления. Менять его заодно с переносом — смешивать два риска.

### 5.7. Ruff: `per-file-ignores` едут за кодом

`pyproject.toml:123` держит игноры для `web_app.py`, включая `ASYNC230` —
блокирующий `open()`. Один из двух таких вызовов (`web_app.py:3358`,
`/proc/self/status` внутри `_bot_info`) переезжает в
`web/admin_settings.py`, поэтому туда же добавляется точечный игнор с той же
пометкой «снимается в Task 6». Второй (`web_app.py:758`) остаётся на месте.

Игноры complexity (`C901`, `PLR09xx`) для новых модулей добавляются только
если ruff действительно ругается — вслепую не копируются.

---

## 6. Порядок работ

Один домен — один коммит. После каждого: полная сюита (65 файлов, ~6 минут)
и `ruff check .`. Порядок — от дешёвого к дорогому, чтобы шаблон переноса
устоялся на простых модулях:

| # | Модуль | Роутов | Строк | Чем интересен |
|---:|---|---:|---:|---|
| 1 | `auth` | 2 | 94 | обкатка `get_templates` |
| 2 | `admin_bans` | 1 | 134 | один роут, только `templates` |
| 3 | `admin_cleanup` | 2 | 163 | первый переезд вложенного хелпера |
| 4 | `admin_keywords` | 4 | 197 | первый межмодульный импорт хелпера |
| 5 | `api` | 4 | 302 | обкатка `get_bot` (`/api/unban`) |
| 6 | `me` | 5 | 449 | патчи тестов на `_fetch_and_save_avatar` |
| 7 | `admin_settings` | 6 | 414 | `_bot_info`, ruff-игнор `ASYNC230` |
| 8 | `admin_users` | 8 | 562 | два роута с `bot` |
| 9 | `admin_presets` | 8 | 601 | late import `_bot_module` |
| 10 | `admin_chats` | 7 | 1082 | самый крупный, 3 роута с `bot` |

Порядок не обязателен к соблюдению как догма, но шаги 1–5 должны идти до
6–10: они дешёвые и проверяют механику связывания до того, как она
применяется к крупным модулям.

---

## 7. Проверка

**Основная страховка — существующая сюита.** Рефакторинг обязан быть строго
поведенчески-нейтральным: ни один тест не должен потребовать правки. Если
тест краснеет — правится код, а не тест. Замер покрытия по доменам (число
тестовых файлов, упоминающих путь):

| Домен | Файлов |
|---|---:|
| `/admin/chats` | 17 |
| `/admin/users/` | 7 |
| `/sanitary/add` | 5 |
| `/me/password` | 4 |
| `/admin/presets/words/`, `/links/` | 3 |
| `/admin/settings/github` | 3 |
| `/api/reset-automute-count` | 3 |
| `/admin/cleanup` | 3 |
| `/admin/keywords/add` | 2 |

**Дополнительно** план требует на каждый вынесенный роутер тест «страница
отвечает 200 и содержит ключевой элемент». Такой тест пишется **до** переноса
для тех страниц, где его нет, — иначе он проверяет уже переехавший код и не
доказывает, что перенос ничего не сломал.

**Дымовая проверка после каждого шага:** число зарегистрированных роутов у
собранного приложения не изменилось — ловит потерю роута при копировании.
Эталон на `4e3d5b2`: **54** уникальных пары `(path, method)`, из них 47 внутри
`create_app()` и 7 в `web/`.

Считать нужно с обходом включённых роутеров. В Starlette 1.6 / FastAPI 0.141
`app.include_router(...)` **не разворачивает** роуты в плоский `app.routes` —
там появляется объект `fastapi.routing._IncludedRouter`, а сами роуты лежат в
его `original_router.routes`. Наивный `[r for r in app.routes если Route]`
вернёт только 47 и будет уменьшаться с каждым вынесенным доменом, создавая
ложную тревогу о потере роутов:

```python
from starlette.routing import Route

def walk(routes):
    for r in routes:
        if isinstance(r, Route):
            yield r
        elif hasattr(r, "original_router"):      # fastapi _IncludedRouter
            yield from walk(r.original_router.routes)

rs = list(walk(app.routes))
pairs = {(r.path, m) for r in rs for m in (r.methods or ()) if m != "HEAD"}
assert len(pairs) == 54
```

Проверено на текущем стеке: до выноса `walk` даёт 54, наивный обход — 47.

---

## 8. Риски

| Риск | Проявление | Смягчение |
|---|---|---|
| Потерянный роут | 404 на проде, тесты могут не заметить | проверка числа роутов после каждого шага |
| Сломанный патч в тестах | краснеет `test_v45_dashboard` | правило §5.1 — импорт через модуль |
| Двойная CSRF-обёртка | `TemplateResponse` вызывается дважды | обёртка остаётся в `create_app`, §5.4 |
| Циклический импорт | падение на старте | late imports сохраняются, §5.5 |
| Ruff краснеет на новых файлах | падает CI | §5.7, игноры едут за кодом |
| Расхождение с прод-поведением | незаметно до деплоя | тела роутов копируются дословно, без «улучшений» |

**Главное правило:** тела роутов переносятся дословно. Любая замеченная по
дороге проблема (дублирование, мёртвый код, стиль) записывается отдельно и не
чинится в этом же коммите. Смешивание переноса с правкой лишает сюиту
диагностической силы: непонятно, что именно сломалось.

---

## 9. Вне объёма

- Переделка CSRF-обёртки над `TemplateResponse` (§5.4).
- Перевод `import bot as _bot_module` на `app_state` (§5.5).
- Инверсия `web/deps.py` (сейчас фасад над `web_app.py`; перенос определений
  сюда — отдельная задача).
- Снятие `ASYNC230` — это Task 6 плана.
- Изменение сигнатуры `create_app(lifespan, bot)` — её зовёт `bot.py`.
- Task 16 (`/health` → `/healthz`) — независимая задача, хотя и трогает
  `web/health.py`.
