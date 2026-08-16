"""Общее окружение для сюиты.

Файлы тестов пришли из внешней песочницы, где каждый сам поднимал sys.path и
переменные окружения. Пути централизованы в `_paths.py`, env — здесь.

conftest импортируется pytest'ом до любого тестового модуля, поэтому
переменные выставлены к моменту, когда тест на уровне модуля делает
`import db` / `import web_app`.

При запуске файла напрямую (`python tests/test_x.py`) conftest не подхватится,
но тесты выставляют свой env сами через `os.environ.setdefault` — этот модуль
только гарантирует одинаковый результат под pytest.
"""
from __future__ import annotations

import os
import sys

from _paths import ROOT

# Корень репозитория в sys.path — чтобы `import db`, `import bot_handlers`,
# `import web_app` находили модули из корня, а не из site-packages.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Дефолты окружения для файлов, которые не поднимают его сами.
#
# Тонкость: conftest импортируется раньше тестового модуля, поэтому его
# setdefault выигрывает у setdefault внутри теста. Двадцать файлов задают
# WEB_PASSWORD присваиванием и не страдают, но семь используют setdefault —
# для них побеждает значение отсюда. Убирать дефолты из-за этого пробовали:
# зелёных файлов стало 28 вместо 32, ни один не починился. Поэтому набор
# оставлен, а расхождения по паролю чинятся в самих файлах.
#
# Не добавляй сюда переменную, значение которой хоть один тест проверяет.
_DEFAULTS = {
    # db.py падает с RuntimeError без токена, значение нигде не проверяется.
    "BOT_TOKEN": "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "ADMIN_IDS": "123456789",
    "WEB_PASSWORD": "test-su-password-12345",
    "SESSION_SECRET": "test-session-secret",
    # Без этого web_app требует SESSION_SECRET из env и шумит в лог.
    "WEB_ALLOW_NO_SECRET": "1",
    # Тесты ходят по http, а не https — иначе кука не ставится и логин не проходит.
    "WEB_COOKIE_SECURE": "0",
    # _client_ip доверяет X-Forwarded-For только от перечисленных прокси (v4.8.8).
    # Пусто = не доверять никому, как и должно быть по умолчанию.
    "TRUSTED_PROXIES": "",
    # Alembic на проде выключен (см. CLAUDE.md), сюита идёт тем же путём —
    # через идемпотентный init_db().
    "DB_USE_LEGACY_MIGRATIONS": "1",
}

for _k, _v in _DEFAULTS.items():
    os.environ.setdefault(_k, _v)


# ── CSRF-шим для легаси-файлов ──────────────────────────────────────────────
# Тринадцать файлов сюиты старше v4.8.8 и постят формы без csrf_token. Патчим
# POST у httpx-клиентов, чтобы они вели себя как браузер: подставляли токен,
# который отрендерил бы csrf_field() в шаблоне. Подробности и границы —
# в docstring _csrf.py. Когда сюиту перепишут на общие фикстуры, шим уйдёт.
def _install_csrf_shim() -> None:
    try:
        import httpx
    except ImportError:  # pragma: no cover
        return

    from _csrf import inject

    # Тело формы может быть не передано вовсе (POST без параметров) — браузер
    # и в этом случае отправит скрытое поле csrf_token. А вот json/content/files
    # трогать нельзя: это не форма, и data туда не добавить.
    #
    # Проверяется значение, а не наличие ключа: TestClient из Starlette
    # объявляет content/data/files/json явными параметрами и передаёт их в
    # httpx.Client.post всегда, в том числе как None. По наличию ключа шим
    # отключался бы на каждом запросе через TestClient.
    _BODY_KWARGS = ("json", "content", "files")

    def _needs_form(kwargs: dict) -> bool:
        return not any(kwargs.get(k) is not None for k in _BODY_KWARGS)

    def _wrap(original, is_async: bool):
        if getattr(original, "_csrf_shim", False):
            return original

        if is_async:
            async def post(self, url, *args, **kwargs):
                if _needs_form(kwargs):
                    kwargs["data"] = inject(kwargs.get("data"), self.cookies)
                return await original(self, url, *args, **kwargs)
        else:
            def post(self, url, *args, **kwargs):
                if _needs_form(kwargs):
                    kwargs["data"] = inject(kwargs.get("data"), self.cookies)
                return original(self, url, *args, **kwargs)

        post._csrf_shim = True
        return post

    httpx.Client.post = _wrap(httpx.Client.post, is_async=False)
    httpx.AsyncClient.post = _wrap(httpx.AsyncClient.post, is_async=True)


_install_csrf_shim()
