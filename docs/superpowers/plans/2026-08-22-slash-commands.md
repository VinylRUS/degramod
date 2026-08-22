# v5.1.0 — Slash-команды, права, вайтлист ботов: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Перевести групповые команды бота с префикса `!` на `/`, закрыть их от обычных участников с внятным ephemeral-отказом, добавить публичные `/mywarns` и `/rules`, вайтлист ботов для via-bot фильтра и переписать неграмотные тексты наказаний.

**Architecture:** Появляется модуль `commands.py` — реестр команд без зависимостей от aiogram и БД. Существующие regex-паттерны не переписываются: у них отваливается якорь `^!`, а матчат они строку, уже нормализованную функцией `strip_prefix` (срезает `/` или `!` и `@botusername`). Диспетчер `handle_group_command` вместо молчаливого `return` при отсутствии прав удаляет команду и отвечает ephemeral с кулдауном.

**Tech Stack:** Python 3.14.7, uv, aiogram 3.x, FastAPI, SQLAlchemy 2.x + aiosqlite, Alembic, Jinja2, unittest (через `tools/run_tests.py`).

**Spec:** `docs/superpowers/specs/2026-08-22-slash-commands-design.md`

## Global Constraints

- Язык кода, комментариев и докстрингов — **русский**.
- Комментарии-версии в формате `# v5.1.0: <что и почему>`. Не пересказывать changelog.
- Ошибки Telegram ловить как `TelegramAPIError` (базовый класс), **не** `TelegramBadRequest`.
- `asyncio.create_task` запрещён — только `_spawn_background_task` (`bot_handlers.py:214`).
- Блокирующий I/O в async запрещён линтером (`ASYNC230`, без исключений).
- Новые критичные вызовы Telegram оборачивать в `tg_safe_call(factory, label=...)`; `factory` — callable, возвращающий **новую** корутину.
- Модули `web/` зовут хелперы через модуль (`web_app._helper(...)`), не `from web_app import _helper`.
- Роутеры `web/` импортируются только внутри `create_app()`.
- Тесты запускать **только** через `uv run python tools/run_tests.py`, не `pytest tests` напрямую.
- Каждая новая колонка/таблица: (1) поле в модель, (2) идемпотентный блок в `init_db()`, (3) Alembic-ревизия. Блок миграции обязан идти **до** любого ORM-запроса к этой таблице.
- Ссылка на правила по умолчанию: `https://rules.degradach.ru/`
- Кулдаун отказа: 60 секунд, ключ `(user_id, chat_id)`.
- Кулдаун `/mywarns`: остаётся 300 секунд.
- После каждой задачи: `uv run ruff check .` и `uv run python tools/run_tests.py` — обе зелёные до коммита.

---

### Task 1: Модуль `commands.py` — реестр и нормализация

**Files:**
- Create: `commands.py`
- Test: `tests/test_v510_commands_registry.py`

**Interfaces:**
- Consumes: ничего (модуль без зависимостей от проекта).
- Produces:
  - `class Access(StrEnum)` со значениями `USER`/`MOD`/`ADMIN`
  - `@dataclass(frozen=True) class CommandSpec` с полями `name: str`, `pattern: re.Pattern`, `args_hint: str`, `description: str`, `access: Access`, `in_menu: bool`
  - `GROUP_COMMANDS: tuple[CommandSpec, ...]`
  - `DM_MENU_COMMANDS: tuple[tuple[str, str], ...]` — пары (имя, описание)
  - `PUNITIVE: frozenset[str]`
  - `strip_prefix(text: str | None, bot_username: str | None) -> str | None`
  - `resolve(text: str | None, bot_username: str | None) -> tuple[CommandSpec, re.Match] | None`
  - `set_bot_username(username: str | None) -> None`
  - `get_bot_username() -> str | None`
  - `spec_by_name(name: str) -> CommandSpec | None`

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_v510_commands_registry.py`:

```python
"""v5.1.0 — реестр команд и нормализация префикса.

Модуль commands.py не зависит ни от aiogram, ни от БД, поэтому тестируется
напрямую, без поднятия бота.

Запуск: uv run python tools/run_tests.py -k v510_commands
"""
from _paths import _P  # noqa: E402
import sys
import unittest

sys.path.insert(0, _P())

import commands  # noqa: E402


class TestStripPrefix(unittest.TestCase):
    def test_slash_prefix(self):
        self.assertEqual(commands.strip_prefix("/ban @vasya Скам", "degradach_bot"),
                         "ban @vasya Скам")

    def test_bang_prefix_still_works(self):
        self.assertEqual(commands.strip_prefix("!ban @vasya Скам", "degradach_bot"),
                         "ban @vasya Скам")

    def test_own_username_suffix_stripped(self):
        self.assertEqual(commands.strip_prefix("/ban@degradach_bot Скам", "degradach_bot"),
                         "ban Скам")

    def test_username_match_is_case_insensitive(self):
        self.assertEqual(commands.strip_prefix("/ban@DegraDach_Bot Скам", "degradach_bot"),
                         "ban Скам")

    def test_foreign_username_rejected(self):
        self.assertIsNone(commands.strip_prefix("/ban@other_bot Скам", "degradach_bot"))

    def test_unknown_own_username_rejects_explicit_mention(self):
        # Username ещё не известен (гонка на старте) — явно адресованную
        # команду не берём, чтобы не перехватить чужую.
        self.assertIsNone(commands.strip_prefix("/ban@degradach_bot Скам", None))

    def test_bare_command_works_without_known_username(self):
        self.assertEqual(commands.strip_prefix("/ban Скам", None), "ban Скам")

    def test_not_a_command(self):
        self.assertIsNone(commands.strip_prefix("просто текст", "degradach_bot"))
        self.assertIsNone(commands.strip_prefix("", "degradach_bot"))
        self.assertIsNone(commands.strip_prefix(None, "degradach_bot"))
        self.assertIsNone(commands.strip_prefix("/", "degradach_bot"))

    def test_leading_whitespace_tolerated(self):
        self.assertEqual(commands.strip_prefix("  /ban Скам", "degradach_bot"), "ban Скам")

    def test_multiline_command_keeps_tail(self):
        # Причина может быть многострочной — DOTALL обязателен.
        self.assertEqual(commands.strip_prefix("/ban Скам\nи флуд", "degradach_bot"),
                         "ban Скам\nи флуд")


class TestResolve(unittest.TestCase):
    def test_resolves_ban_with_named_groups(self):
        found = commands.resolve("/ban @vasya Скам", "degradach_bot")
        self.assertIsNotNone(found)
        spec, m = found
        self.assertEqual(spec.name, "ban")
        self.assertEqual(m.group("target"), "@vasya")
        self.assertEqual(m.group("reason"), "Скам")

    def test_resolves_mute_duration(self):
        spec, m = commands.resolve("/mute 2ч Флуд", "degradach_bot")
        self.assertEqual(spec.name, "mute")
        self.assertEqual(m.group("dur"), "2ч")
        self.assertEqual(m.group("reason"), "Флуд")

    def test_ban_without_reason_does_not_resolve(self):
        # v4.8.6: negative lookahead — «/ban @vasya» без причины не матчится.
        self.assertIsNone(commands.resolve("/ban @vasya", "degradach_bot"))

    def test_foreign_command_does_not_resolve(self):
        self.assertIsNone(commands.resolve("/roll 2d6", "degradach_bot"))

    def test_every_registry_entry_resolves_itself(self):
        # Защита от опечатки в паттерне: каждая команда обязана матчить
        # собственный минимальный пример.
        samples = {
            "ban": "/ban Скам",
            "sban": "/sban",
            "warn": "/warn Мат",
            "swarn": "/swarn",
            "mute": "/mute 2ч Флуд",
            "smute": "/smute 2ч",
            "unmute": "/unmute",
            "unban": "/unban",
            "unwarn": "/unwarn",
            "warns": "/warns",
            "resetwarns": "/resetwarns",
            "resetmc": "/resetmc",
            "alarm": "/alarm on",
            "mywarns": "/mywarns",
            "rules": "/rules",
        }
        for spec in commands.GROUP_COMMANDS:
            with self.subTest(command=spec.name):
                self.assertIn(spec.name, samples,
                              f"нет примера для команды {spec.name}")
                found = commands.resolve(samples[spec.name], "degradach_bot")
                self.assertIsNotNone(found, f"/{spec.name} не резолвится")
                self.assertEqual(found[0].name, spec.name)


class TestRegistryShape(unittest.TestCase):
    def test_names_unique(self):
        names = [s.name for s in commands.GROUP_COMMANDS]
        self.assertEqual(len(names), len(set(names)), "дубли имён в GROUP_COMMANDS")

    def test_only_user_commands_in_menu(self):
        # Мод-команды не публикуются в меню — см. спеку, раздел «Меню команд».
        for spec in commands.GROUP_COMMANDS:
            if spec.in_menu:
                self.assertEqual(spec.access, commands.Access.USER,
                                 f"/{spec.name} в меню, но не USER")

    def test_mywarns_and_rules_are_in_menu(self):
        in_menu = {s.name for s in commands.GROUP_COMMANDS if s.in_menu}
        self.assertEqual(in_menu, {"mywarns", "rules"})

    def test_punitive_set_matches_registry(self):
        for name in commands.PUNITIVE:
            self.assertIsNotNone(commands.spec_by_name(name),
                                 f"{name} в PUNITIVE, но не в реестре")

    def test_every_spec_has_description(self):
        for spec in commands.GROUP_COMMANDS:
            with self.subTest(command=spec.name):
                self.assertTrue(spec.description.strip())


class TestBotUsernameGlobal(unittest.TestCase):
    def test_set_and_get(self):
        commands.set_bot_username("degradach_bot")
        self.assertEqual(commands.get_bot_username(), "degradach_bot")
        commands.set_bot_username(None)
        self.assertIsNone(commands.get_bot_username())


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Прогнать тест, убедиться что падает**

Run: `uv run python tools/run_tests.py -k v510_commands`
Expected: FAIL — `ModuleNotFoundError: No module named 'commands'`

- [ ] **Step 3: Написать `commands.py`**

Паттерны копируются из `bot_handlers.py:515-570` **дословно**, меняется только якорь: `^!mute` → `^mute`. Именованные группы `target`/`dur`/`reason` не трогать — на них завязан `_resolve_punishment_target`.

```python
"""Реестр команд бота — единственный источник истины (v5.1.0).

До v5.1.0 список команд жил в трёх местах: _ALL_MOD_COMMANDS в
bot_handlers.py и два блока /help (строки 7168 и 7319). Копии успели
разойтись. Теперь команда описывается здесь один раз, а диспетчер, меню
Telegram и тексты /help выводятся из этого описания.

Модуль намеренно не зависит ни от aiogram, ни от БД: это делает его
тестируемым напрямую, без поднятия бота.

Нормализация префикса. Исторически команды жили на «!», сейчас основной
префикс — «/» (общая конвенция Telegram). «!» остаётся рабочим алиасом, но
нигде не документируется и не показывается в меню. Вместо того чтобы
править якорь в пятнадцати паттернах, префикс и суффикс «@botusername»
срезаются один раз в strip_prefix, а паттерны матчат уже нормализованную
строку: «ban @vasya Скам» вместо «!ban @vasya Скам».
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class Access(StrEnum):
    """Уровень доступа, необходимый для команды."""

    USER = "user"    # доступно всем участникам чата
    MOD = "mod"      # проверяется через _is_admin()
    ADMIN = "admin"  # дополнительно требует роль su/admin


@dataclass(frozen=True)
class CommandSpec:
    """Описание одной команды.

    :param name: имя без префикса («ban»)
    :param pattern: regex по нормализованной строке (без «^!»)
    :param args_hint: аргументы для текста справки («<длит> <причина>»)
    :param description: одна строка для /help и меню Telegram
    :param access: минимальный уровень доступа
    :param in_menu: публиковать ли в меню команд Telegram
    """

    name: str
    pattern: re.Pattern
    args_hint: str
    description: str
    access: Access
    in_menu: bool = False


# ── Паттерны ────────────────────────────────────────────────────────────
# Скопированы из bot_handlers.py без изменений, кроме якоря: «^!mute» → «^mute».
# Комментарии о причинах их нынешней формы (v4.8.3.1, v4.8.6) остались в
# bot_handlers.py рядом с историей правок.

_P_MUTE = re.compile(
    r"^mute\s+(?:(?P<target>@\w+|\d+)\s+)?(?P<dur>\d+[a-zа-я]+)\s+(?P<reason>.+)$",
    re.IGNORECASE | re.DOTALL,
)
_P_WARN = re.compile(
    r"^warn\s+(?:(?P<target>@\w+|\d+)\s+)?(?P<reason>(?!@\w+$|\d+$).+)$",
    re.IGNORECASE | re.DOTALL,
)
_P_BAN = re.compile(
    r"^ban\s+(?:(?P<target>@\w+|\d+)\s+)?(?P<reason>(?!@\w+$|\d+$).+)$",
    re.IGNORECASE | re.DOTALL,
)
_P_SMUTE = re.compile(
    r"^smute(?:\s+(?:(?P<target>@\w+|\d+)\s+)?(?P<dur>\d+[a-zа-я]+)(?:\s+(?P<reason>.+))?)?$",
    re.IGNORECASE | re.DOTALL,
)
_P_SWARN = re.compile(
    r"^swarn(?:\s+(?P<target>@\w+|\d+))?(?:\s+(?P<reason>.+))?$",
    re.IGNORECASE | re.DOTALL,
)
_P_SBAN = re.compile(
    r"^sban(?:\s+(?P<target>@\w+|\d+))?(?:\s+(?P<reason>.+))?$",
    re.IGNORECASE | re.DOTALL,
)
_P_UNMUTE = re.compile(r"^unmute\s*$", re.IGNORECASE)
_P_UNBAN = re.compile(r"^unban\s*$", re.IGNORECASE)
_P_UNWARN = re.compile(r"^unwarn(?:\s+(?P<count>\d+))?\s*$", re.IGNORECASE)
_P_WARNS = re.compile(r"^warns\s*$", re.IGNORECASE)
_P_RESETWARNS = re.compile(r"^resetwarns\s*$", re.IGNORECASE)
_P_RESETMC = re.compile(
    r"^resetmc(?:\s+(?P<target>@\w+|\d+))?\s*$",
    re.IGNORECASE,
)
_P_ALARM = re.compile(
    r"^alarm\s+(?P<state>on|off|вкл|выкл)"
    r"(?:\s+(?P<amount>\d+)\s*(?P<unit>ч|h|м|m|д|d))?"
    r"\s*$",
    re.IGNORECASE,
)
_P_MYWARNS = re.compile(r"^mywarns\s*$", re.IGNORECASE)
_P_RULES = re.compile(r"^rules\s*$", re.IGNORECASE)


# ── Реестр групповых команд ─────────────────────────────────────────────
GROUP_COMMANDS: tuple[CommandSpec, ...] = (
    # Публичные — единственные, что попадают в меню Telegram.
    CommandSpec("mywarns", _P_MYWARNS, "", "показать свои варны", Access.USER, in_menu=True),
    CommandSpec("rules", _P_RULES, "", "ссылка на правила чата", Access.USER, in_menu=True),
    # Громкие мод-команды.
    CommandSpec("mute", _P_MUTE, "<длит> <причина>",
                "мьют. Длительность: 1d2h, 30м, 2h", Access.MOD),
    CommandSpec("warn", _P_WARN, "<причина>",
                "варн (1 поинт). Сообщение нарушителя удаляется", Access.MOD),
    CommandSpec("ban", _P_BAN, "<причина>",
                "бан. Если reply на стикер — пак автодобавляется", Access.MOD),
    # Тихие (stealth).
    CommandSpec("smute", _P_SMUTE, "<длит> [причина]",
                "мьют без публичного сообщения", Access.MOD),
    CommandSpec("swarn", _P_SWARN, "[причина]",
                "варн. Нарушитель видит причину", Access.MOD),
    CommandSpec("sban", _P_SBAN, "[причина]",
                "бан без публичного сообщения", Access.MOD),
    # Снятие ограничений.
    CommandSpec("unmute", _P_UNMUTE, "", "снять мьют (reply)", Access.MOD),
    CommandSpec("unban", _P_UNBAN, "", "снять бан (reply)", Access.MOD),
    CommandSpec("unwarn", _P_UNWARN, "[N]",
                "снять N последних варнов (по умолчанию 1)", Access.MOD),
    CommandSpec("warns", _P_WARNS, "", "показать активные варны цели", Access.MOD),
    # Только su/admin — доп. проверка роли живёт в обработчике.
    CommandSpec("resetwarns", _P_RESETWARNS, "", "обнулить варны", Access.ADMIN),
    CommandSpec("resetmc", _P_RESETMC, "[@user|tgid]",
                "обнулить счётчик автомьютов", Access.ADMIN),
    # Режим тревоги.
    CommandSpec("alarm", _P_ALARM, "on [длит] | off",
                "режим тревоги (усиленные ограничения)", Access.MOD),
)

# Команды, применяющие наказание. На них в диспетчере навешаны проверки
# self-harm и friendly-fire, и для них цель резолвится через
# _resolve_punishment_target.
PUNITIVE: frozenset[str] = frozenset({"ban", "warn", "mute", "sban", "swarn", "smute"})

# Публикуются в меню личных чатов. Административные DM-команды
# (/bansticker, /setkeywords, /linkallow и ещё около двадцати) сюда
# намеренно не входят: AllPrivateChats видят все, кто написал боту, и
# публикация вывесила бы наружу всю админскую поверхность.
DM_MENU_COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "привязать учётную запись"),
    ("help", "справка по командам"),
    ("mywarns", "показать свои варны"),
    ("rules", "ссылка на правила"),
)


# ── Имя бота ────────────────────────────────────────────────────────────
# Нужно, чтобы отличить «/ban@degradach_bot» от «/ban@other_bot». Ставится
# один раз при старте из lifespan; фильтры синхронные и сами await'ить
# bot.me() не могут.
_bot_username: str | None = None


def set_bot_username(username: str | None) -> None:
    """Запоминает username бота (без «@»). Вызывается из lifespan."""
    global _bot_username
    _bot_username = username


def get_bot_username() -> str | None:
    """Username бота, либо None если ещё не установлен."""
    return _bot_username


_HEAD = re.compile(
    r"^([!/])([A-Za-z0-9_]+)(?:@([A-Za-z0-9_]+))?(.*)$",
    re.DOTALL,
)


def strip_prefix(text: str | None, bot_username: str | None) -> str | None:
    """Срезает префикс команды и «@botusername».

    Возвращает нормализованную строку («ban @vasya Скам») либо None, если
    текст не является командой этого бота.

    Команда, явно адресованная другому боту («/ban@other_bot»), отбрасывается.
    Если собственный username ещё не известен, явная адресация тоже
    отбрасывается — перехватывать чужие команды хуже, чем пропустить свою;
    команда без «@» при этом продолжает работать.
    """
    if not text:
        return None
    m = _HEAD.match(text.lstrip())
    if m is None:
        return None
    _prefix, name, at_username, rest = m.groups()
    if at_username is not None:
        if not bot_username or at_username.lower() != bot_username.lower():
            return None
    return f"{name}{rest}"


def resolve(
    text: str | None, bot_username: str | None,
) -> tuple[CommandSpec, re.Match] | None:
    """Находит команду в тексте.

    Возвращает пару (спека, match) — match нужен вызывающему коду ради
    именованных групп target/dur/reason. None, если это не наша команда.
    """
    normalized = strip_prefix(text, bot_username)
    if normalized is None:
        return None
    for spec in GROUP_COMMANDS:
        m = spec.pattern.match(normalized)
        if m is not None:
            return spec, m
    return None


def spec_by_name(name: str) -> CommandSpec | None:
    """Спека по имени команды, либо None."""
    for spec in GROUP_COMMANDS:
        if spec.name == name:
            return spec
    return None
```

- [ ] **Step 4: Прогнать тест, убедиться что проходит**

Run: `uv run python tools/run_tests.py -k v510_commands`
Expected: PASS, все классы зелёные.

- [ ] **Step 5: Линт**

Run: `uv run ruff check .`
Expected: без новых замечаний (базовый уровень легаси — 78).

- [ ] **Step 6: Коммит**

```bash
git add commands.py tests/test_v510_commands_registry.py
git commit -m "feat(v5.1.0): реестр команд и нормализация префикса

Команда описывается один раз в commands.py. Префикс (/ или !) и
суффикс @botusername срезаются в strip_prefix, паттерны матчат уже
нормализованную строку — вместо правки якоря в пятнадцати регэкспах."
```

---

### Task 2: Диспетчер групповых команд

**Files:**
- Modify: `bot_handlers.py:515-600` (паттерны и `_is_moderation_command`), `bot_handlers.py:619-650` (фильтр), `bot_handlers.py:4167-4260` (диспетчер)
- Test: `tests/test_v510_command_dispatch.py`

**Interfaces:**
- Consumes: `commands.resolve`, `commands.get_bot_username`, `commands.PUNITIVE`, `commands.Access` (Task 1)
- Produces:
  - `_KnownCommandFilter` — заменяет `_ModerationCommandFilter`
  - `_DENIED_COOLDOWN_SECONDS: int = 60`
  - `_denied_last_call: dict[tuple[int, int], float]`
  - `_denied_prune_stale(now: float) -> None`
  - `_send_access_denied(message, chat_id: int, user) -> None`

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_v510_command_dispatch.py`:

```python
"""v5.1.0 — диспетчер групповых команд: права, удаление, кулдаун отказа.

Проверяет, что обычный участник получает ephemeral-отказ вместо тишины,
что команда удаляется в любом случае и что повторный отказ гасится
кулдауном.

Запуск: uv run python tools/run_tests.py -k v510_command_dispatch
"""
from _paths import _P  # noqa: E402
import os
import sys
import unittest

os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ["ADMIN_IDS"] = "111"
os.environ["WEB_ALLOW_NO_SECRET"] = "1"
os.environ["DB_PATH"] = "/tmp/degramod_v510_dispatch.db"

sys.path.insert(0, _P())

import re  # noqa: E402


class TestPatternsLostBangAnchor(unittest.TestCase):
    """Паттерны переехали в commands.py и больше не якорятся на «!»."""

    def test_bot_handlers_has_no_bang_anchored_patterns(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        leftovers = re.findall(r'r"\^!\w+', src)
        self.assertEqual(leftovers, [],
                         f"остались якоря на !: {leftovers}")

    def test_filter_renamed(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        self.assertIn("_KnownCommandFilter", src)
        self.assertNotIn("_ModerationCommandFilter", src)

    def test_denied_cooldown_constant_present(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        self.assertIn("_DENIED_COOLDOWN_SECONDS = 60", src)


class TestDeniedCooldown(unittest.TestCase):
    """Кулдаун отказа — чистая логика, проверяется без Telegram."""

    def setUp(self):
        import bot_handlers
        self.bh = bot_handlers
        self.bh._denied_last_call.clear()

    def test_prune_removes_stale_entries(self):
        self.bh._denied_last_call[(1, -100)] = 0.0
        self.bh._denied_prune_stale(now=self.bh._DENIED_COOLDOWN_SECONDS + 1)
        self.assertEqual(self.bh._denied_last_call, {})

    def test_prune_keeps_fresh_entries(self):
        self.bh._denied_last_call[(1, -100)] = 100.0
        self.bh._denied_prune_stale(now=101.0)
        self.assertIn((1, -100), self.bh._denied_last_call)

    def test_key_order_is_user_then_chat(self):
        # Совпадает с конвенцией _mywarns_last_call.
        self.bh._denied_last_call[(42, -100500)] = 1.0
        (user_id, chat_id), = self.bh._denied_last_call
        self.assertEqual(user_id, 42)
        self.assertEqual(chat_id, -100500)


class TestResolveDrivesDispatch(unittest.TestCase):
    """Диспетчер опирается на реестр, а не на каскад .match()."""

    def test_dispatcher_uses_commands_resolve(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        self.assertIn("commands.resolve(", src)

    def test_punitive_check_uses_registry(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        self.assertIn("commands.PUNITIVE", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Прогнать тест, убедиться что падает**

Run: `uv run python tools/run_tests.py -k v510_command_dispatch`
Expected: FAIL — `_KnownCommandFilter` не найден, остались якоря `r"^!...`.

- [ ] **Step 3: Удалить паттерны из `bot_handlers.py`**

Удалить блок определений `_CMD_MUTE`…`_CMD_ALARM` (`bot_handlers.py:515-578`) и кортеж `_ALL_MOD_COMMANDS` (`bot_handlers.py:585-591`). Историю правок (комментарии v4.8.3.1, v4.8.6) перенести в `commands.py` рядом с соответствующими паттернами.

Добавить импорт в шапку `bot_handlers.py`:

```python
import commands
```

- [ ] **Step 4: Заменить `_is_moderation_command` и фильтр**

```python
def _is_known_command(text: str) -> bool:
    """v5.1.0: True, если текст — команда этого бота.

    Раньше проверка звалась _is_moderation_command и знала только про
    модераторские команды на «!». Теперь спрашивает реестр, поэтому
    ловит и публичные /mywarns и /rules, и оба префикса.
    """
    return commands.resolve(text, commands.get_bot_username()) is not None


class _KnownCommandFilter(BaseFilter):
    """v5.1.0: матчит любую известную команду бота, независимо от прав.

    Раньше (_ModerationCommandFilter) фильтр знал только мод-команды, а
    отсутствие прав обрабатывалось молчаливым return в хендлере — команда
    обычного юзера так и оставалась висеть в чате без ответа. Теперь
    фильтр пропускает команду внутрь всегда, а хендлер решает, выполнить
    её или отказать.

    Незнакомые команды (/roll соседнего бота) реестром не резолвятся,
    сюда не попадают и проваливаются дальше — в handle_content_filters.

    v4.8.3: если message.text пустой — проверяем message.caption
    (команда может быть подписью к скриншоту нарушения).
    """

    async def __call__(self, message: types.Message) -> bool:
        text = message.text or message.caption
        if not text:
            return False
        return _is_known_command(text)
```

- [ ] **Step 5: Добавить кулдаун отказа**

Разместить рядом с `_mywarns_last_call` (`bot_handlers.py:3958`):

```python
# ── v5.1.0: кулдаун ephemeral-отказа «нет прав» ─────────────────────────
# Без него участник, долбящий /ban, заставляет бота слать по ephemeral на
# каждое нажатие и упереться во flood control уже на уровне всего бота.
# Сама команда удаляется всегда — гасится только повторный ответ.
_DENIED_COOLDOWN_SECONDS = 60
_denied_last_call: dict[tuple[int, int], float] = {}  # (user_id, chat_id) → timestamp


def _denied_prune_stale(now: float) -> None:
    """Чистит протухшие записи кулдауна отказов."""
    stale = [
        key for key, ts in _denied_last_call.items()
        if now - ts >= _DENIED_COOLDOWN_SECONDS
    ]
    for key in stale:
        del _denied_last_call[key]


async def _send_access_denied(
    message: types.Message, chat_id: int, user: types.User,
) -> None:
    """v5.1.0: ephemeral «нет прав» с кулдауном на (user_id, chat_id).

    Ошибки отправки глушит _send_ephemeral — если юзер ограничил
    ephemeral-сообщения, показать ему всё равно нечего.
    """
    now = time.time()
    key = (user.id, chat_id)
    last = _denied_last_call.get(key, 0.0)
    if now - last < _DENIED_COOLDOWN_SECONDS:
        logger.debug(
            "access denied (cooldown): user %s in chat %s", user.id, chat_id,
        )
        return
    _denied_prune_stale(now)
    _denied_last_call[key] = now
    await _send_ephemeral(
        bot=message.bot,
        chat_id=chat_id,
        recipient=user,
        text="❌ У вас нет прав на эту команду.",
    )
```

- [ ] **Step 6: Переписать голову диспетчера**

В `handle_group_command` (`bot_handlers.py:4167`) заменить декоратор `_ModerationCommandFilter()` на `_KnownCommandFilter()` и заменить блок от `if not _is_moderation_command(text):` до вычисления `is_punitive_cmd`:

```python
    text = message.text or message.caption
    if not text:
        return

    found = commands.resolve(text, commands.get_bot_username())
    if found is None:
        # Defensive guard: фильтр уже проверил, но вдруг его поменяют.
        return
    spec, cmd_match = found

    # Публичные команды обслуживают собственные хендлеры (/mywarns, /rules).
    if spec.access == commands.Access.USER:
        return

    chat_id = message.chat.id
    mod = message.from_user

    # ── v5.1.0: отказ вместо тишины ────────────────────────────────────
    # Раньше здесь стоял «if not is_adm: return»: команда обычного юзера
    # оставалась висеть в чате, и он не понимал, сработала она или нет.
    # Теперь команда удаляется в любом случае, а при отсутствии прав
    # уходит ephemeral (с кулдауном — см. _send_access_denied).
    async with async_session() as session:
        is_adm = await _is_admin(session, chat_id, mod.id)
    if not is_adm:
        try:
            await message.delete()
        except TelegramAPIError as e:
            logger.debug(
                "denied command: cannot delete message in chat %s: %s", chat_id, e,
            )
        await _send_access_denied(message, chat_id, mod)
        return

    # ── Резолв цели наказания ──────────────────────────────────────────
    is_punitive_cmd = spec.name in commands.PUNITIVE
    is_resetmc_cmd = spec.name == "resetmc"

    if is_punitive_cmd or is_resetmc_cmd:
        cmd_target_str: str | None = cmd_match.groupdict().get("target")
        ...
```

Дальше по телу заменить оставшиеся обращения к удалённым паттернам на сравнение `spec.name`:

| Было | Стало |
|---|---|
| `_CMD_UNWARN.match(text)` | `spec.name == "unwarn"` |
| `_CMD_WARNS.match(text)` | `spec.name == "warns"` |
| `_CMD_RESETWARNS.match(text)` | `spec.name == "resetwarns"` |
| `m.group(1)` у `_CMD_UNWARN` | `cmd_match.group("count")` |

Найти все места: `grep -n "_CMD_" bot_handlers.py mod_commands.py`. Каждое обязано либо исчезнуть, либо превратиться в сравнение по `spec.name`. Обработчики в `mod_commands.py` принимают уже готовые аргументы и паттернов не знают — но проверить.

- [ ] **Step 7: Прогнать тесты**

Run: `uv run python tools/run_tests.py`
Expected: PASS, включая `test_v510_command_dispatch`. Существующие тесты, ссылавшиеся на `_CMD_*` или `_ModerationCommandFilter`, обновить под новые имена — это ожидаемая часть задачи, не повод откатывать переименование.

- [ ] **Step 8: Линт и коммит**

```bash
uv run ruff check .
git add bot_handlers.py tests/test_v510_command_dispatch.py
git commit -m "feat(v5.1.0): диспетчер команд на реестре, отказ вместо тишины

Паттерны переехали в commands.py без якоря на !. Участник без прав
получает ephemeral-отказ с кулдауном 60с, команда удаляется в любом
случае. Незнакомые команды соседних ботов не перехватываются."
```

---

### Task 3: `/mywarns` с прогрессом до порога

**Files:**
- Modify: `bot_handlers.py:3958` (константы), `bot_handlers.py:4020-4060` (`_format_user_warns_for_chat`), `bot_handlers.py:4063-4160` (оба хендлера)
- Test: `tests/test_v510_mywarns_progress.py`

**Interfaces:**
- Consumes: `commands.resolve` (Task 1), `_count_warns`, `_get_chat_settings`
- Produces: `_format_warn_progress(total: int, warns_to_mute: int | None, warns_to_ban: int | None) -> str` — возвращает `"2/3 (до заглушения)"`, `"2/5 (до бана)"` или `"2"`.

- [ ] **Step 1: Написать падающий тест**

```python
"""v5.1.0 — /mywarns показывает прогресс до ближайшего порога.

Пороги warns_to_mute и warns_to_ban независимы, 0 = выключен. В
_check_warn_threshold бан проверяется раньше мьюта, поэтому при равных
порогах показывается бан.

Запуск: uv run python tools/run_tests.py -k v510_mywarns
"""
from _paths import _P  # noqa: E402
import os
import sys
import unittest

os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ["ADMIN_IDS"] = "111"
os.environ["WEB_ALLOW_NO_SECRET"] = "1"
os.environ["DB_PATH"] = "/tmp/degramod_v510_mywarns.db"

sys.path.insert(0, _P())

from bot_handlers import _format_warn_progress  # noqa: E402


class TestWarnProgress(unittest.TestCase):
    def test_only_mute_threshold(self):
        self.assertEqual(_format_warn_progress(2, 3, 0), "2/3 (до заглушения)")

    def test_only_ban_threshold(self):
        self.assertEqual(_format_warn_progress(2, 0, 5), "2/5 (до бана)")

    def test_both_thresholds_picks_nearest(self):
        self.assertEqual(_format_warn_progress(2, 3, 5), "2/3 (до заглушения)")

    def test_equal_thresholds_ban_wins(self):
        # _check_warn_threshold проверяет бан первым.
        self.assertEqual(_format_warn_progress(2, 3, 3), "2/3 (до бана)")

    def test_no_thresholds_plain_count(self):
        self.assertEqual(_format_warn_progress(2, 0, 0), "2")

    def test_thresholds_already_passed_plain_count(self):
        # Порог выключили задним числом или понизили — дробь соврала бы.
        self.assertEqual(_format_warn_progress(7, 3, 5), "7")

    def test_none_treated_as_disabled(self):
        self.assertEqual(_format_warn_progress(2, None, None), "2")

    def test_mute_nearer_than_ban_when_ban_passed(self):
        self.assertEqual(_format_warn_progress(4, 6, 5), "4/5 (до бана)")


class TestRegistryWiring(unittest.TestCase):
    def test_pattern_moved_to_registry(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        self.assertNotIn("_CMD_MYWARNS", src,
                         "паттерн должен приехать из commands.py")

    def test_mywarns_filter_exists(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        self.assertIn("class _MywarnsFilter", src)

    def test_both_prefixes_resolve(self):
        import commands
        for text in ("/mywarns", "!mywarns"):
            with self.subTest(text=text):
                found = commands.resolve(text, "degradach_bot")
                self.assertIsNotNone(found)
                self.assertEqual(found[0].name, "mywarns")


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Прогнать тест, убедиться что падает**

Run: `uv run python tools/run_tests.py -k v510_mywarns`
Expected: FAIL — `ImportError: cannot import name '_format_warn_progress'`

- [ ] **Step 3: Реализовать `_format_warn_progress`**

Разместить рядом с `_format_user_warns_for_chat`:

```python
def _format_warn_progress(
    total: int, warns_to_mute: int | None, warns_to_ban: int | None,
) -> str:
    """v5.1.0: «2/3 (до заглушения)» — прогресс до ближайшего порога.

    Пороги независимы, 0 и None означают «выключен». Берём наименьший
    включённый порог строго больше текущего счётчика. При равных порогах
    показываем бан: в _check_warn_threshold он проверяется первым.

    Если ни один включённый порог не превышает счётчик (порог понизили или
    выключили задним числом) — возвращаем голое число, потому что дробь в
    этой ситуации соврала бы.

    Величина честная: наказание срабатывает по total >= N, после чего
    варны гасятся через consumed_by_action и _count_warns их не считает.
    """
    candidates: list[tuple[int, str]] = []
    # Бан первым в списке — при равенстве порогов min() возьмёт его.
    if warns_to_ban and warns_to_ban > total:
        candidates.append((warns_to_ban, "до бана"))
    if warns_to_mute and warns_to_mute > total:
        candidates.append((warns_to_mute, "до заглушения"))
    if not candidates:
        return str(total)
    threshold, label = min(candidates, key=lambda c: c[0])
    return f"{total}/{threshold} ({label})"
```

- [ ] **Step 4: Прогнать тест, убедиться что проходит**

Run: `uv run python tools/run_tests.py -k v510_mywarns`
Expected: PASS

- [ ] **Step 5: Подключить прогресс в вывод**

Переписать `_format_user_warns_for_chat`, чтобы первая строка использовала прогресс:

```python
async def _format_user_warns_for_chat(session, user_id: int, chat_id: int) -> str | None:
    """Форматирует сводку варнов юзера для конкретного чата.

    Возвращает None если варнов нет. Иначе строку вида:
      «2/3 (до заглушения)
       • Последний варн: 12 авг 2026, 15:30»

    v5.1.0: добавлен прогресс до ближайшего порога — см. _format_warn_progress.
    """
    total = await _count_warns(session, user_id, chat_id)
    if total == 0:
        return None

    cs = await _get_chat_settings(session, chat_id)
    progress = _format_warn_progress(
        total, cs.warns_to_mute if cs else 0, cs.warns_to_ban if cs else 0,
    )

    last_warn = (await session.execute(
        select(Punishment)
        .where(
            Punishment.user_id == user_id,
            Punishment.chat_id == chat_id,
            Punishment.action_type == "warn",
            Punishment.is_revoked.is_(False),
            Punishment.consumed_by_action.is_(None),
        )
        .order_by(Punishment.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()

    lines = [progress]
    if last_warn and last_warn.created_at:
        lines.append(f"• Последний варн: {_format_msk_date_ru(last_warn.created_at)}")

    return "\n".join(lines)
```

В `handle_mywarns_group` поправить обёртку текста:

```python
    if warn_info is None:
        text = "✅ У вас нет варнов в этом чате."
    else:
        text = f"📊 Ваши варны в этом чате: {warn_info}"
```

- [ ] **Step 6: Перевести хендлеры на реестр**

Заменить декораторы и внутренние проверки:

```python
@router.message(F.chat.type.in_(["group", "supergroup"]), _MywarnsFilter())
```

где рядом с `_KnownCommandFilter` добавить:

```python
class _MywarnsFilter(BaseFilter):
    """v5.1.0: матчит /mywarns (и legacy !mywarns) через реестр."""

    async def __call__(self, message: types.Message) -> bool:
        found = commands.resolve(message.text, commands.get_bot_username())
        return found is not None and found[0].name == "mywarns"
```

Удалить `_CMD_MYWARNS` и внутренние `if not _CMD_MYWARNS.match(...)` в обоих хендлерах. Для DM-хендлера использовать тот же фильтр.

- [ ] **Step 7: Прогнать всю сюиту**

Run: `uv run python tools/run_tests.py`
Expected: PASS. Существующие тесты `/mywarns` ожидают старый текст — обновить их под новый формат.

- [ ] **Step 8: Линт и коммит**

```bash
uv run ruff check .
git add bot_handlers.py tests/test_v510_mywarns_progress.py
git commit -m "feat(v5.1.0): /mywarns показывает прогресс до порога

Формат «2/3 (до заглушения)». Пороги независимы, 0 = выключен, при
равных побеждает бан — в _check_warn_threshold он проверяется первым.
Если включённый порог не превышает счётчик, дробь не показывается."
```

---

### Task 4: `/rules` и колонка `rules_url`

**Files:**
- Modify: `db.py:171-345` (модель `ChatSettings`), `db.py:1193` (блок миграции рядом с via_bot), `bot_handlers.py` (новый хендлер), `web/admin_chats.py:157,428` (форма), `templates/admin_chats.html`
- Create: `migrations/versions/a1b2c3d4e5f6_v5_1_0_rules_url.py`
- Test: `tests/test_v510_rules.py`

**Interfaces:**
- Consumes: `commands.resolve` (Task 1), `_send_ephemeral`
- Produces:
  - `ChatSettings.rules_url: Column(String, nullable=True)`
  - `bot_handlers.RULES_URL_DEFAULT: str = "https://rules.degradach.ru/"`
  - `bot_handlers._resolve_rules_url(cs) -> str`
  - `_RULES_COOLDOWN_SECONDS: int = 60`

- [ ] **Step 1: Написать падающий тест**

```python
"""v5.1.0 — /rules: ссылка на правила, per-chat с дефолтом.

Запуск: uv run python tools/run_tests.py -k v510_rules
"""
from _paths import _P  # noqa: E402
import os
import sys
import unittest

os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ["ADMIN_IDS"] = "111"
os.environ["WEB_ALLOW_NO_SECRET"] = "1"
os.environ["DB_PATH"] = "/tmp/degramod_v510_rules.db"

sys.path.insert(0, _P())

import bot_handlers  # noqa: E402
from db import ChatSettings  # noqa: E402


class _FakeSettings:
    def __init__(self, rules_url):
        self.rules_url = rules_url


class TestRulesUrlResolution(unittest.TestCase):
    def test_default_when_none(self):
        self.assertEqual(
            bot_handlers._resolve_rules_url(_FakeSettings(None)),
            "https://rules.degradach.ru/",
        )

    def test_default_when_empty_string(self):
        self.assertEqual(
            bot_handlers._resolve_rules_url(_FakeSettings("   ")),
            "https://rules.degradach.ru/",
        )

    def test_default_when_settings_missing(self):
        self.assertEqual(
            bot_handlers._resolve_rules_url(None),
            "https://rules.degradach.ru/",
        )

    def test_per_chat_override(self):
        self.assertEqual(
            bot_handlers._resolve_rules_url(_FakeSettings("https://example.org/r")),
            "https://example.org/r",
        )

    def test_override_is_stripped(self):
        self.assertEqual(
            bot_handlers._resolve_rules_url(_FakeSettings("  https://example.org/r  ")),
            "https://example.org/r",
        )


class TestModelAndMigration(unittest.TestCase):
    def test_column_on_model(self):
        self.assertTrue(hasattr(ChatSettings, "rules_url"))

    def test_legacy_migration_block_present(self):
        with open(_P("db.py")) as f:
            src = f.read()
        self.assertIn('"rules_url" not in existing_cols', src)
        self.assertIn("ALTER TABLE chat_settings ADD COLUMN rules_url", src)

    def test_alembic_revision_present(self):
        import pathlib
        versions = pathlib.Path(_P("migrations/versions"))
        found = [p for p in versions.glob("*.py") if "rules_url" in p.name]
        self.assertTrue(found, "нет ревизии Alembic для rules_url")

    def test_default_constant(self):
        self.assertEqual(bot_handlers.RULES_URL_DEFAULT, "https://rules.degradach.ru/")


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Прогнать тест, убедиться что падает**

Run: `uv run python tools/run_tests.py -k v510_rules`
Expected: FAIL — нет `_resolve_rules_url`, нет колонки.

- [ ] **Step 3: Добавить колонку в модель**

В `db.py`, в `ChatSettings`, рядом с via-bot полями (`db.py:334`):

```python
    # v5.1.0: ссылка на правила для команды /rules. Пусто → RULES_URL_DEFAULT.
    # Бот мультичатовый, и у чатов со временем заводятся свои правила —
    # колонка дешевле, чем миграция задним числом.
    rules_url = Column(String, nullable=True)
```

- [ ] **Step 4: Добавить блок легаси-миграции**

В `init_db()`, в блок миграций `chat_settings` рядом с via_bot (`db.py:1193`). Разместить **до** любого ORM-запроса к `chat_settings`:

```python
        if "rules_url" not in existing_cols:
            await conn.exec_driver_sql(
                "ALTER TABLE chat_settings ADD COLUMN rules_url VARCHAR"
            )
```

- [ ] **Step 5: Добавить Alembic-ревизию**

Создать `migrations/versions/a1b2c3d4e5f6_v5_1_0_rules_url.py`. В репозитории пока одна ревизия — `2334dcf313d1`, от неё и наследуемся:

```python
"""v5_1_0_rules_url

Revision ID: a1b2c3d4e5f6
Revises: 2334dcf313d1
Create Date: 2026-08-22 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '2334dcf313d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Добавляет chat_settings.rules_url (v5.1.0, команда /rules)."""
    op.add_column('chat_settings', sa.Column('rules_url', sa.String(), nullable=True))


def downgrade() -> None:
    """Убирает chat_settings.rules_url."""
    op.drop_column('chat_settings', 'rules_url')
```

- [ ] **Step 6: Реализовать хелпер и хендлеры**

В `bot_handlers.py`:

```python
# ── v5.1.0: /rules ──────────────────────────────────────────────────────
RULES_URL_DEFAULT = "https://rules.degradach.ru/"
_RULES_COOLDOWN_SECONDS = 60
_rules_last_call: dict[tuple[int, int], float] = {}  # (user_id, chat_id) → ts


def _resolve_rules_url(cs) -> str:
    """Ссылка на правила для чата: настройка или дефолт.

    Пустая строка и пробелы считаются «не задано» — админ мог очистить
    поле в веб-панели, и это должно означать возврат к дефолту, а не
    отправку пустой ссылки.
    """
    if cs is None:
        return RULES_URL_DEFAULT
    url = (getattr(cs, "rules_url", None) or "").strip()
    return url or RULES_URL_DEFAULT


class _RulesFilter(BaseFilter):
    """v5.1.0: матчит /rules через реестр."""

    async def __call__(self, message: types.Message) -> bool:
        found = commands.resolve(message.text, commands.get_bot_username())
        return found is not None and found[0].name == "rules"


@router.message(F.chat.type.in_(["group", "supergroup"]), _RulesFilter())
async def handle_rules_group(message: types.Message) -> None:
    """/rules в группе — удаляет команду, отвечает ephemeral со ссылкой."""
    if not message.from_user:
        return

    chat_id = message.chat.id
    user = message.from_user

    try:
        await message.delete()
    except TelegramAPIError as e:
        logger.debug("rules: cannot delete command in chat %s: %s", chat_id, e)

    now = time.time()
    key = (user.id, chat_id)
    if now - _rules_last_call.get(key, 0.0) < _RULES_COOLDOWN_SECONDS:
        return
    stale = [
        k for k, ts in _rules_last_call.items()
        if now - ts >= _RULES_COOLDOWN_SECONDS
    ]
    for k in stale:
        del _rules_last_call[k]
    _rules_last_call[key] = now

    async with async_session() as session:
        cs = await _get_chat_settings(session, chat_id)
    url = _resolve_rules_url(cs)

    await _send_ephemeral(
        bot=message.bot,
        chat_id=chat_id,
        recipient=user,
        text=f'📜 Правила чата: <a href="{html.escape(url, quote=True)}">{html.escape(url, quote=False)}</a>',
    )


@router.message(F.chat.type == "private", _RulesFilter())
async def handle_rules_dm(message: types.Message) -> None:
    """/rules в личке — обычный ответ с дефолтной ссылкой."""
    try:
        await tg_safe_call(
            lambda: message.reply(f"📜 Правила: {RULES_URL_DEFAULT}", parse_mode=None),
            label="rules_dm_reply",
        )
    except TelegramAPIError as e:
        logger.debug("rules: cannot reply in DM: %s", e)
```

- [ ] **Step 7: Добавить поле в веб-форму**

В `web/admin_chats.py` в сигнатуру `admin_chats_update` добавить параметр (обязательно `Form("")`, а не `Form(...)` — пустое текстовое поле иначе отсекается сырым 422, см. контракт в `tests/test_v4812_empty_form_fields.py`):

```python
    rules_url: str = Form(""),
```

И в теле, рядом с присвоением via-bot полей (`web/admin_chats.py:428`):

```python
        # v5.1.0: пусто → дефолт из RULES_URL_DEFAULT (решается на чтении).
        cs.rules_url = (rules_url or "").strip() or None
```

В `templates/admin_chats.html`, в главную форму карточки чата:

```html
<label style="display:block; margin-top:8px;">
  <span style="font-size:10px; color:var(--text-dim);">RULES URL</span>
  <input type="text" name="rules_url" value="{{ c.rules_url or '' }}"
         placeholder="https://rules.degradach.ru/"
         style="width:100%;">
</label>
```

Поле обязано лежать **внутри** `<form action="/admin/chats/{id}/update">` и не внутри вложенной формы — вложенные `<form>` уже ломали сохранение настроек чата (changelog, `templates/base.html:1341`).

- [ ] **Step 8: Прогнать тесты и линт**

Run: `uv run python tools/run_tests.py -k v510_rules && uv run ruff check .`
Expected: PASS

- [ ] **Step 9: Коммит**

```bash
git add db.py bot_handlers.py web/admin_chats.py templates/admin_chats.html \
        migrations/versions/a1b2c3d4e5f6_v5_1_0_rules_url.py tests/test_v510_rules.py
git commit -m "feat(v5.1.0): команда /rules и колонка chat_settings.rules_url

Ephemeral со ссылкой на правила, кулдаун 60с. Пусто → дефолт
https://rules.degradach.ru/. Миграция обоими путями: init_db + Alembic."
```

---

### Task 5: Публикация меню команд

**Files:**
- Modify: `bot.py:1368-1373` (блок `delete_my_commands`)
- Test: `tests/test_v510_menu_scopes.py`

**Interfaces:**
- Consumes: `commands.GROUP_COMMANDS`, `commands.DM_MENU_COMMANDS`, `commands.set_bot_username` (Task 1)
- Produces: `bot._publish_bot_commands(bot) -> None`

- [ ] **Step 1: Написать падающий тест**

```python
"""v5.1.0 — меню команд: что публикуется и в каких скоупах.

Мод-команды не публикуются нигде: скоуп AllChatAdministrators у Telegram
означает настоящих админов чата, а _is_admin про них не знает — он смотрит
ADMIN_IDS, WebUser и chat_admins. В этой инсталляции TG-админов больше,
и такой скоуп рекламировал бы /ban тем, кому он запрещён.

Запуск: uv run python tools/run_tests.py -k v510_menu
"""
from _paths import _P  # noqa: E402
import asyncio
import os
import sys
import unittest

os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ["ADMIN_IDS"] = "111"
os.environ["WEB_ALLOW_NO_SECRET"] = "1"
os.environ["DB_PATH"] = "/tmp/degramod_v510_menu.db"

sys.path.insert(0, _P())

import commands  # noqa: E402


class _RecordingBot:
    """Фиксирует вызовы set_my_commands/delete_my_commands."""

    def __init__(self):
        self.calls = []
        self.deleted = []

    async def set_my_commands(self, commands_list, scope=None, **kw):
        self.calls.append((type(scope).__name__ if scope else None,
                           [c.command for c in commands_list]))

    async def delete_my_commands(self, scope=None, **kw):
        self.deleted.append(type(scope).__name__ if scope else None)


class TestMenuScopes(unittest.TestCase):
    def setUp(self):
        import bot as bot_module
        self.mod = bot_module
        self.bot = _RecordingBot()
        asyncio.run(self.mod._publish_bot_commands(self.bot))
        self.by_scope = dict(self.bot.calls)

    def test_group_scope_has_only_user_commands(self):
        self.assertEqual(sorted(self.by_scope["BotCommandScopeAllGroupChats"]),
                         ["mywarns", "rules"])

    def test_private_scope_matches_registry(self):
        expected = sorted(name for name, _ in commands.DM_MENU_COMMANDS)
        self.assertEqual(sorted(self.by_scope["BotCommandScopeAllPrivateChats"]),
                         expected)

    def test_no_mod_commands_anywhere(self):
        published = {c for _scope, names in self.bot.calls for c in names}
        mod_names = {
            s.name for s in commands.GROUP_COMMANDS
            if s.access != commands.Access.USER
        }
        self.assertEqual(published & mod_names, set(),
                         "мод-команды не должны публиковаться ни в одном скоупе")

    def test_chat_administrators_scope_not_set(self):
        # Скоупы Telegram не складываются: пустой админский скоуп отобрал бы
        # у админов /mywarns и /rules. Не задаём его вовсе — админы
        # наследуют AllGroupChats.
        self.assertNotIn("BotCommandScopeAllChatAdministrators", self.by_scope)

    def test_default_scope_cleared(self):
        self.assertIn(None, self.bot.deleted)


class TestFailureIsNonFatal(unittest.TestCase):
    def test_telegram_failure_does_not_raise(self):
        import bot as bot_module

        class _BrokenBot:
            async def set_my_commands(self, *a, **kw):
                raise RuntimeError("Telegram недоступен")

            async def delete_my_commands(self, *a, **kw):
                raise RuntimeError("Telegram недоступен")

        # Публикация меню не должна ронять старт бота.
        asyncio.run(bot_module._publish_bot_commands(_BrokenBot()))


class TestStealthRemovedDeliberately(unittest.TestCase):
    def test_blanket_delete_my_commands_gone(self):
        with open(_P("bot.py")) as f:
            src = f.read()
        self.assertNotIn("Bot commands cleared (stealth mode)", src,
                         "безусловная очистка меню заменена на _publish_bot_commands")


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Прогнать тест, убедиться что падает**

Run: `uv run python tools/run_tests.py -k v510_menu`
Expected: FAIL — нет `_publish_bot_commands`.

- [ ] **Step 3: Реализовать публикацию**

В `bot.py` добавить импорты:

```python
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
)
import commands as commands_registry
```

и функцию:

```python
async def _publish_bot_commands(bot) -> None:
    """v5.1.0: публикует меню команд по скоупам.

    До v5.1.0 здесь стоял безусловный delete_my_commands() — бот прятал
    меню целиком (стелс). Теперь наружу выходят ровно две публичные
    команды, /mywarns и /rules.

    Мод-команды не публикуются нигде. Скоуп AllChatAdministrators у
    Telegram означает настоящих админов чата, а _is_admin
    (bot_handlers.py) Telegram не спрашивает вовсе — он смотрит ADMIN_IDS,
    WebUser и chat_admins. Админов чата в этой инсталляции заметно больше,
    чем модераторов в БД, поэтому такой скоуп рекламировал бы /ban строго
    более широкому кругу, чем тот, кому команда разрешена.

    AllChatAdministrators при этом не задаётся вовсе, а не задаётся
    пустым: скоупы Telegram не складываются, более узкий замещает более
    широкий целиком, и пустой админский скоуп отобрал бы у админов
    /mywarns и /rules. Не задавая его, мы позволяем им унаследовать
    AllGroupChats.

    Любая ошибка Telegram гасится: меню — не повод не стартовать.
    """
    try:
        group_cmds = [
            BotCommand(command=spec.name, description=spec.description)
            for spec in commands_registry.GROUP_COMMANDS
            if spec.in_menu
        ]
        dm_cmds = [
            BotCommand(command=name, description=description)
            for name, description in commands_registry.DM_MENU_COMMANDS
        ]
        await bot.set_my_commands(group_cmds, scope=BotCommandScopeAllGroupChats())
        await bot.set_my_commands(dm_cmds, scope=BotCommandScopeAllPrivateChats())
        # Default чистим: он служит фолбэком для скоупов, которые мы не задаём.
        await bot.delete_my_commands()
        logger.info(
            "Bot commands published: %d in groups, %d in DM",
            len(group_cmds), len(dm_cmds),
        )
    except Exception as e:
        logger.warning("_publish_bot_commands failed: %s", e)
```

- [ ] **Step 4: Подключить в `lifespan`**

Заменить блок `bot.py:1368-1373` целиком:

```python
    # v5.1.0: меню команд вместо безусловной очистки (см. _publish_bot_commands).
    # Username нужен фильтрам, чтобы отличать /ban@degradach_bot от
    # /ban@other_bot — ставим до публикации меню.
    try:
        me = await bot.me()
        commands_registry.set_bot_username(me.username)
        logger.info("Bot username: @%s", me.username)
    except Exception as e:
        logger.warning("cannot resolve bot username: %s", e)
    await _publish_bot_commands(bot)
```

- [ ] **Step 5: Прогнать тест и линт**

Run: `uv run python tools/run_tests.py -k v510_menu && uv run ruff check .`
Expected: PASS

- [ ] **Step 6: Коммит**

```bash
git add bot.py tests/test_v510_menu_scopes.py
git commit -m "feat(v5.1.0): меню команд по скоупам, мод-команды скрыты

Наружу выходят /mywarns и /rules. AllChatAdministrators не задаётся:
скоуп Telegram означает админов чата, а _is_admin про них не знает —
он рекламировал бы /ban тем, кому команда запрещена."
```

---

### Task 6: Модель `BotWhitelist` и обход via-bot фильтра

**Files:**
- Modify: `db.py` (модель + `init_db()`), `bot_handlers.py:7884-7935` (`_check_via_bot_filter`)
- Create: `migrations/versions/b2c3d4e5f6a7_v5_1_0_bot_whitelist.py`
- Test: `tests/test_v510_bot_whitelist.py`

**Interfaces:**
- Consumes: —
- Produces:
  - `db.BotWhitelist` с полями `id`, `chat_id`, `bot_username`, `bot_id`, `note`, `added_by_mod_id`, `created_at`; `UniqueConstraint("chat_id", "bot_username")`
  - `bot_handlers._is_bot_whitelisted(session, chat_id: int, bot_username: str, bot_id: int) -> bool`

- [ ] **Step 1: Написать падающий тест**

```python
"""v5.1.0 — вайтлист ботов: обход via-bot кулдауна и автомьюта.

Запуск: uv run python tools/run_tests.py -k v510_bot_whitelist
"""
from _paths import _P  # noqa: E402
import asyncio
import os
import sys
import unittest

os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ["ADMIN_IDS"] = "111"
os.environ["WEB_ALLOW_NO_SECRET"] = "1"
os.environ["DB_PATH"] = "/tmp/degramod_v510_whitelist.db"

sys.path.insert(0, _P())

import bot_handlers  # noqa: E402
from db import BotWhitelist, async_session, init_db  # noqa: E402

CHAT = -1001234567890
OTHER_CHAT = -1009876543210


class TestWhitelistMatching(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        async with async_session() as s:
            for row in (await s.execute(
                __import__("sqlalchemy").select(BotWhitelist)
            )).scalars().all():
                await s.delete(row)
            await s.commit()

    async def _add(self, chat_id, username, bot_id=None):
        async with async_session() as s:
            s.add(BotWhitelist(chat_id=chat_id, bot_username=username, bot_id=bot_id))
            await s.commit()

    async def test_per_chat_match(self):
        await self._add(CHAT, "gif")
        async with async_session() as s:
            self.assertTrue(await bot_handlers._is_bot_whitelisted(s, CHAT, "gif", 42))

    async def test_per_chat_does_not_leak_to_other_chat(self):
        await self._add(CHAT, "gif")
        async with async_session() as s:
            self.assertFalse(
                await bot_handlers._is_bot_whitelisted(s, OTHER_CHAT, "gif", 42)
            )

    async def test_global_applies_everywhere(self):
        await self._add(0, "gif")
        async with async_session() as s:
            self.assertTrue(
                await bot_handlers._is_bot_whitelisted(s, OTHER_CHAT, "gif", 42)
            )

    async def test_match_by_bot_id_when_username_changed(self):
        await self._add(0, "oldname", bot_id=42)
        async with async_session() as s:
            self.assertTrue(
                await bot_handlers._is_bot_whitelisted(s, CHAT, "newname", 42)
            )

    async def test_username_match_is_case_insensitive(self):
        await self._add(0, "gif")
        async with async_session() as s:
            self.assertTrue(await bot_handlers._is_bot_whitelisted(s, CHAT, "GIF", 42))

    async def test_unknown_bot_not_whitelisted(self):
        await self._add(0, "gif")
        async with async_session() as s:
            self.assertFalse(
                await bot_handlers._is_bot_whitelisted(s, CHAT, "spammer", 99)
            )


class TestFilterIntegration(unittest.TestCase):
    def test_check_runs_before_rate_limit(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        body = src[src.index("async def _check_via_bot_filter"):]
        body = body[:body.index("\nasync def ", 10)]
        wl = body.index("_is_bot_whitelisted")
        rl = body.index("_via_bot_rate_limit[key] = now")
        self.assertLess(wl, rl,
                        "вайтлист обязан проверяться до записи timestamp")

    def test_whitelisted_bot_does_not_consume_slot(self):
        # Белый бот не занимает слот кулдауна — иначе он подставит
        # следующего под автомьют.
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        self.assertIn("не занимает слот", src)


class TestMigrations(unittest.TestCase):
    def test_legacy_migration_present(self):
        with open(_P("db.py")) as f:
            src = f.read()
        self.assertIn("bot_whitelist", src)

    def test_alembic_revision_present(self):
        import pathlib
        found = [p for p in pathlib.Path(_P("migrations/versions")).glob("*.py")
                 if "bot_whitelist" in p.name]
        self.assertTrue(found, "нет ревизии Alembic для bot_whitelist")


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Прогнать тест, убедиться что падает**

Run: `uv run python tools/run_tests.py -k v510_bot_whitelist`
Expected: FAIL — `ImportError: cannot import name 'BotWhitelist'`

- [ ] **Step 3: Добавить модель**

В `db.py`, рядом с `LinkAllowlist` (`db.py:459`):

```python
class BotWhitelist(Base):
    """v5.1.0: боты, на которых не действует via-bot кулдаун и автомьют.

    Калька с LinkAllowlist: chat_id=0 означает «во всех чатах», конкретный
    chat_id — только в этом чате.

    bot_id заполняется оппортунистически, когда бот впервые встречается в
    message.via_bot: username сменить можно, числовой id — нет. Матч идёт
    по username ИЛИ по известному bot_id.
    """

    __tablename__ = "bot_whitelist"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, nullable=False, default=0)  # 0 = global
    bot_username = Column(String, nullable=False)            # lower, без «@»
    bot_id = Column(BigInteger, nullable=True)
    note = Column(String, nullable=True)
    added_by_mod_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("chat_id", "bot_username", name="uq_bot_whitelist_chat_bot"),
    )
```

Убедиться, что `UniqueConstraint` импортирован из `sqlalchemy` в шапке `db.py`; если нет — добавить.

- [ ] **Step 4: Добавить миграции**

В `init_db()` — идемпотентное создание таблицы (рядом с остальными `CREATE TABLE IF NOT EXISTS`):

```python
        await conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS bot_whitelist ("
            "id INTEGER PRIMARY KEY, "
            "chat_id BIGINT NOT NULL DEFAULT 0, "
            "bot_username VARCHAR NOT NULL, "
            "bot_id BIGINT, "
            "note VARCHAR, "
            "added_by_mod_id BIGINT, "
            "created_at DATETIME, "
            "UNIQUE (chat_id, bot_username))"
        )
```

Создать `migrations/versions/b2c3d4e5f6a7_v5_1_0_bot_whitelist.py`:

```python
"""v5_1_0_bot_whitelist

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-22 12:05:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Создаёт bot_whitelist (v5.1.0)."""
    op.create_table(
        'bot_whitelist',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('bot_username', sa.String(), nullable=False),
        sa.Column('bot_id', sa.BigInteger(), nullable=True),
        sa.Column('note', sa.String(), nullable=True),
        sa.Column('added_by_mod_id', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('chat_id', 'bot_username',
                            name='uq_bot_whitelist_chat_bot'),
    )


def downgrade() -> None:
    """Удаляет bot_whitelist."""
    op.drop_table('bot_whitelist')
```

- [ ] **Step 5: Реализовать проверку и встроить в фильтр**

В `bot_handlers.py`:

```python
async def _is_bot_whitelisted(
    session, chat_id: int, bot_username: str, bot_id: int,
) -> bool:
    """v5.1.0: True, если бот в вайтлисте для этого чата или глобально.

    Матч по username (регистронезависимо) ИЛИ по bot_id — username бота
    можно сменить, числовой id нет.
    """
    username = (bot_username or "").lower().lstrip("@")
    row = (await session.execute(
        select(BotWhitelist).where(
            BotWhitelist.chat_id.in_((0, chat_id)),
            or_(
                func.lower(BotWhitelist.bot_username) == username,
                BotWhitelist.bot_id == bot_id,
            ),
        ).limit(1)
    )).scalar_one_or_none()
    return row is not None
```

Импортировать `or_` и `func` из `sqlalchemy` и `BotWhitelist` из `db`, если ещё не импортированы.

В `_check_via_bot_filter`, внутри существующего `async with async_session() as session:` (там уже читаются настройки — новую сессию не открывать):

```python
            rate_limit = settings.via_bot_rate_limit_seconds or 300
            mute_min = settings.via_bot_mute_minutes or 10
            # v5.1.0: вайтлист проверяется ДО rate-limit и timestamp не
            # пишется — белый бот не занимает слот кулдауна, иначе он
            # подставил бы под автомьют следующего отправителя.
            if await _is_bot_whitelisted(
                session, chat_id, vb.username or "", vb.id,
            ):
                logger.debug(
                    "Via-bot filter: @%s whitelisted in chat %s — skip",
                    (vb.username or "unknown"), chat_id,
                )
                return False
```

Блок разместить **после** чтения `rate_limit`/`mute_min`, но **до** выхода из `async with` и до любой работы с `_via_bot_rate_limit`.

- [ ] **Step 6: Прогнать тесты и линт**

Run: `uv run python tools/run_tests.py -k v510_bot_whitelist && uv run ruff check .`
Expected: PASS

- [ ] **Step 7: Коммит**

```bash
git add db.py bot_handlers.py migrations/versions/b2c3d4e5f6a7_v5_1_0_bot_whitelist.py \
        tests/test_v510_bot_whitelist.py
git commit -m "feat(v5.1.0): вайтлист ботов для via-bot фильтра

Per-chat и global (chat_id=0), матч по username или bot_id. Проверка
идёт до rate-limit и не пишет timestamp: белый бот не занимает слот
кулдауна, иначе подставил бы следующего под автомьют."
```

---

### Task 7: Управление вайтлистом из Телеграма

**Files:**
- Modify: `bot_handlers.py` (рядом с `cmd_linkallow`, `bot_handlers.py:5301-5417`)
- Test: `tests/test_v510_whitelist_commands.py`

**Interfaces:**
- Consumes: `db.BotWhitelist` (Task 6)
- Produces: хендлеры `cmd_botallow`, `cmd_botunallow`, `cmd_botallowlist`; хелпер `_parse_whitelist_scope(arg: str) -> int | None`

- [ ] **Step 1: Написать падающий тест**

```python
"""v5.1.0 — DM-команды управления вайтлистом ботов.

Доступ по ADMIN_IDS — паритет с /linkallow.

Запуск: uv run python tools/run_tests.py -k v510_whitelist_commands
"""
from _paths import _P  # noqa: E402
import os
import sys
import unittest

os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ["ADMIN_IDS"] = "111"
os.environ["WEB_ALLOW_NO_SECRET"] = "1"
os.environ["DB_PATH"] = "/tmp/degramod_v510_wlcmd.db"

sys.path.insert(0, _P())

import bot_handlers  # noqa: E402


class TestScopeParsing(unittest.TestCase):
    def test_global_keyword(self):
        self.assertEqual(bot_handlers._parse_whitelist_scope("global"), 0)

    def test_global_case_insensitive(self):
        self.assertEqual(bot_handlers._parse_whitelist_scope("GLOBAL"), 0)

    def test_numeric_chat_id(self):
        self.assertEqual(
            bot_handlers._parse_whitelist_scope("-1001234567890"), -1001234567890,
        )

    def test_garbage_returns_none(self):
        self.assertIsNone(bot_handlers._parse_whitelist_scope("не-число"))
        self.assertIsNone(bot_handlers._parse_whitelist_scope(""))


class TestHandlersRegistered(unittest.TestCase):
    def test_all_three_commands_exist(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        for name in ("botallow", "botunallow", "botallowlist"):
            self.assertIn(f'Command("{name}")', src, f"нет хендлера /{name}")

    def test_admin_only(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        body = src[src.index("async def cmd_botallow("):]
        body = body[:body.index("\n@router.message", 10)]
        self.assertIn("ADMIN_IDS", body, "/botallow должна быть только для ADMIN_IDS")

    def test_not_published_in_menu(self):
        import commands
        menu = {name for name, _ in commands.DM_MENU_COMMANDS}
        self.assertNotIn("botallow", menu)
        self.assertNotIn("botallowlist", menu)


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Прогнать тест, убедиться что падает**

Run: `uv run python tools/run_tests.py -k v510_whitelist_commands`
Expected: FAIL — нет `_parse_whitelist_scope`.

- [ ] **Step 3: Реализовать хелпер и три хендлера**

```python
# ── v5.1.0: управление вайтлистом ботов из личных сообщений ─────────────
# Паритет с /linkallow: доступ только у ADMIN_IDS, скоуп задаётся первым
# аргументом — «global» либо chat_id.


def _parse_whitelist_scope(arg: str) -> int | None:
    """«global» → 0, число → chat_id, мусор → None."""
    value = (arg or "").strip().lower()
    if value == "global":
        return 0
    try:
        return int(value)
    except ValueError:
        return None


def _normalize_bot_username(arg: str) -> str:
    """Приводит «@GifBot» к «gifbot»."""
    return (arg or "").strip().lstrip("@").lower()


@router.message(F.chat.type == "private", Command("botallow"))
async def cmd_botallow(message: types.Message) -> None:
    """v5.1.0: /botallow <chat_id|global> <@bot> — добавить бота в вайтлист."""
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.reply(
            "📋 Формат: /botallow chat_id|global <@bot>\n"
            "💡 Пример: /botallow global @gif\n"
            "    /botallow -1001234567890 @vid",
            parse_mode=None,
        )
        return

    chat_id = _parse_whitelist_scope(parts[1])
    if chat_id is None:
        await message.reply("❌ chat_id должен быть числом или 'global'", parse_mode=None)
        return

    username = _normalize_bot_username(parts[2])
    if not username:
        await message.reply("❌ Укажите @username бота", parse_mode=None)
        return

    async with async_session() as session:
        existing = (await session.execute(
            select(BotWhitelist).where(
                BotWhitelist.chat_id == chat_id,
                func.lower(BotWhitelist.bot_username) == username,
            )
        )).scalar_one_or_none()
        if existing:
            await message.reply(f"⚠️ @{username} уже в вайтлисте", parse_mode=None)
            return
        session.add(BotWhitelist(
            chat_id=chat_id,
            bot_username=username,
            added_by_mod_id=message.from_user.id,
        ))
        await session.commit()

    scope_str = "глобально" if chat_id == 0 else f"в чате {chat_id}"
    await message.reply(f"✅ @{username} добавлен в вайтлист {scope_str}", parse_mode=None)


@router.message(F.chat.type == "private", Command("botunallow"))
async def cmd_botunallow(message: types.Message) -> None:
    """v5.1.0: /botunallow <chat_id|global> <@bot> — убрать бота из вайтлиста."""
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.reply(
            "📋 Формат: /botunallow chat_id|global <@bot>", parse_mode=None,
        )
        return

    chat_id = _parse_whitelist_scope(parts[1])
    if chat_id is None:
        await message.reply("❌ chat_id должен быть числом или 'global'", parse_mode=None)
        return

    username = _normalize_bot_username(parts[2])
    async with async_session() as session:
        row = (await session.execute(
            select(BotWhitelist).where(
                BotWhitelist.chat_id == chat_id,
                func.lower(BotWhitelist.bot_username) == username,
            )
        )).scalar_one_or_none()
        if row is None:
            await message.reply(f"⚠️ @{username} не найден в вайтлисте", parse_mode=None)
            return
        await session.delete(row)
        await session.commit()

    await message.reply(f"✅ @{username} убран из вайтлиста", parse_mode=None)


@router.message(F.chat.type == "private", Command("botallowlist"))
async def cmd_botallowlist(message: types.Message) -> None:
    """v5.1.0: /botallowlist [chat_id|global] — показать вайтлист."""
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split(maxsplit=1)

    stmt = select(BotWhitelist).order_by(
        BotWhitelist.chat_id.asc(), BotWhitelist.bot_username.asc(),
    )
    if len(parts) > 1:
        chat_id = _parse_whitelist_scope(parts[1])
        if chat_id is None:
            await message.reply(
                "❌ chat_id должен быть числом или 'global'", parse_mode=None,
            )
            return
        stmt = stmt.where(BotWhitelist.chat_id == chat_id)

    async with async_session() as session:
        rows = (await session.execute(stmt)).scalars().all()

    if not rows:
        await message.reply("📋 Вайтлист ботов пуст", parse_mode=None)
        return

    lines = ["📋 Вайтлист ботов:"]
    for row in rows:
        scope = "global" if row.chat_id == 0 else str(row.chat_id)
        lines.append(f"• @{row.bot_username} — {scope}")
    await message.reply("\n".join(lines), parse_mode=None)
```

- [ ] **Step 4: Прогнать тесты и линт**

Run: `uv run python tools/run_tests.py -k v510_whitelist_commands && uv run ruff check .`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add bot_handlers.py tests/test_v510_whitelist_commands.py
git commit -m "feat(v5.1.0): /botallow, /botunallow, /botallowlist

Управление вайтлистом ботов из личных сообщений, доступ по ADMIN_IDS —
паритет с /linkallow. В меню команд не публикуются."
```

---

### Task 8: Вайтлист ботов в веб-панели

**Files:**
- Modify: `web/admin_presets.py:60-90` (загрузка данных), `web/admin_presets.py:560-640` (роуты по образцу link allowlist), `templates/admin_presets.html`
- Test: `tests/test_v510_whitelist_web.py`

**Interfaces:**
- Consumes: `db.BotWhitelist` (Task 6)
- Produces: роуты `POST /admin/presets/bots/add`, `POST /admin/presets/bots/{wl_id:int}/delete`; переменная шаблона `bot_whitelist`

- [ ] **Step 1: Написать падающий тест**

```python
"""v5.1.0 — веб-управление вайтлистом ботов на /admin/presets.

Запуск: uv run python tools/run_tests.py -k v510_whitelist_web
"""
from _paths import _P  # noqa: E402
import os
import sys
import unittest

os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ["ADMIN_IDS"] = "111"
os.environ["WEB_PASSWORD"] = "testpass"
os.environ["WEB_ALLOW_NO_SECRET"] = "1"
os.environ["WEB_COOKIE_SECURE"] = "0"
os.environ["DB_PATH"] = "/tmp/degramod_v510_wlweb.db"

sys.path.insert(0, _P())


class TestRoutesRegistered(unittest.TestCase):
    def test_add_and_delete_routes_exist(self):
        with open(_P("web/admin_presets.py")) as f:
            src = f.read()
        self.assertIn('"/admin/presets/bots/add"', src)
        self.assertIn('"/admin/presets/bots/{wl_id:int}/delete"', src)

    def test_uses_csrf_admin_dependency(self):
        with open(_P("web/admin_presets.py")) as f:
            src = f.read()
        body = src[src.index("async def admin_presets_bots_add("):]
        body = body[:body.index("\n@router.post", 10)]
        self.assertIn("require_csrf_admin", body,
                      "паритет с link allowlist по защите")

    def test_username_field_uses_empty_default(self):
        # Form(...) отсекает пустую строку сырым 422 — контракт v4.8.12.
        with open(_P("web/admin_presets.py")) as f:
            src = f.read()
        self.assertIn('bot_username: str = Form("")', src)

    def test_helpers_called_through_module(self):
        # Модули web/ зовут хелперы как web_app._helper(...), иначе
        # патчи в тестах перестают действовать.
        with open(_P("web/admin_presets.py")) as f:
            src = f.read()
        self.assertNotIn("from web_app import _req_logger", src)


class TestTemplate(unittest.TestCase):
    def test_section_rendered(self):
        with open(_P("templates/admin_presets.html")) as f:
            html = f.read()
        self.assertIn("/admin/presets/bots/add", html)
        self.assertIn("bot_whitelist", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Прогнать тест, убедиться что падает**

Run: `uv run python tools/run_tests.py -k v510_whitelist_web`
Expected: FAIL — роутов нет.

- [ ] **Step 3: Загрузить данные в страницу**

В `web/admin_presets.py` в функцию, собирающую контекст `/admin/presets` (рядом с загрузкой `LinkAllowlist`, строка ~71), добавить:

```python
        bot_whitelist = (await session.execute(
            select(BotWhitelist)
            .order_by(BotWhitelist.chat_id.asc(), BotWhitelist.bot_username.asc())
        )).scalars().all()
```

и положить `bot_whitelist` в контекст шаблона. Импорт `BotWhitelist` добавить в существующую строку импорта из `db`.

- [ ] **Step 4: Добавить роуты**

По образцу `admin_presets_links_add` / `admin_presets_links_delete` (`web/admin_presets.py:560-640`):

```python
@router.post("/admin/presets/bots/add")
async def admin_presets_bots_add(
    chat_id_str: str = Form("0"),
    bot_username: str = Form(""),
    note: str = Form(""),
    _auth: AuthUser = Depends(require_csrf_admin),
):
    """v5.1.0: добавить бота в вайтлист via-bot фильтра.

    chat_id_str = "0" означает global — паритет с link allowlist.
    """
    try:
        chat_id_int = int((chat_id_str or "0").strip())
    except ValueError:
        return RedirectResponse(
            url="/admin/presets?flash=Invalid+chat_id", status_code=303,
        )

    username = (bot_username or "").strip().lstrip("@").lower()
    if not username:
        return RedirectResponse(
            url="/admin/presets?flash=Bot+username+required", status_code=303,
        )

    async with async_session() as session:
        existing = (await session.execute(
            select(BotWhitelist).where(
                BotWhitelist.chat_id == chat_id_int,
                func.lower(BotWhitelist.bot_username) == username,
            )
        )).scalar_one_or_none()
        if existing:
            return RedirectResponse(
                url=f"/admin/presets?flash=Bot+already+whitelisted:+{username}",
                status_code=303,
            )
        session.add(BotWhitelist(
            chat_id=chat_id_int,
            bot_username=username,
            note=(note or "").strip() or None,
            added_by_mod_id=_auth.tg_user_id,
        ))
        await session.commit()
        web_app._req_logger.info(
            "bot_whitelist_add: chat_id=%d bot=%r by=%s",
            chat_id_int, username, _auth.username,
        )

    return RedirectResponse(
        url=f"/admin/presets?flash=Bot+whitelisted:+{username}", status_code=303,
    )


@router.post("/admin/presets/bots/{wl_id:int}/delete")
async def admin_presets_bots_delete(
    wl_id: int,
    _auth: AuthUser = Depends(require_csrf_admin),
):
    """v5.1.0: убрать бота из вайтлиста (hard delete, как у link allowlist)."""
    async with async_session() as session:
        row = (await session.execute(
            select(BotWhitelist).where(BotWhitelist.id == wl_id)
        )).scalar_one_or_none()
        if row is None:
            return RedirectResponse(
                url="/admin/presets?flash=Bot+not+found", status_code=303,
            )
        username = row.bot_username
        await session.delete(row)
        await session.commit()
        web_app._req_logger.info(
            "bot_whitelist_delete: id=%d bot=%r by=%s",
            wl_id, username, _auth.username,
        )

    return RedirectResponse(
        url=f"/admin/presets?flash=Bot+removed:+{username}", status_code=303,
    )
```

- [ ] **Step 5: Добавить секцию в шаблон**

В `templates/admin_presets.html`, следующей секцией после блока link allowlist (скопировать его разметку и подставить свои поля):

```html
<h2>Вайтлист ботов</h2>
<p style="color: var(--text-dim); font-size: 11px;">
  На этих ботов не действует via-bot кулдаун и автомьют. chat_id = 0 — во всех чатах.
</p>
<table>
  <tr><th>Бот</th><th>Чат</th><th>Заметка</th><th></th></tr>
  {% for wl in bot_whitelist %}
  <tr>
    <td>@{{ wl.bot_username }}</td>
    <td>{{ 'global' if wl.chat_id == 0 else wl.chat_id }}</td>
    <td>{{ wl.note or '—' }}</td>
    <td>
      <form method="post" action="/admin/presets/bots/{{ wl.id }}/delete">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <button type="submit">Удалить</button>
      </form>
    </td>
  </tr>
  {% endfor %}
</table>
<form method="post" action="/admin/presets/bots/add">
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
  <input type="text" name="bot_username" placeholder="@gif" required>
  <input type="text" name="chat_id_str" placeholder="0 = global" value="0">
  <input type="text" name="note" placeholder="заметка">
  <button type="submit">Добавить</button>
</form>
```

Имя переменной CSRF-токена взять такое же, как в форме link allowlist на этой же странице.

- [ ] **Step 6: Прогнать тесты и линт**

Run: `uv run python tools/run_tests.py && uv run ruff check .`
Expected: PASS

- [ ] **Step 7: Коммит**

```bash
git add web/admin_presets.py templates/admin_presets.html tests/test_v510_whitelist_web.py
git commit -m "feat(v5.1.0): вайтлист ботов в веб-панели

Секция на /admin/presets по образцу доменного allowlist: те же
require_csrf_admin, hard delete и flash-редиректы."
```

---

### Task 9: Формулировки наказаний

**Files:**
- Modify: `bot_handlers.py:3490-3540` (`_send_public_punishment_notice`), `bot_handlers.py:7991-8003` (via-bot текст)
- Test: `tests/test_v510_punishment_wording.py`

**Interfaces:**
- Consumes: —
- Produces: `_build_punishment_notice(action: str, display_name: str, reason: str | None, duration: int | None) -> str | None` — вынесенное построение текста, чтобы тест не поднимал Telegram.

- [ ] **Step 1: Написать падающий тест**

```python
"""v5.1.0 — формулировки публичных сообщений о наказаниях.

Единая схема «кто → что с ним сделали → по причине», русские «ёлочки».

Запуск: uv run python tools/run_tests.py -k v510_punishment_wording
"""
from _paths import _P  # noqa: E402
import os
import sys
import unittest

os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ["ADMIN_IDS"] = "111"
os.environ["WEB_ALLOW_NO_SECRET"] = "1"
os.environ["DB_PATH"] = "/tmp/degramod_v510_wording.db"

sys.path.insert(0, _P())

from bot_handlers import _build_punishment_notice  # noqa: E402


class TestWording(unittest.TestCase):
    def test_mute(self):
        self.assertEqual(
            _build_punishment_notice("mute", "Vasya", "Флуд", 7200),
            "Пользователь «<b>Vasya</b>» был заглушён на <b>2ч</b> "
            "по причине: «<i>Флуд</i>»",
        )

    def test_ban(self):
        self.assertEqual(
            _build_punishment_notice("ban", "Vasya", "Скам", None),
            "Пользователь «<b>Vasya</b>» был забанен по причине: «<i>Скам</i>»",
        )

    def test_warn(self):
        self.assertEqual(
            _build_punishment_notice("warn", "Vasya", "Мат", None),
            "Пользователь «<b>Vasya</b>» получил предупреждение "
            "по причине: «<i>Мат</i>»",
        )

    def test_unknown_action_returns_none(self):
        self.assertIsNone(_build_punishment_notice("teleport", "Vasya", "x", None))

    def test_html_escaped(self):
        text = _build_punishment_notice("ban", "<script>", "a & b", None)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("a &amp; b", text)

    def test_no_latin_quotes_left(self):
        for action in ("mute", "ban", "warn"):
            text = _build_punishment_notice(action, "V", "R", 60)
            self.assertNotIn('"', text, f"{action}: остались латинские кавычки")


class TestViaBotWording(unittest.TestCase):
    def test_new_text_present(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        self.assertIn("слишком много срал ботами и был заглушён на", src)

    def test_old_text_gone(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        self.assertNotIn("задолбал срать в чат", src)

    def test_old_mute_wording_gone(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        self.assertNotIn("замутан за", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Прогнать тест, убедиться что падает**

Run: `uv run python tools/run_tests.py -k v510_punishment_wording`
Expected: FAIL — нет `_build_punishment_notice`.

- [ ] **Step 3: Вынести построение текста и переписать формулировки**

```python
def _build_punishment_notice(
    action: str, display_name: str, reason: str | None, duration: int | None,
) -> str | None:
    """v5.1.0: текст публичного сообщения о наказании.

    Единая схема «кто → что с ним сделали → по причине» и русские
    «ёлочки». До v5.1.0 формулировки были разнобойные и неграмотные
    («замутан за "Причина" на "2ч"»).

    Возвращает None при неизвестном action — вызывающий код логирует.
    """
    name_safe = html.escape(display_name, quote=False)
    reason_safe = html.escape(reason, quote=False) if reason else ""

    if action == "ban":
        return f"Пользователь «<b>{name_safe}</b>» был забанен по причине: «<i>{reason_safe}</i>»"
    if action == "warn":
        return (
            f"Пользователь «<b>{name_safe}</b>» получил предупреждение "
            f"по причине: «<i>{reason_safe}</i>»"
        )
    if action == "mute":
        dur_safe = html.escape(_format_duration(duration) if duration else "", quote=False)
        return (
            f"Пользователь «<b>{name_safe}</b>» был заглушён на <b>{dur_safe}</b> "
            f"по причине: «<i>{reason_safe}</i>»"
        )
    return None
```

`_send_public_punishment_notice` теперь только отправляет:

```python
    text = _build_punishment_notice(
        action, _user_display_name(target), reason, duration,
    )
    if text is None:
        logger.warning("_send_public_punishment_notice: unknown action=%r", action)
        return
```

Обновить и комментарий-шапку блока (`bot_handlers.py:3490-3499`) — там перечислены старые форматы.

В via-bot тексте (`bot_handlers.py:7999-8003`):

```python
                text=(
                    f'Пользователь «<b>{name_safe}</b>» слишком много срал '
                    f'ботами и был заглушён на <b>{dur_safe}</b>'
                ),
```

Запятой перед «и» нет намеренно: одно подлежащее, два однородных сказуемых. Комментарий выше (`bot_handlers.py:7991-7993`) с описанием старого формата тоже обновить.

- [ ] **Step 4: Прогнать тесты и линт**

Run: `uv run python tools/run_tests.py && uv run ruff check .`
Expected: PASS. Существующие тесты, проверяющие старые тексты, обновить.

- [ ] **Step 5: Коммит**

```bash
git add bot_handlers.py tests/test_v510_punishment_wording.py
git commit -m "fix(v5.1.0): грамотные формулировки наказаний

Единая схема «кто → что с ним сделали → по причине», русские «ёлочки».
Построение текста вынесено в _build_punishment_notice — тестируется без
поднятия Telegram."
```

---

### Task 10: `/help`, версия, changelog, roadmap

**Files:**
- Modify: `bot_handlers.py:7168-7194` и `7319-7349` (блоки `/help`), `web_app.py:121` (`APP_VERSION`), `templates/base.html` (changelog), `roadmap.md`
- Test: `tests/test_v510_release.py`

**Interfaces:**
- Consumes: `commands.GROUP_COMMANDS` (Task 1)
- Produces: `bot_handlers._help_rows() -> list[tuple[str, str]]`

- [ ] **Step 1: Написать падающий тест**

```python
"""v5.1.0 — релизные артефакты: /help из реестра, версия, changelog.

Запуск: uv run python tools/run_tests.py -k v510_release
"""
from _paths import _P  # noqa: E402
import os
import sys
import unittest

os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ["ADMIN_IDS"] = "111"
os.environ["WEB_PASSWORD"] = "testpass"
os.environ["WEB_ALLOW_NO_SECRET"] = "1"
os.environ["DB_PATH"] = "/tmp/degramod_v510_release.db"

sys.path.insert(0, _P())


class TestHelpFromRegistry(unittest.TestCase):
    def test_help_rows_use_slash(self):
        import bot_handlers
        rows = bot_handlers._help_rows()
        self.assertTrue(rows)
        for label, description in rows:
            self.assertTrue(label.startswith("/"), f"{label} не на слэше")
            self.assertTrue(description.strip())

    def test_no_bang_commands_in_help_source(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        for name in ("!mute", "!ban", "!warn", "!sban", "!alarm", "!unwarn"):
            self.assertNotIn(f'("{name}', src, f"{name} остался в /help")

    def test_help_covers_every_mod_command(self):
        import bot_handlers
        import commands
        labels = {label.split()[0].lstrip("/") for label, _ in bot_handlers._help_rows()}
        for spec in commands.GROUP_COMMANDS:
            self.assertIn(spec.name, labels, f"/{spec.name} отсутствует в /help")


class TestVersion(unittest.TestCase):
    def test_app_version(self):
        import web_app
        self.assertEqual(web_app.APP_VERSION, "v5.1.0")

    def test_changelog_entry(self):
        with open(_P("templates/base.html")) as f:
            html = f.read()
        self.assertIn("v5.1.0", html)


class TestRoadmap(unittest.TestCase):
    def test_bothost_moved_to_v520(self):
        with open(_P("roadmap.md")) as f:
            md = f.read()
        self.assertIn("v5.2.0", md,
                      "bothost-задачи должны быть перенесены на v5.2.0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Прогнать тест, убедиться что падает**

Run: `uv run python tools/run_tests.py -k v510_release`
Expected: FAIL — нет `_help_rows`, версия старая.

- [ ] **Step 3: Генерировать `/help` из реестра**

```python
def _help_rows() -> list[tuple[str, str]]:
    """v5.1.0: строки справки из реестра команд.

    До v5.1.0 список команд был продублирован в двух блоках /help
    (строки 7168 и 7319) и в _ALL_MOD_COMMANDS. Копии разошлись. Теперь
    источник один — commands.GROUP_COMMANDS.

    Префикс «!» намеренно не упоминается: он остаётся рабочим алиасом,
    но не рекламируется.
    """
    rows: list[tuple[str, str]] = []
    for spec in commands.GROUP_COMMANDS:
        label = f"/{spec.name}"
        if spec.args_hint:
            label = f"{label} {spec.args_hint}"
        rows.append((label, spec.description))
    return rows
```

Оба блока `/help` заменить на итерацию по `_help_rows()`, сохранив нынешнее оформление через `RichText`-объекты. Строку про `!idea` оставить как есть: этот хендлер живёт отдельно с `Command("idea", prefix="!/")` и в реестр не входит — заменить в тексте на `/idea`.

- [ ] **Step 4: Поднять версию и написать changelog**

`web_app.py:121`:

```python
APP_VERSION = "v5.1.0"
```

В `templates/base.html` добавить секцию v5.1.0 первым элементом списка релизов. Содержание: переход на `/`, `!` как тихий алиас, ephemeral-отказ с кулдауном, `/mywarns` с прогрессом `x/N`, `/rules` с `chat_settings.rules_url`, меню команд по скоупам с объяснением, почему мод-команды скрыты, вайтлист ботов, новые формулировки. Оформление скопировать у соседней секции.

- [ ] **Step 5: Обновить roadmap**

В `roadmap.md`: строки `5.0.0-02`…`5.0.0-06` перевести с «перенесён в v5.1.0» на «перенесён в v5.2.0» с пометкой, что хост не поддержал свой API. Добавить строку про v5.1.0 в таблицу релизов.

- [ ] **Step 6: Прогнать всю сюиту и линт**

Run: `uv run python tools/run_tests.py && uv run ruff check .`
Expected: PASS, `tests/known_failing.txt` остаётся пустым.

- [ ] **Step 7: Проверить сборку контейнера**

Run: `docker build -t degramod .`
Expected: успех — CI гоняет то же самое.

- [ ] **Step 8: Коммит**

```bash
git add bot_handlers.py web_app.py templates/base.html roadmap.md tests/test_v510_release.py
git commit -m "release(v5.1.0): /help из реестра, версия, changelog, roadmap

Два блока /help схлопнуты в _help_rows() поверх commands.GROUP_COMMANDS.
Bothost-задачи перенесены на v5.2.0: хост не поддержал свой API."
```

---

## Проверка перед сдачей

- [ ] `uv run ruff check .` — без новых замечаний сверх легаси-базы (78).
- [ ] `uv run python tools/run_tests.py` — все файлы зелёные, `known_failing.txt` пуст.
- [ ] `docker build -t degramod .` — успех.
- [ ] `grep -rn '_CMD_' bot_handlers.py mod_commands.py` — пусто.
- [ ] `grep -rn 'задолбал срать\|замутан за' .` — пусто.
- [ ] Ручная проверка на стенде: обычный участник шлёт `/ban` → сообщение исчезает, приходит ephemeral-отказ; второй `/ban` подряд → исчезает молча (кулдаун).
- [ ] Ручная проверка: `/mywarns` и `/rules` видны в меню группы; `/ban` в меню не появляется ни у кого.
- [ ] Ручная проверка: бот из вайтлиста шлёт два сообщения подряд через inline — автомьюта нет.
