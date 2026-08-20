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
        with patch.object(bothost_agent, "_can_connect", return_value=True):
            self.assertEqual(bothost_agent.resolve_agent_url(), "http://agent:8000")

    def test_falls_back_to_env_variable(self):
        os.environ["BOTHOST_AGENT_URL"] = "http://msk1.bothost.ru"
        with patch.object(bothost_agent, "_can_connect", return_value=False):
            self.assertEqual(
                bothost_agent.resolve_agent_url(), "http://msk1.bothost.ru",
            )

    def test_falls_back_to_public_default(self):
        os.environ.pop("BOTHOST_AGENT_URL", None)
        with patch.object(bothost_agent, "_can_connect", return_value=False):
            self.assertEqual(
                bothost_agent.resolve_agent_url(), "http://agent.bothost.ru",
            )

    def test_result_is_cached(self):
        """Адрес не меняется в пределах жизни контейнера — socket дёргаем раз."""
        with patch.object(bothost_agent, "_can_connect", return_value=True) as probe:
            bothost_agent.resolve_agent_url()
            bothost_agent.resolve_agent_url()
        self.assertEqual(probe.call_count, 1)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
