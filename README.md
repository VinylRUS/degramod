<div align="center">

# 🛡️ DegraBan

**Скрытый модераторский бот для Telegram — невидимый для нарушителей, мощный для модераторов.**

[![Version](https://img.shields.io/badge/version-v4.5.6-blue.svg?style=flat-square)](CHANGELOG)
[![Python](https://img.shields.io/badge/python-3.11+-3776AB.svg?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![aiogram](https://img.shields.io/badge/aiogram-3.30-26A5E4.svg?style=flat-square&logo=telegram&logoColor=white)](https://aiogram.dev)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![SQLite](https://img.shields.io/badge/SQLite-async-003B57.svg?style=flat-square&logo=sqlite&logoColor=white)](https://sqlite.org)
[![License](https://img.shields.io/badge/license-MIT-green.svg?style=flat-square)](LICENSE)

</div>

---

## ✨ Что такое DegraBan

**DegraBan** — это модераторский бот для Telegram-групп, спроектированный в парадигме **стелс-модерации**: обычные участники чата не догадываются о его существовании. Бот не отвечает на `/start`, `/help` или любые публичные команды — он молча игнорирует всё, что не исходит от уполномоченных модераторов.

Когда модератор отвечает реплаем на сообщение нарушителя командой `!mute` / `!warn` / `!ban`, бот:

1. **Применяет санкцию** (мьют / варн / бан) — мгновенно и тихо.
2. **Удаляет сообщение нарушителя** — публично nobody sees nothing.
3. **Отправляет эфемерное подтверждение модератору** — видно только ему через `receiver_user_id`. Исчезает через 30 секунд.
4. **При варне — отправляет эфемерное уведомление нарушителю** (видно только ему), тоже авто-удаляется.
5. **Формирует Rich-отчёт в приватный лог-канал** — со скриншотом, причиной, профилем нарушителя и кнопкой в веб-панель.

Всё, что происходит между модератором и ботом, невидимо для остальных участников — никаких публичных «✅ Пользователь замьючен на 1 час».

---

## 🚀 Ключевые возможности

### 🥷 Стелс-модерация
- Бот **не реагирует** на команды обычных юзеров (ни `/start`, ни любые другие).
- Подтверждения модератору — **эфемерные** (`receiver_user_id`), видно только самому модератору.
- Уведомления нарушителю при варне — тоже эфемерные, видит только он.
- Все эфемерные сообщения **авто-удаляются через 30 секунд** — никаких дублей при перезапуске клиента.

### ⚖️ Модераторские санкции (reply-команды в группе)

| Команда | Описание |
|---|---|
| `!mute <1d/2h/30m> <причина>` | Полный мьют (все виды отправки) на указанную длительность |
| `!warn <причина>` | Выдать варн + удалить сообщение нарушителя |
| `!ban <причина>` | Забанить. Если reply на стикер — стикерпак автодобавляется в бан-лист |
| `!unmute` | Размьютить (восстанавливает текущие права чата) |
| `!unban` | Разбанить (`only_if_banned` — безопасный) |
| `!unwarn [N]` | Снять N последних варнов (по умолчанию 1) |
| `!warns` | Показать текущее кол-во варнов юзера (в личку админу) |
| `!resetwarns` | Обнулить варны юзера (только SU / admin) |

### 🤖 Автоматическая защита

- **CAS-интеграция** — проверка новых участников по базе [Combot Anti-Spam](https://cas.chat). Подозрительные — банятся автоматически.
- **Word Filter** — фильтр слов/паттернов (подстрока или regex, case-insensitive). Действие: `delete` / `warn` / `mute` / `ban`. Per-chat + global паттерны.
- **Link Filter** — блокировка ссылок на внешние домены с allowlist'ом (per-chat + global).
- **Sticker Filter** — бан-лист стикерпаков с настраиваемой санкцией (`delete` / `warn` / `mute` / `ban`).
- **Auto-Warn Thresholds** — при достижении N варнов → автo-мьют; при M варнов → автo-бан.
- **Warn Decay** — варны автоматически «сгорают» через N дней (настраиваемо, 0 = отключено).
- **Friendly-Fire Protection** — модераторы не могут быть замьючены/забанены/получить варн от других модераторов.

### 🌙 Ночной режим

- Автоматическое ограничение чата на ночное окно (например, 23:00 → 07:00).
- **Per-chat timezone** — каждый чат живёт в своём часовом поясе.
- **Weekend schedule** — отдельное расписание на выходные.
- **Custom permissions** — три пресета (`strict` / `text_only` / `none`) или свой JSON.
- **Enter/exit notifications** — опциональные сообщения при входе/выходе из ночного режима.
- Snapshot/restore pattern — корректно восстанавливает права чата при выходе.

### 🧹 Санитарные дни

- Полный локдаун чата на заданные даты (например, 1 января).
- Один API-вызов `ChatPermissions → all False` на чат — эффективно даже для 19k+ участников.
- **Модераторов не трогает** — их Telegram admin rights override'ют chat-level permissions.
- Имеет приоритет над ночным режимом — night mode пропускает чаты с активным санитарным днём.
- Поддержка форматов: `YYYY-MM-DD`, `YYYY-MM-DD:YYYY-MM-DD` (диапазон), `#` комментарии.

### 🔐 Bot Rights Check

- При добавлении в чат бот **автоматически проверяет свои права** (can_delete_messages, can_restrict_members, can_ban_users и т.д.).
- Если прав недостаточно — DM супер-админу со списком отсутствующих прав.
- В веб-панели — `⚠ RIGHTS` badge в списке чатов и кнопка Recheck.

### 📊 Веб-панель администратора

Современный веб-интерфейс на FastAPI + Jinja2:

- **Dashboard** — обзорная статистика по чатам, модераторам, санкциям.
- **Чаты** — список всех чатов с ботом, их настройки, badges (RIGHTS / NIGHT / SAN).
- **Пользователи** — поиск, профиль нарушителя с историей санкций.
- **Настройки чата** — все параметры в одном UI: word filter, link filter, sticker filter, night mode, sanitary days, CAS, warn thresholds.
- **Профиль модератора** — история его действий.
- **Changelog modal** — кликабельная плашка версии в футере, открывает историю изменений.

### 📝 Rich-отчёты в лог-канал

Каждая санкция формирует структурированный отчёт:

- **Section heading** + делители для визуальной чистоты.
- **Список**: нарушитель / причина / ссылка на веб-профиль.
- **Медиа под спойлером** (`Details`-блок) — для шок-контента.
- **Доп. инфо** в свёрнутом блоке.
- **Footer** с кликабельным именем модератора (без «Модератор:» подписи).
- Длинный URL веб-профиля спрятан под коротким текстом «Открыть профиль →».
- ID нарушителя — моноширинным кодом (легко копируется долгим тапом).

### 👥 Трёхуровневая ролевая модель

| Роль | Где управляется | Права |
|---|---|---|
| **Super Admin (SU)** | `ADMIN_IDS` env | Глобальный — все чаты, все команды |
| **Admin** | `/addadmin` | Назначает moderators, меняет настройки чата |
| **Moderator** | Назначается admin'ом | Применяет санкции (`!mute` / `!warn` / `!ban` и т.д.) |

Все роли привязаны к конкретным `chat_id` — модератор в одном чате не имеет прав в другом.

---

## 🛠️ Технологический стек

| Компонент | Технология |
|---|---|
| Telegram Bot API | [aiogram 3.30](https://aiogram.dev) |
| Web-фреймворк | [FastAPI 0.115](https://fastapi.tiangolo.com) |
| ASGI-сервер | [uvicorn](https://uvicorn.org) |
| База данных | SQLite (через SQLAlchemy 2.0 + aiosqlite) |
| Шаблонизатор | Jinja2 3.1 |
| HTTP-клиент | aiohttp 3.13 |
| Python | 3.11+ |

**Режим работы** определяется автоматически:
- Если `WEBHOOK_URL` задан и удалось установить webhook → webhook mode.
- Иначе → long polling (надёжный фоллбэк).

FastAPI запускается всегда — для веб-панели.

---

## 📦 Быстрый старт

### Вариант 1: Docker (рекомендуется)

```bash
# 1. Клонируем репозиторий
git clone https://github.com/yourusername/degraban.git
cd degraban

# 2. Копируем и заполняем .env
cp .env.example .env
nano .env   # BOT_TOKEN, ADMIN_IDS, WEB_PASSWORD — обязательно

# 3. Запускаем
docker build -t degraban .
docker run -d \
  --name degraban \
  -p 3000:3000 \
  -v $(pwd)/data:/app/data \
  --env-file .env \
  degraban
```

Бот поднимется на порту `3000`. Веб-панель — на `http://localhost:3000/`.

### Вариант 2: VPS вручную

```bash
# 1. Зависимости системы
sudo apt update && sudo apt install -y python3.11 python3.11-venv git

# 2. Клонирование
git clone https://github.com/yourusername/degraban.git
cd degraban

# 3. Виртуальное окружение
python3.11 -m venv venv
source venv/bin/activate

# 4. Зависимости Python
pip install -r requirements.txt

# 5. Конфигурация
cp .env.example .env
nano .env   # заполнить BOT_TOKEN, ADMIN_IDS, WEB_PASSWORD

# 6. Запуск
python bot.py
```

### Вариант 3: Bothost

Если используете [Bothost](https://bothost.tech):

1. Залейте файлы в панель управления.
2. В разделе **Переменные окружения** задайте `BOT_TOKEN`, `ADMIN_IDS`, `WEB_PASSWORD`.
3. Bothost автоматически задаёт `PORT`, `WEBHOOK_URL`, `BOT_ID`, `DOMAIN`.
4. Нажмите **Запустить**.

---

## ⚙️ Конфигурация

Все настройки — через переменные окружения (файл `.env`):

### Обязательные

| Переменная | Описание |
|---|---|
| `BOT_TOKEN` | Токен бота от [@BotFather](https://t.me/BotFather) |
| `ADMIN_IDS` | ID супер-админов через запятую (`123456,789012`) |
| `WEB_PASSWORD` | Пароль для входа в веб-панель |

### Опциональные

| Переменная | Default | Описание |
|---|---|---|
| `PORT` | `3000` | Порт для FastAPI |
| `WEBHOOK_URL` | _(пусто)_ | URL для webhook. Если пусто — long polling |
| `WEBHOOK_SECRET` | _(auto)_ | Секрет для webhook-заголовка. Лучше задать фиксированный |
| `DB_PATH` | `/app/data/shadow_logs.db` | Путь к SQLite |
| `SESSION_SECRET` | _(auto)_ | Секрет для подписи кук |
| `WEB_PUBLIC_URL` | `https://degraban.bothost.tech` | Публичный URL веб-панели для ссылок в отчётах |

### Динамические настройки (через команды бота)

Эти настройки **не** задаются через env — управляются через личку бота:

```bash
/setreport default <chat_id>          # глобальный чат для отчётов
/setreport <chat_id> <report_chat_id> # per-chat override
/setreport ... 0                      # сбросить

/sethashtag <chat_id> #хэштег         # хэштег чата для отчётов
```

---

## 📚 Команды

### В группе (reply на сообщение нарушителя)

> Бот использует префикс `!` для модераторских команд, чтобы не пересекаться со стандартными `/`-командами Telegram.

```
!mute <1d/2h/30m> <причина>   — замьютить
!warn <причина>               — выдать варн (+ удалить сообщение)
!ban <причина>                — забанить (если reply на стикер — пак автодобавляется)
!unmute                       — размьютить
!unban                        — разбанить
!unwarn [N]                   — снять N варнов (по умолчанию 1)
!warns                        — показать кол-во варнов (в личку админу)
!resetwarns                   — обнулить варны (только SU / admin)
```

### В личке бота (только ADMIN_IDS)

```bash
# Управление ролями
/addadmin <chat_id> <user_id>          # добавить admin'а
/deladmin <chat_id> <user_id>          # убрать admin'а

# Настройки чата
/settings <chat_id>                    # показать текущие настройки
/mute_duration <chat_id> <1d/2h/30m>   # длительность мьюта по умолчанию
/warns_mute <chat_id> <N>              # варнов до авто-мьюта (0 = выкл)
/warns_ban <chat_id> <N>               # варнов до авто-бана (0 = выкл)
/warndecay <chat_id> <days>            # срок действия варна (0 = выкл)

# Word Filter
/addword <chat_id> <pattern> [action] [is_regex]   # action: delete|warn|mute|ban
/delword <chat_id> <pattern>
/listwords [chat_id]

# Link Filter
/linkfilter <chat_id> on|off
/linkallow <chat_id|global> <domain>
/linkallowlist [chat_id]

# Sticker Filter
/bansticker <pack_name_or_link> [punishment] [duration]
/liststickers [chat_id]
/delsticker <pack_name> [chat_id]

# CAS
/cas <chat_id> on|off

# Ночной режим
/nightmode <chat_id> <start> <end> [permissions]   # HH:MM, permissions: strict|text_only|none
/nightmode <chat_id> off
/nightmode <chat_id> tz <Europe/Moscow>
/nightmode <chat_id> weekend <start> <end>
/nightmode <chat_id> notify on|off
/nightmode <chat_id> enter_msg "текст при входе"
/nightmode <chat_id> exit_msg "текст при выходе"

# Санитарные дни
/sanitary <chat_id> list
/sanitary <chat_id> add <YYYY-MM-DD>
/sanitary <chat_id> add <YYYY-MM-DD>:<YYYY-MM-DD>
/sanitary <chat_id> remove <YYYY-MM-DD>
/sanitary <chat_id> clear
/sanitary <chat_id> toggle    # ручное переключение для теста

# Помощь
/help
```

---

## 📁 Структура проекта

```
degraban/
├── bot.py                  # Точка входа: FastAPI + Aiogram, ночной тик, санитарный тик
├── bot_handlers.py         # Все хендлеры команд и сообщений (~4500 строк)
├── db.py                   # SQLAlchemy-модели, миграции, helpers
├── web_app.py              # FastAPI-приложение веб-панели
├── templates/              # Jinja2-шаблоны веб-панели
│   ├── base.html           # Базовый шаблон + changelog modal
│   ├── login.html
│   ├── dashboard.html
│   ├── admin.html
│   ├── admin_chats.html
│   ├── admin_settings.html
│   ├── profile.html
│   └── user.html
├── avatars/                # Кеш аватарок юзеров для отчётов
├── scripts/                # Тест-сьюты (218+ тестов)
│   ├── test_v456_ephemeral_autodelete.py
│   ├── test_v454_sanitary_day.py
│   ├── test_v453_night_mode.py
│   ├── test_v452_features.py
│   ├── test_v451_audit_fixes.py
│   └── ...
├── requirements.txt
├── Dockerfile
├── .env.example
└── TODO.md
```

---

## 🧪 Разработка и тестирование

```bash
# Установка зависимостей разработки
pip install -r requirements.txt

# Запуск тест-сьютов
python scripts/test_v456_ephemeral_autodelete.py
python scripts/test_v454_sanitary_day.py
python scripts/test_v453_night_mode.py
python scripts/test_v452_features.py
python scripts/test_v451_audit_fixes.py

# Все 218+ тестов должны пройти
```

### Логирование

Бот пишет структурированные логи в stdout:

```
2026-07-29 14:18:23 │ shadow_logger            │ INFO    │ === ENV DUMP ===
2026-07-29 14:18:23 │ shadow_logger            │ INFO    │   BOT_TOKEN = 1234567:...
2026-07-29 14:18:23 │ shadow_logger            │ INFO    │   ADMIN_IDS = 123456,789012
```

Для продакшена рекомендуется `LOG_LEVEL=INFO`. Для дебага — `LOG_LEVEL=DEBUG`.

---

## 🔄 Changelog

### v4.5.6 — 29 июля 2026
- **Ephemeral auto-delete**: подтверждения модератору (`✅ Замьютил` / `Варн выдан` / etc.) и уведомления нарушителю при варне теперь авто-удаляются через 30 секунд.
- **Why**: Telegram-клиенты не испаряют `receiver_user_id`-сообщения сами — они переотображаются при перезапуске. Бот теперь удаляет их сам.
- Параметр `delete_after=30.0` в `_send_ephemeral` и `_send_user_warn_notification`. `delete_after=0` отключает авто-удаление.

### v4.5.5
- Bot rights check при добавлении в чат + DM Admin/SU если прав недостаточно.
- `⚠ RIGHTS` badge в `/admin/chats`, кнопка Recheck.
- `stealth_catchall` fallback.

### v4.5.4
- **Sanitary days**: полный локдаун чата на заданные даты.
- Модераторов не трогает (Telegram admin rights override'ют chat-level ChatPermissions).
- Ночной режим пропускает чаты с активным санитарным днём.
- `/sanitary` CLI + textarea в `/admin/chats`, timezone-aware date boundaries.

### v4.5.3
- `/help` cleanup.
- **Night mode**: per-chat timezone, weekend schedule, custom permissions, enter/exit notifications, `/nightmode` subcommands.

### v4.5.2
- CAS integration, word/link/sticker filters, auto night mode, warn decay, version display.

### v4.5.1
- Audit fixes: consumed warns, webhook secret, login rate-limit, POST logout, report chat validation, multi-report-chat, WAL checkpoint, `!unwarn` cap, `!resetwarns` SU/admin-only, friendly-fire protection.

<details>
<summary><strong>Полный changelog (старые версии)</strong></summary>

> См. `templates/base.html` → клик по плашке версии в футере веб-панели → modal с полной историей.

</details>

---

## 🗺️ Roadmap

- [ ] **CAPTCHA-верификация** новых участников (как у Shieldy / Rose)
- [ ] **Anti-flood** — rate-limiting по сообщениями в единицу времени
- [ ] **Raid protection** — детекция массового входа + авто-локдаун
- [ ] **Slow mode** — интеграция с нативным Telegram slow mode
- [ ] **Multi-language** — локализация UI веб-панели

---

## 🤝 Участие в разработке

Pull requests приветствуются. Перед крупными изменениями откройте issue для обсуждения.

Все тесты должны проходить:

```bash
for f in scripts/test_v4*.py; do python "$f" || exit 1; done
```

---

## 📄 Лицензия

MIT License. См. [LICENSE](LICENSE).

---

<div align="center">

**DegraBan** — стелс-модерация для Telegram, которую не видно, но которая работает.

Made with 🛡️ and Python

</div>
