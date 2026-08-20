"""
test_v490_decomposition.py — инварианты декомпозиции create_app() (Task 10).

Проверяет механику, на которой держится вынос роутов в web/:
  1. app.state.templates и app.state.bot проставлены;
  2. get_templates / get_bot достают их из request;
  3. get_bot возвращает None, когда create_app вызван без бота
     (тесты и часть роутов рассчитывают на 503, а не на падение);
  4. общее число роутов приложения не меняется при переносе.

Пункт 4 — страховка от потери роута при копировании. Считать нужно с
обходом _IncludedRouter: в Starlette 1.6 include_router не разворачивает
роуты в app.routes.
"""
from __future__ import annotations

import os
import sys
import unittest

from _paths import _P

sys.path.insert(0, _P())

os.environ.setdefault("BOT_TOKEN", "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw")
os.environ.setdefault("ADMIN_IDS", "111111111")
os.environ.setdefault("WEB_PASSWORD", "test_pw")
os.environ.setdefault("WEB_ALLOW_NO_SECRET", "1")

from starlette.routing import Route  # noqa: E402

import web_app  # noqa: E402
from web.deps import get_bot, get_templates  # noqa: E402

# Эталон: 54 роута после декомпозиции (47 из create_app + 7 вынесенных
# раньше) плюс /healthz, добавленный в v4.10.2 (Task 16).
_EXPECTED_ROUTES = 55


def _walk(routes):
    """Разворачивает вложенные роутеры.

    FastAPI 0.141 кладёт в app.routes объект _IncludedRouter, а сами роуты
    прячет в его original_router.routes. Без обхода счётчик покажет только
    роуты, объявленные внутри create_app, и будет уменьшаться с каждым
    вынесенным доменом.
    """
    for r in routes:
        if isinstance(r, Route):
            yield r
        elif hasattr(r, "original_router"):
            yield from _walk(r.original_router.routes)


def _route_pairs(app):
    return {
        (r.path, m)
        for r in _walk(app.routes)
        for m in (r.methods or ())
        if m != "HEAD"
    }


class _FakeRequest:
    """Минимальный объект с .app — провайдерам больше ничего не нужно."""

    def __init__(self, app):
        self.app = app


class TestAppState(unittest.TestCase):

    def test_templates_in_app_state(self):
        """create_app кладёт templates в app.state — роутеры берут его оттуда."""
        app = web_app.create_app()
        self.assertTrue(hasattr(app.state, "templates"))

    def test_get_templates_returns_state_object(self):
        """get_templates отдаёт тот же объект, что лежит в state.

        Важно, что именно тот же: на нём висит CSRF-обёртка над
        TemplateResponse, собранная в create_app.
        """
        app = web_app.create_app()
        self.assertIs(get_templates(_FakeRequest(app)), app.state.templates)

    def test_get_bot_returns_none_without_bot(self):
        """create_app() без бота → get_bot даёт None, а не падает.

        На это рассчитывают роуты, отвечающие 503 при bot is None, и вся
        сюита: она зовёт create_app() без аргументов.
        """
        app = web_app.create_app()
        self.assertIsNone(get_bot(_FakeRequest(app)))

    def test_get_bot_returns_passed_bot(self):
        """Переданный бот доезжает до провайдера."""
        sentinel = object()
        app = web_app.create_app(bot=sentinel)
        self.assertIs(get_bot(_FakeRequest(app)), sentinel)


class TestRouteInventory(unittest.TestCase):

    def test_route_count_unchanged(self):
        """Число роутов не меняется — ловит потерю роута при переносе."""
        app = web_app.create_app()
        self.assertEqual(len(_route_pairs(app)), _EXPECTED_ROUTES)

    def test_no_duplicate_routes(self):
        """Один и тот же (путь, метод) не зарегистрирован дважды.

        При копировании легко оставить роут и в create_app, и в новом
        модуле: FastAPI не ругается, просто первый выигрывает.
        """
        app = web_app.create_app()
        pairs = [
            (r.path, m)
            for r in _walk(app.routes)
            for m in (r.methods or ())
            if m != "HEAD"
        ]
        self.assertEqual(len(pairs), len(set(pairs)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
