"""
test_v4103_critical_calls_wrapped.py — критичные вызовы Bot API идут через
tg_safe_call (Task 4 плана стабилизации).

Telegram отвечает 429 (TelegramRetryAfter), когда бот превышает лимит частоты.
Голый `await` в этот момент теряет вызов: исключение улетает наверх, хендлер
падает, действие не выполняется. Для ответов это терпимо — модератор повторит
команду. Для изменения состояния чата — нет.

Что здесь стережётся:

  • `set_chat_permissions` — ядро режимов чата. Потерянный вызов оставляет
    права в состоянии предыдущего режима, а следующий тик снимет snapshot уже
    с испорченного (инвариант 5 в CLAUDE.md: порядок alarm → sanitary → night
    существует ровно чтобы права не «залипали»).
  • `restrict_chat_member` / `ban_chat_member` / `unban_chat_member` —
    наказание записано в БД и показано в панели, но в Telegram не применено.
  • `delete_message` — сообщение нарушителя остаётся висеть в чате.
  • `leave_chat` — бот числится удалённым из чата, но продолжает в нём быть.

Ответы (`reply`, `send_message` в чат) намеренно НЕ входят в список: их
потеря видна сразу и лечится повтором команды, а оборачивать двести
call site ради этого — большой дифф без разбора смысла. Решение
зафиксировано в CLAUDE.md: «Новый критичный вызов оборачивай».
"""
from __future__ import annotations

import ast
import os
import re
import sys
import unittest
from pathlib import Path

from _paths import _P

os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("ADMIN_IDS", "1")

sys.path.insert(0, _P())

WORK_DIR = Path(_P())

# Методы Bot API, теряющие состояние при 429.
_CRITICAL = (
    "restrict_chat_member",
    "ban_chat_member",
    "unban_chat_member",
    "set_chat_permissions",
    "promote_chat_member",
    "leave_chat",
    "delete_message",
)

# Где ищем. web/ — роуты панели, корень — логика бота.
_SOURCES = (
    "bot_handlers.py", "mod_commands.py", "chat_modes.py", "modchat.py",
    "bot.py", "web/admin_chats.py", "web/api.py", "web/admin_users.py",
)


def _strings_and_comments(src: str) -> list[tuple[int, int]]:
    """Диапазоны строк, занятые докстроками и комментариями.

    Нужны, чтобы сторож не срабатывал на упоминание метода в тексте: в
    bot_handlers.py докстроки подробно разбирают, как работает
    `bot.ban_chat_member()`, и наивный грep считает это вызовом.
    """
    spans = []
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            spans.append((node.lineno, node.end_lineno or node.lineno))
    return spans


def _bare_critical_calls(path: Path) -> list[str]:
    """Возвращает описания вызовов критичных методов без tg_safe_call."""
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    literal_spans = _strings_and_comments(src)

    def in_literal(lineno: int) -> bool:
        return any(a <= lineno <= b for a, b in literal_spans)

    pattern = re.compile(r"await\s+[\w\.\[\]_]*\.(" + "|".join(_CRITICAL) + r")\(")
    found = []
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("#") or in_literal(i):
            continue
        m = pattern.search(line)
        if not m:
            continue
        # tg_safe_call оборачивает через lambda — обёртка стоит на 1-3 строки выше.
        context = "\n".join(lines[max(0, i - 4):i])
        if "tg_safe_call" in context:
            continue
        found.append(f"{path.name}:{i} — {m.group(1)}")
    return found


class TestCriticalCallsWrapped(unittest.TestCase):

    def test_no_bare_critical_calls(self):
        """Ни одного критичного вызова Bot API мимо tg_safe_call.

        Этот тест — сторож: он падает не только на текущем долге, но и на
        любом новом вызове, добавленном без обёртки.
        """
        bare = []
        for name in _SOURCES:
            path = WORK_DIR / name
            if path.exists():
                bare.extend(_bare_critical_calls(path))
        self.assertEqual(
            bare, [],
            "критичные вызовы Bot API без tg_safe_call:\n  " + "\n  ".join(bare),
        )

    def test_guard_detects_planted_violation(self):
        """Сторож реально ловит нарушение, а не всегда зелен.

        Без этой проверки тест выше мог бы молча деградировать — например,
        если регулярка перестанет совпадать после смены стиля вызовов.
        """
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write("async def x(bot):\n    await bot.set_chat_permissions(chat_id=1)\n")
            tmp = Path(f.name)
        try:
            self.assertEqual(len(_bare_critical_calls(tmp)), 1)
        finally:
            tmp.unlink()

    def test_guard_ignores_docstring_mentions(self):
        """Упоминание метода в докстроке — не вызов.

        В bot_handlers.py докстроки разбирают поведение bot.ban_chat_member();
        наивный грep считал их нарушениями.
        """
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write('async def x(bot):\n    """Зовёт await bot.ban_chat_member() внутри."""\n    return 1\n')
            tmp = Path(f.name)
        try:
            self.assertEqual(_bare_critical_calls(tmp), [])
        finally:
            tmp.unlink()


class TestSetChatPermissionsRetries(unittest.IsolatedAsyncioTestCase):
    """Поведенческая проверка: применение прав переживает 429."""

    async def test_retries_after_flood_control(self):
        """При 429 права применяются со второй попытки, а не теряются.

        Это ядро режимов чата: потерянный вызов оставит права предыдущего
        режима, и следующий тик снимет snapshot с испорченного состояния.
        """
        from unittest.mock import AsyncMock, MagicMock

        from aiogram.exceptions import TelegramRetryAfter

        import chat_modes

        calls = []

        async def flaky(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise TelegramRetryAfter(method=MagicMock(), message="flood", retry_after=0)
            return True

        bot = MagicMock()
        bot.set_chat_permissions = AsyncMock(side_effect=flaky)
        perms = MagicMock()

        ok = await chat_modes._apply_chat_permissions(bot, -100123, perms)

        self.assertTrue(ok, "после ретрая права должны примениться")
        self.assertEqual(len(calls), 2, "должна быть вторая попытка")
        self.assertTrue(
            calls[-1].get("use_independent_chat_permissions"),
            "инвариант: use_independent_chat_permissions обязателен и при ретрае",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
