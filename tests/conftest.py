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

import atexit
import glob
import os
import sys
import tempfile

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
    # Без этого db.py берёт прод-дефолт /app/data/shadow_logs.db: локально он
    # даёт "unable to open database file", а на машине с боевым каталогом
    # прогон писал бы в рабочую базу. Файл — свой на каждый процесс, а раннер
    # запускает файлы по одному, так что тесты не мешают друг другу.
    "DB_PATH": os.path.join(
        tempfile.gettempdir(), f"degramod_tests_{os.getpid()}.db"
    ),
}

for _k, _v in _DEFAULTS.items():
    os.environ.setdefault(_k, _v)


@atexit.register
def _cleanup_test_db() -> None:
    """Убирает временную БД процесса вместе с WAL-спутниками."""
    base = _DEFAULTS["DB_PATH"]
    if os.environ.get("DB_PATH") != base:
        return  # файл задал свой путь — не наше дело
    for path in glob.glob(base + "*"):
        try:
            os.unlink(path)
        except OSError:
            pass


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

        # Кука сессии живёт либо в jar клиента (обычный логин через POST /login),
        # либо передаётся на конкретный запрос параметром cookies= — так делают
        # тесты, которые кладут заранее выписанный токен. Второй источник
        # приоритетнее: он и уйдёт на сервер.
        def _cookies_for(self, kwargs):
            return kwargs.get("cookies") or self.cookies

        if is_async:
            async def post(self, url, *args, **kwargs):
                if _needs_form(kwargs):
                    kwargs["data"] = inject(kwargs.get("data"), _cookies_for(self, kwargs))
                return await original(self, url, *args, **kwargs)
        else:
            def post(self, url, *args, **kwargs):
                if _needs_form(kwargs):
                    kwargs["data"] = inject(kwargs.get("data"), _cookies_for(self, kwargs))
                return original(self, url, *args, **kwargs)

        post._csrf_shim = True
        return post

    httpx.Client.post = _wrap(httpx.Client.post, is_async=False)
    httpx.AsyncClient.post = _wrap(httpx.AsyncClient.post, is_async=True)


_install_csrf_shim()
