"""
app_state.py — v4.8.9: реестр функций bot.py, импортируемых из других модулей.

Зачем:
  До v4.8.9 в bot.py был хак:
      sys.modules.setdefault("bot", _self_module)
  Он позволял late import `from bot import _exit_night_mode` из bot_handlers.py
  и web_app.py — даже если bot.py запущен как __main__. Это путает IDE, ломает
  тесты и затрудняет refactoring (см. 03_TASK_v4.8.9.md §3, 10_KEY_DECISIONS.md §7).

  Теперь: bot.py при старте регистрирует свои функции в app_state:
      from app_state import register
      register(exit_night_mode=_exit_night_mode,
               enter_sanitary_day=_enter_sanitary_day,
               exit_sanitary_day=_exit_sanitary_day)
  А в bot_handlers.py / web_app.py — заменяем:
      # Было: from bot import _exit_night_mode
      # Стало:
      from app_state import get_exit_night_mode
      _exit_night_mode = get_exit_night_mode()

  Это чистый паттерн "service locator". Минус — статический анализ (IDE,
  mypy) не видит тип функции. Для нас это приемлемо: late import и так
  неявный, а функции стабильны (не менялись с v4.7.x).

Если bot.py ещё не зарегистрировал функцию (например, при импорте модуля
до старта bot) — get_*() поднимёт RuntimeError с понятным сообщением.
"""
from __future__ import annotations

from typing import Any, Callable, Coroutine

# ── Реестр ──────────────────────────────────────────────────────────────────
# Имя функции (str) → сама функция. Заполняется bot.py при старте.
_registry: dict[str, Callable[..., Any]] = {}


def register(**funcs: Callable[..., Any]) -> None:
    """Регистрирует функции в реестре. Вызывается из bot.py при старте.

    Пример:
        register(
            exit_night_mode=_exit_night_mode,
            enter_sanitary_day=_enter_sanitary_day,
            exit_sanitary_day=_exit_sanitary_day,
        )
    """
    _registry.update(funcs)


def _get(name: str) -> Callable[..., Any]:
    """Достаёт функцию из реестра. RuntimeError если не зарегистрирована."""
    fn = _registry.get(name)
    if fn is None:
        raise RuntimeError(
            f"app_state: function '{name}' is not registered. "
            "bot.py must call app_state.register(...) at startup before "
            "any module can use get_*() helpers."
        )
    return fn


# ── Type-safe getters ───────────────────────────────────────────────────────
# Каждый getter возвращает функцию с правильной сигнатурой. Если сигнатура
# bot.py-функции поменяется — нужно обновить и здесь.

# _exit_night_mode(cs: ChatSettings, allow_auto_enter: bool = True) -> None
def get_exit_night_mode() -> Callable[..., Coroutine[Any, Any, None]]:
    """Возвращает _exit_night_mode из bot.py."""
    return _get("exit_night_mode")


# _enter_sanitary_day(cs: ChatSettings) -> None
def get_enter_sanitary_day() -> Callable[..., Coroutine[Any, Any, None]]:
    """Возвращает _enter_sanitary_day из bot.py."""
    return _get("enter_sanitary_day")


# _exit_sanitary_day(cs: ChatSettings) -> None
def get_exit_sanitary_day() -> Callable[..., Coroutine[Any, Any, None]]:
    """Возвращает _exit_sanitary_day из bot.py."""
    return _get("exit_sanitary_day")
