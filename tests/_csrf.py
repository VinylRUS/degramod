"""Шим, дополняющий POST-запросы тестов csrf-токеном.

Зачем. Тринадцать файлов сюиты написаны до v4.8.8, когда CSRF-защиты ещё не
было: они постят формы без `csrf_token` и получают 403. В шаблонах сейчас
стоит `{{ csrf_field() }}`, то есть настоящий браузер токен из формы
отправляет — эти клиенты просто не умеют его подставлять.

Шим воспроизводит поведение браузера: берёт имя пользователя из куки сессии и
кладёт в тело формы тот же токен, который отрендерил бы шаблон. Серверную
проверку он не трогает и не ослабляет — если бы `require_csrf_*` пропал с
роута, запрос всё равно прошёл бы, но это ловится отдельно: `test_v488_verify_csrf`
сканирует web_app.py и падает на любом POST-роуте без CSRF-зависимости.

Токен не подставляется, если:
  - в теле он уже есть (тест проверяет CSRF осознанно);
  - куки сессии нет (аноним — ему токен и не положен).
"""
from __future__ import annotations

import json
import re
from contextlib import contextmanager

# Тесты, для которых отсутствие токена — суть проверки, отключают шим:
#
#     with _csrf.disabled():
#         r = client.post("/admin/users/create", data={...})   # ждём 403
_ENABLED = True


@contextmanager
def disabled():
    """Внутри блока шим не подставляет токен."""
    global _ENABLED
    prev = _ENABLED
    _ENABLED = False
    try:
        yield
    finally:
        _ENABLED = prev

_COOKIE = "sl_session"
# Кука = <json payload>:<hmac>. Имя нужно только из payload.
_USER_RE = re.compile(r'"u"\s*:\s*"([^"]+)"')


def _username_from_cookies(cookies) -> str | None:
    """Достаёт username из куки сессии клиента."""
    try:
        raw = cookies.get(_COOKIE)
    except Exception:
        raw = None
    if not raw:
        return None
    # httpx хранит куку в закавыченном виде с \054 вместо запятых.
    raw = raw.strip('"').replace("\\054", ",").replace('\\"', '"')
    payload = raw.rsplit(":", 1)[0]
    try:
        return json.loads(payload).get("u")
    except (ValueError, AttributeError):
        m = _USER_RE.search(payload)
        return m.group(1) if m else None


def token_for(username: str) -> str | None:
    """Тот же токен, что подставил бы csrf_field() в шаблоне."""
    import sys

    web_app = sys.modules.get("web_app")
    if web_app is None:
        return None
    fn = getattr(web_app, "_csrf_token_for_username", None)
    return fn(username) if fn else None


def inject(data, cookies):
    """Дополняет тело формы csrf-токеном. Возвращает новое тело.

    `data=None` — POST без параметров: браузер и в этом случае отправил бы
    скрытое поле формы, поэтому тело создаётся из одного токена.
    """
    if not _ENABLED:
        return data
    if data is None:
        data = {}
    if not isinstance(data, dict) or "csrf_token" in data:
        return data
    username = _username_from_cookies(cookies)
    if not username:
        # Аноним: тело было пустым — пустым и оставляем, чтобы не подменять
        # отсутствие данных на пустой словарь.
        return data or None
    token = token_for(username)
    if not token:
        return data or None
    return {**data, "csrf_token": token}
