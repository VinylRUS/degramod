"""
test_v510_bothost_agent.py — обёртка над Bothost Agent API (v5.1.0).

Агент доступен только изнутри Docker-сети Bothost: локально `agent:8000` не
резолвится, внешние адреса отказывают в соединении. Поэтому здесь мокается
HTTP-клиент, а проверяется поведение обёртки — что она возвращает, когда
агента нет, когда он отвечает мусором и когда таймаутит.

Главное требование: обёртка НЕ бросает исключений. Её зовут веб-роуты (там
исключение превратится в 500 вместо честного «агент недоступен») и фоновая
задача (там оно убьёт наблюдение ровно тогда, когда оно нужнее всего).
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from _paths import _P

os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("ADMIN_IDS", "1")

sys.path.insert(0, _P())

import bothost_agent  # noqa: E402
import aiohttp  # noqa: E402


class _AgentCase(unittest.TestCase):
    def setUp(self):
        bothost_agent.reset_cache()
        self._prev_bot_id = os.environ.get("BOT_ID")
        self._prev_url = os.environ.get("BOTHOST_AGENT_URL")
        os.environ["BOT_ID"] = "bot_test_123"
        # v5.1.0 (fix-1): по умолчанию агент «недоступен» без обращения к сети.
        # reset_cache() выше заставляет resolve_agent_url() каждый раз заново
        # проверять agent:8000, а это реальный DNS-резолв: в изоляции тест шёл
        # 0.30s, в составе файла — 3.8s, то есть время зависело от резолвера,
        # а не от кода. Тесты, которым нужно именно поведение проверки, ставят
        # свой patch поверх этого — внутренний контекст-менеджер побеждает.
        _patcher = patch.object(bothost_agent, "_can_connect", new=AsyncMock(return_value=False))
        _patcher.start()
        self.addCleanup(_patcher.stop)

    def tearDown(self):
        for key, prev in (("BOT_ID", self._prev_bot_id),
                          ("BOTHOST_AGENT_URL", self._prev_url)):
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
        bothost_agent.reset_cache()


class TestUrlResolution(_AgentCase):
    """Адрес агента: внутренний → переменная → дефолт."""

    def test_prefers_internal_docker_address(self):
        """Внутри сети Bothost агент доступен как agent:8000."""
        with patch.object(bothost_agent, "_can_connect", new=AsyncMock(return_value=True)):
            self.assertEqual(
                asyncio.run(bothost_agent.resolve_agent_url()), "http://agent:8000",
            )

    def test_falls_back_to_env_variable(self):
        os.environ["BOTHOST_AGENT_URL"] = "http://msk1.bothost.ru"
        with patch.object(bothost_agent, "_can_connect", new=AsyncMock(return_value=False)):
            self.assertEqual(
                asyncio.run(bothost_agent.resolve_agent_url()), "http://msk1.bothost.ru",
            )

    def test_falls_back_to_public_default(self):
        os.environ.pop("BOTHOST_AGENT_URL", None)
        with patch.object(bothost_agent, "_can_connect", new=AsyncMock(return_value=False)):
            self.assertEqual(
                asyncio.run(bothost_agent.resolve_agent_url()), "http://agent.bothost.ru",
            )

    def test_result_is_cached(self):
        """Адрес не меняется в пределах жизни контейнера — socket дёргаем раз."""
        probe = AsyncMock(return_value=True)
        with patch.object(bothost_agent, "_can_connect", new=probe):
            asyncio.run(bothost_agent.resolve_agent_url())
            asyncio.run(bothost_agent.resolve_agent_url())
        self.assertEqual(probe.call_count, 1)

    def test_connect_check_does_not_block_event_loop(self):
        """Проверка доступности не морозит event loop.

        Регресс: синхронный socket.create_connection блокировал общий loop
        бота и панели на время DNS-резолвинга — несколько секунд, в течение
        которых бот не отвечал ни в одном чате.
        """
        import inspect
        self.assertTrue(
            inspect.iscoroutinefunction(bothost_agent._can_connect),
            "_can_connect должна быть асинхронной, иначе блокирует event loop",
        )


class TestMissingBotId(_AgentCase):
    """Без BOT_ID в сеть не ходим вовсе."""

    def test_stats_refuses_without_bot_id(self):
        os.environ.pop("BOT_ID", None)
        result = asyncio.run(bothost_agent.get_stats())
        self.assertFalse(result.ok)
        self.assertIn("BOT_ID", result.error)

    def test_restart_refuses_without_bot_id(self):
        os.environ.pop("BOT_ID", None)
        result = asyncio.run(bothost_agent.restart_self())
        self.assertFalse(result.ok)


class TestFailureHandling(_AgentCase):
    """Отказы превращаются в AgentResult, а не в исключения."""

    def _mock_session(self, *, json_value=None, json_raises=None,
                      text_value="", status=200, request_raises=None):
        """Подменяет aiohttp.ClientSession одним ответом."""
        response = MagicMock()
        response.status = status
        response.json = AsyncMock(
            side_effect=json_raises, return_value=json_value,
        )
        response.text = AsyncMock(return_value=text_value)
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        if request_raises is not None:
            session.request = MagicMock(side_effect=request_raises)
        else:
            session.request = MagicMock(return_value=response)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        return session

    def test_unreachable_agent_returns_error(self):
        """Нет агента — честный ok=False, а не исключение наружу."""
        session = self._mock_session(
            request_raises=aiohttp.ClientConnectorError(MagicMock(), OSError()),
        )
        with patch.object(bothost_agent.aiohttp, "ClientSession", return_value=session):
            result = asyncio.run(bothost_agent.get_stats())
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)

    def test_timeout_returns_error(self):
        session = self._mock_session(request_raises=TimeoutError())
        with patch.object(bothost_agent.aiohttp, "ClientSession", return_value=session):
            result = asyncio.run(bothost_agent.get_stats())
        self.assertFalse(result.ok)
        self.assertIn("таймаут", result.error)

    def test_non_json_response_returns_error(self):
        """HTML вместо JSON — типичный ответ прокси, а не агента."""
        session = self._mock_session(
            json_raises=ValueError("not json"), text_value="<html>502</html>", status=502,
        )
        with patch.object(bothost_agent.aiohttp, "ClientSession", return_value=session):
            result = asyncio.run(bothost_agent.get_stats())
        self.assertFalse(result.ok)
        self.assertIn("не JSON", result.error)

    def test_agent_says_not_ok(self):
        """Агент ответил ok=false — причину показываем как есть."""
        session = self._mock_session(
            json_value={"ok": False, "msg": "Контейнер не найден"},
        )
        with patch.object(bothost_agent.aiohttp, "ClientSession", return_value=session):
            result = asyncio.run(bothost_agent.get_stats())
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "Контейнер не найден")

    def test_successful_response_carries_data(self):
        session = self._mock_session(
            json_value={"ok": True, "stats": {"cpu_percent": 15.5}},
        )
        with patch.object(bothost_agent.aiohttp, "ClientSession", return_value=session):
            result = asyncio.run(bothost_agent.get_stats())
        self.assertTrue(result.ok)
        self.assertEqual(result.data["stats"]["cpu_percent"], 15.5)

    def test_restart_sends_bot_id_header(self):
        """X-Bot-ID обязателен по документации."""
        session = self._mock_session(json_value={"ok": True, "message": "перезапущен"})
        with patch.object(bothost_agent.aiohttp, "ClientSession", return_value=session):
            asyncio.run(bothost_agent.restart_self())
        kwargs = session.request.call_args.kwargs
        self.assertEqual(kwargs["headers"]["X-Bot-ID"], "bot_test_123")


class TestSettingsAgentBlock(unittest.TestCase):
    """Страница Settings показывает состояние агента и не падает без него.

    v5.1.0 (fix-1): раньше эти тесты звали `_agent_info()` без единого мока —
    внутри это реальный `bothost_agent.probe()` → `resolve_agent_url()` →
    TCP-проверка `agent:8000` и настоящий HTTP-запрос. На CI/локально агент
    недоступен, но тест всё равно уходил в сеть и ждал таймаут (замерено
    ревьюером: 6.25s). Теперь `probe`/`resolve_agent_url` мокаются
    асинхронными заглушками — `_agent_info()` делает `import bothost_agent`
    внутри функции, поэтому патчить нужно атрибуты самого модуля
    `bothost_agent`, а не то, что импортировано в `web.admin_settings`.
    """

    def setUp(self):
        os.environ["WEB_PASSWORD"] = "test-pwd"
        os.environ["WEB_ALLOW_NO_SECRET"] = "1"
        self._prev_bot_id = os.environ.get("BOT_ID")
        os.environ["BOT_ID"] = "bot_test_456"

    def tearDown(self):
        if self._prev_bot_id is None:
            os.environ.pop("BOT_ID", None)
        else:
            os.environ["BOT_ID"] = self._prev_bot_id

    def test_agent_info_present_in_context(self):
        """Хелпер отдаёт словарь с конкретными значениями из мока, не просто с ключами.

        v5.1.0 (fix-1): раньше тест проверял только `assertIn(key, info)` —
        такая проверка не ловит регресс «забыли await перед
        resolve_agent_url()»: без await в `url` оказался бы объект корутины,
        а `assertIn` всё равно был бы доволен. Теперь проверяем ТИП (`str`)
        и ЗНАЧЕНИЕ (равенство адресу, подставленному мокой) — если await
        пропадёт, `info["url"]` станет корутиной, `assertIsInstance` упадёт.
        """
        import web.admin_settings as admin_settings

        mock_url = "http://mock-agent:9999"
        mock_result = bothost_agent.AgentResult(
            ok=True, data={"stats": {"cpu_percent": 42.0}}, error=None,
        )
        with patch.object(bothost_agent, "probe", new=AsyncMock(return_value=mock_result)), \
                patch.object(bothost_agent, "resolve_agent_url", new=AsyncMock(return_value=mock_url)):
            info = asyncio.run(admin_settings._agent_info())

        self.assertIsInstance(info["url"], str)
        self.assertEqual(info["url"], mock_url)
        self.assertEqual(info["bot_id"], "bot_test_456")
        self.assertTrue(info["available"])
        self.assertIsNone(info["error"])
        self.assertEqual(info["raw"], {"stats": {"cpu_percent": 42.0}})

    def test_unavailable_agent_does_not_raise(self):
        """Агента нет — это состояние, а не ошибка страницы."""
        import web.admin_settings as admin_settings

        mock_result = bothost_agent.AgentResult(
            ok=False, error="агент недоступен: ConnectionError",
        )
        with patch.object(bothost_agent, "probe", new=AsyncMock(return_value=mock_result)), \
                patch.object(bothost_agent, "resolve_agent_url",
                             new=AsyncMock(return_value="http://agent.bothost.ru")):
            info = asyncio.run(admin_settings._agent_info())

        self.assertFalse(info["available"])
        self.assertEqual(info["error"], "агент недоступен: ConnectionError")


class TestSettingsAgentPageRender(unittest.TestCase):
    """Блок диагностики агента реально попадает в отрендеренный HTML страницы.

    v5.1.0 (fix-1): до этого блок в шаблоне не проверялся ничем —
    `test_v486_settings_render.py` использует print вместо assert и про
    Agent-блок не знает вовсе. Логин настоящей su-кукой в TestClient не
    работает (значение куки с запятыми httpx квотирует иначе, чем ожидает
    require_auth) — обходим авторизацию через dependency_overrides.
    """

    def setUp(self):
        os.environ["WEB_PASSWORD"] = "test-pwd"
        os.environ["WEB_ALLOW_NO_SECRET"] = "1"

    def test_agent_block_rendered_on_settings_page(self):
        from fastapi.testclient import TestClient

        import web_app
        from web_app import AuthUser, require_auth, require_su

        mock_url = "http://mock-agent:9999"
        mock_result = bothost_agent.AgentResult(
            ok=True, data={"stats": {"cpu_percent": 7.0}}, error=None,
        )
        fake_user = AuthUser(username="su_test", is_su=True, role="su")

        app = web_app.create_app(bot=None)
        app.dependency_overrides[require_su] = lambda: fake_user
        app.dependency_overrides[require_auth] = lambda: fake_user
        client = TestClient(app)

        with patch.object(bothost_agent, "probe", new=AsyncMock(return_value=mock_result)), \
                patch.object(bothost_agent, "resolve_agent_url", new=AsyncMock(return_value=mock_url)):
            r = client.get("/admin/settings", follow_redirects=False)

        self.assertEqual(r.status_code, 200)
        self.assertIn('id="agent"', r.text)
        self.assertIn(mock_url, r.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
