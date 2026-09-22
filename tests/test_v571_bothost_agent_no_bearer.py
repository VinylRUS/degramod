"""
test_v571_bothost_agent_no_bearer.py — тесты v5.7.0 (drop-bearer в bothost_agent).

Что проверяет:

  T1:  _auth_headers() возвращает пустой словарь (нет Bearer).
  T2:  _auth_headers(token="anything") тоже возвращает {} — token игнорируется.
  T3:  В _request() не появляется заголовок Authorization.
  T4:  В _request() появляется заголовок X-Bot-ID если задан BOT_ID.
  T5:  Без BOT_ID в env — заголовков авторизации нет вовсе.
  T6:  diagnose_tokens() не делает сетевых запросов, только читает env.
  T7:  diagnose_tokens() возвращает "задан (не используется для agent API)".
  T8:  Файл bothost_agent.py не содержит "Bearer" в исходниках (кроме
       документации и исторических комментариев).
  T9:  Обратная совместимость: функции _auth_headers и diagnose_tokens
       остались (не удалены), сигнатуры не изменились.

Контекст: на проде 22.09.2026 агент Bothost на n19.bothost.ru отвечал
`Unauthorized: invalid token`, потому что код слал Bearer с Telegram-токеном,
а Bothost кладёт в BOT_API_TOKEN тот же самый Telegram-токен (см. документацию
bothost.ru/llms-full.txt: «API_TOKEN — альтернативное имя для BOT_TOKEN»).
Документация Agent API явно говорит: «Без Bearer-токена: используется только
X-Bot-ID». Решение — убрать Bearer из всех запросов, оставить только X-Bot-ID.

Запуск:
    uv run python tools/run_tests.py -k v571_bothost
    uv run python tests/test_v571_bothost_agent_no_bearer.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

from _paths import ROOT

sys.path.insert(0, str(ROOT))


class TestAuthHeadersReturnsNoBearer(unittest.TestCase):
    """_auth_headers() возвращает пустой словарь — Bearer не шлётся."""

    def setUp(self):
        # Чистим env от токенов — чтобы убедиться, что функция не берёт их.
        for key in ("BOT_API_TOKEN", "API_TOKEN", "BOTHOST_API_TOKEN", "AGENT_TOKEN",
                    "BOT_ID", "BOTHOST_AGENT_URL"):
            os.environ.pop(key, None)

    def test_t1_no_env_returns_empty(self):
        """T1: без env-токенов — пустой словарь."""
        import bothost_agent
        self.assertEqual(bothost_agent._auth_headers(), {})

    def test_t2_explicit_token_ignored(self):
        """T2: даже с явным токеном — пустой словарь (token игнорируется)."""
        import bothost_agent
        # Передаём токен явно — функция должна его проигнорировать.
        result = bothost_agent._auth_headers(token="some-random-token-12345")
        self.assertEqual(result, {})
        self.assertNotIn("Authorization", result)
        self.assertNotIn("Bearer", str(result))

    def test_t2b_env_token_ignored(self):
        """T2b: даже если в env лежит BOT_API_TOKEN — Bearer не возвращается."""
        os.environ["BOT_API_TOKEN"] = "telegram-token:abc"
        import bothost_agent
        result = bothost_agent._auth_headers()
        self.assertEqual(result, {})
        self.assertNotIn("Authorization", result)


class TestRequestSendsXBotIdOnly(unittest.TestCase):
    """_request() шлёт X-Bot-ID, не шлёт Authorization."""

    def setUp(self):
        for key in ("BOT_API_TOKEN", "API_TOKEN", "BOTHOST_API_TOKEN", "AGENT_TOKEN"):
            os.environ.pop(key, None)
        # Включаем агент через env, чтобы resolve_agent_url не вернул None.
        os.environ["BOTHOST_AGENT_URL"] = "http://fake-agent.test"
        os.environ["BOT_ID"] = "bot_test_123"

    def tearDown(self):
        for key in ("BOTHOST_AGENT_URL", "BOT_ID"):
            os.environ.pop(key, None)
        # Сброс кеша bothost_agent (он кеширует _cached_url).
        import bothost_agent
        bothost_agent.reset_cache()

    def test_t3_no_authorization_header(self):
        """T3: в _request() не появляется заголовок Authorization."""
        import bothost_agent

        captured_headers: dict = {}

        class FakeResponse:
            status = 200
            async def json(self):
                return {"ok": True, "data": "test"}
            async def text(self):
                return ""
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass

        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def request(self, method, url, headers=None, **kwargs):
                captured_headers.update(headers or {})
                # Возвращаем AsyncMock-контекст-менеджер
                fake_resp = FakeResponse()
                fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
                fake_resp.__aexit__ = AsyncMock(return_value=None)
                return fake_resp

        with patch("aiohttp.ClientSession", FakeSession):
            asyncio.run(bothost_agent._request("GET", "/api/bots/test/stats"))

        # Bearer не должен появиться.
        self.assertNotIn("Authorization", captured_headers,
                         f"Authorization header не должен слаться: {captured_headers}")
        # Bearer не должен встречаться ни в каком значении.
        for key, value in captured_headers.items():
            self.assertNotIn("Bearer", str(value),
                              f"Bearer найден в заголовке {key}: {value}")

    def test_t4_x_bot_id_present(self):
        """T4: X-Bot-ID присутствует если задан BOT_ID."""
        import bothost_agent

        captured_headers: dict = {}

        class FakeResponse:
            status = 200
            async def json(self):
                return {"ok": True, "data": "test"}
            async def text(self):
                return ""
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass

        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def request(self, method, url, headers=None, **kwargs):
                captured_headers.update(headers or {})
                fake_resp = FakeResponse()
                fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
                fake_resp.__aexit__ = AsyncMock(return_value=None)
                return fake_resp

        with patch("aiohttp.ClientSession", FakeSession):
            asyncio.run(bothost_agent._request("GET", "/api/bots/test/stats"))

        self.assertIn("X-Bot-ID", captured_headers)
        self.assertEqual(captured_headers["X-Bot-ID"], "bot_test_123")

    def test_t5_no_bot_id_no_headers(self):
        """T5: без BOT_ID в env — никаких заголовков авторизации."""
        os.environ.pop("BOT_ID", None)
        import bothost_agent

        captured_headers: dict = {}

        class FakeResponse:
            status = 200
            async def json(self):
                return {"ok": True, "data": "test"}
            async def text(self):
                return ""
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass

        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def request(self, method, url, headers=None, **kwargs):
                captured_headers.update(headers or {})
                fake_resp = FakeResponse()
                fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
                fake_resp.__aexit__ = AsyncMock(return_value=None)
                return fake_resp

        with patch("aiohttp.ClientSession", FakeSession):
            asyncio.run(bothost_agent._request("GET", "/api/bots/test/stats"))

        self.assertNotIn("X-Bot-ID", captured_headers)
        self.assertNotIn("Authorization", captured_headers)


class TestDiagnoseTokensNoNetwork(unittest.TestCase):
    """diagnose_tokens() не делает сетевых запросов, только читает env."""

    def setUp(self):
        for key in ("BOT_API_TOKEN", "API_TOKEN", "BOTHOST_API_TOKEN", "AGENT_TOKEN"):
            os.environ.pop(key, None)

    def test_t6_no_network_calls(self):
        """T6: diagnose_tokens() не дёргает get_stats → не делает сетевых запросов."""
        import bothost_agent

        with patch.object(bothost_agent, "get_stats", new_callable=AsyncMock) as mock:
            result = asyncio.run(bothost_agent.diagnose_tokens())
            mock.assert_not_called()
            self.assertEqual(result, [])

    def test_t6b_returns_for_each_token_in_env(self):
        """T6b: для каждой переменной в env возвращает запись."""
        os.environ["BOT_API_TOKEN"] = "telegram:abc"
        os.environ["API_TOKEN"] = "telegram:abc2"
        import bothost_agent

        result = asyncio.run(bothost_agent.diagnose_tokens())
        self.assertEqual(len(result), 2)
        names = [name for name, _ in result]
        self.assertIn("BOT_API_TOKEN", names)
        self.assertIn("API_TOKEN", names)

    def test_t7_message_does_not_say_accepted(self):
        """T7: вердикт не говорит «принят агентом» — Bearer больше не шлём."""
        os.environ["BOT_API_TOKEN"] = "telegram:abc"
        import bothost_agent

        result = asyncio.run(bothost_agent.diagnose_tokens())
        for name, verdict in result:
            self.assertNotIn("принят", verdict.lower(),
                             f"Вердикт для {name} не должен говорить 'принят': {verdict}")
            self.assertIn("не используется", verdict.lower())


class TestFileDoesNotHaveBearerInCode(unittest.TestCase):
    """Sanity check: Bearer не встречается в исходниках кроме как в комментариях."""

    def test_t8_no_bearer_in_auth_headers_body(self):
        """T8: Bearer не встречается в теле _auth_headers."""
        content = (ROOT / "bothost_agent.py").read_text(encoding="utf-8")
        # Извлекаем тело функции _auth_headers
        import re
        m = re.search(r"def _auth_headers[^:]*:.*?return\s+(\{[^}]*\})", content, re.S)
        self.assertIsNotNone(m, "_auth_headers должна иметь return-строку")
        body = m.group(1)
        self.assertNotIn("Bearer", body,
                         f"Bearer найден в теле _auth_headers: {body!r}")

    def test_t9_auth_headers_callable(self):
        """T9: обратная совместимость — _auth_headers остаётся callable."""
        import bothost_agent
        self.assertTrue(callable(bothost_agent._auth_headers))
        # Сигнатура осталась — принимает опциональный token.
        result = bothost_agent._auth_headers(token=None)
        self.assertEqual(result, {})

    def test_t9b_diagnose_tokens_callable(self):
        """T9b: diagnose_tokens остаётся callable."""
        import bothost_agent
        self.assertTrue(callable(bothost_agent.diagnose_tokens))


if __name__ == "__main__":
    unittest.main(verbosity=2)
