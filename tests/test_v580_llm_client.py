"""
test_v580_llm_client.py — тесты модуля llm_client (GigaChat, v5.8.0).

Что проверяет:

  T1:  LLMResult по умолчанию: ok=False, text=None, error=None.
  T2:  is_available() False если _cached_settings is None.
  T3:  complete() fast-path к LLMResult(ok=False) если нет ключа.
  T4:  complete() fast-path к LLMResult(ok=False) если is_active=False.
  T5:  complete() на 200 OK от GigaChat — ok=True, text берётся из choices[0].message.content.
  T6:  complete() на 401 — ok=False, ошибка упоминает unauthorized.
  T7:  complete() на 429 — ретраится, затем ok=False.
  T8:  complete() на timeout — ok=False, error упоминает таймаут.
  T9:  complete() на некорректном JSON — ok=False с внятной причиной.
  T10: complete() не бросает исключений при сетевой ошибке (aiohttp.ClientError).
  T11: Кеш: 2 идентичных вызова = 1 сетевой запрос (cached=True на втором).
  T12: reset_cache() сбрасывает кеш.
  T13: expand_reason() строит messages с system+user, не падает при None
       offender_text/reply_context_text.
  T14: test_connection() шлёт короткий промпт и возвращает ok=True при успехе.
  T15: test_connection() на 401 возвращает ok=False с упоминанием unauthorized.
  T16: _extract_content / _extract_usage корректно парсят тело GigaChat.

Запуск:
    uv run python tools/run_tests.py -k v580_llm_client
    uv run python tests/test_v580_llm_client.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

from _paths import ROOT

sys.path.insert(0, str(ROOT))


def _reset_llm_client_state():
    """Полный сброс состояния llm_client между тестами."""
    import llm_client
    llm_client.reset_cache()
    llm_client.settings_changed()  # сбросит _cached_settings → None


class _FakeResponse:
    """Эмуляция aiohttp-ответа для тестов."""

    def __init__(self, status: int, body: dict | str):
        self.status = status
        self._body = body

    async def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body

    async def text(self):
        if isinstance(self._body, str):
            return self._body
        return json.dumps(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class _FakeSession:
    """Эмуляция aiohttp.ClientSession. Перехватывает все запросы,
    кладёт их в self.captured для проверок.
    """

    def __init__(self, *args, **kwargs):
        self.captured: list[dict] = []
        self._responses: list[_FakeResponse] = []

    def add_response(self, response: _FakeResponse):
        self._responses.append(response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def post(self, url, json=None, headers=None, **kwargs):
        self.captured.append({
            "method": "POST", "url": url, "json": json, "headers": headers,
        })
        # Берём следующий заготовленный ответ, или дефолтный 200.
        if self._responses:
            return self._responses.pop(0)
        return _FakeResponse(200, {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        })


def _patch_settings(api_key="test-key-123", is_active=True, model_name=None, timeout=30):
    """Мокает _get_settings() на возврат LlmSettings с заданными параметрами."""
    import llm_client
    settings = llm_client.LlmSettings(
        id=1,
        is_active=is_active,
        api_key=api_key,
        model_name=model_name or "GigaChat-Pro",
        timeout_seconds=timeout,
        updated_at=None,
    )
    # Лазим в приватный кеш напрямую — это тест, не API.
    llm_client._cached_settings = settings
    return settings


class TestLLMResultDefaults(unittest.TestCase):
    """T1: LLMResult по умолчанию корректен."""

    def test_t1_defaults(self):
        import llm_client
        r = llm_client.LLMResult(ok=False)
        self.assertFalse(r.ok)
        self.assertIsNone(r.text)
        self.assertIsNone(r.error)
        self.assertFalse(r.cached)
        self.assertEqual(r.elapsed_ms, 0)
        self.assertIsNone(r.usage)


class TestIsAvailable(unittest.TestCase):
    """T2: is_available() — быстрый синхронный чек рубильника."""

    def setUp(self):
        _reset_llm_client_state()

    def tearDown(self):
        _reset_llm_client_state()

    def test_t2_no_settings(self):
        import llm_client
        self.assertFalse(llm_client.is_available())

    def test_t2b_active_settings(self):
        import llm_client
        _patch_settings(is_active=True, api_key="abc")
        self.assertTrue(llm_client.is_available())

    def test_t2c_inactive_settings(self):
        import llm_client
        _patch_settings(is_active=False, api_key="abc")
        self.assertFalse(llm_client.is_available())

    def test_t2d_no_key_active(self):
        import llm_client
        _patch_settings(is_active=True, api_key="")
        # is_available проверяет is_active, не ключ. Ключ проверяет complete().
        # Так задумано: is_available — это "рубильник", а не "можно ли сейчас
        # сделать запрос".
        self.assertTrue(llm_client.is_available())


class TestCompleteFastPathDisabled(unittest.TestCase):
    """T3, T4: complete() fast-path если LLM выключена."""

    def setUp(self):
        _reset_llm_client_state()

    def tearDown(self):
        _reset_llm_client_state()

    def test_t3_no_settings_no_network(self):
        """T3: без настроек — ok=False, без сетевого запроса."""
        import llm_client

        with patch("aiohttp.ClientSession", _FakeSession) as mock_session_cls:
            result = asyncio.run(llm_client.complete(
                [{"role": "user", "content": "test"}]
            ))
            self.assertFalse(result.ok)
            self.assertIn("выключена", result.error.lower())
            # Сетевого запроса быть не должно.
            self.assertEqual(mock_session_cls.captured, [])

    def test_t4_inactive_settings_no_network(self):
        """T4: is_active=False — ok=False, без сетевого запроса."""
        import llm_client
        _patch_settings(is_active=False, api_key="abc")

        with patch("aiohttp.ClientSession", _FakeSession) as mock_session_cls:
            result = asyncio.run(llm_client.complete(
                [{"role": "user", "content": "test"}]
            ))
            self.assertFalse(result.ok)
            self.assertIn("выключена", result.error.lower())
            self.assertEqual(mock_session_cls.captured, [])

    def test_t4b_no_key_active_settings_no_network(self):
        """T4b: is_active=True, но ключ пустой — ok=False, без сети."""
        import llm_client
        _patch_settings(is_active=True, api_key="")

        with patch("aiohttp.ClientSession", _FakeSession) as mock_session_cls:
            result = asyncio.run(llm_client.complete(
                [{"role": "user", "content": "test"}]
            ))
            self.assertFalse(result.ok)
            self.assertEqual(mock_session_cls.captured, [])


class TestCompleteSuccess(unittest.TestCase):
    """T5: complete() на 200 OK — ok=True, текст берётся из choices[0].message.content."""

    def setUp(self):
        _reset_llm_client_state()
        _patch_settings(is_active=True, api_key="abc")

    def tearDown(self):
        _reset_llm_client_state()

    def test_t5_success_returns_text(self):
        import llm_client
        session = _FakeSession()
        session.add_response(_FakeResponse(200, {
            "choices": [{"message": {"role": "assistant", "content": "развернутая причина"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }))
        with patch("aiohttp.ClientSession", return_value=session):
            result = asyncio.run(llm_client.complete(
                [{"role": "user", "content": "спам"}]
            ))
        self.assertTrue(result.ok, f"должен быть ok, error={result.error}")
        self.assertEqual(result.text, "развернутая причина")
        self.assertEqual(result.usage["total_tokens"], 15)
        self.assertFalse(result.cached)
        self.assertGreaterEqual(result.elapsed_ms, 0)

    def test_t5b_authorization_header_has_bearer(self):
        """Заголовок Authorization содержит Bearer <key>."""
        import llm_client
        session = _FakeSession()
        with patch("aiohttp.ClientSession", return_value=session):
            asyncio.run(llm_client.complete([{"role": "user", "content": "x"}]))
        self.assertEqual(len(session.captured), 1)
        headers = session.captured[0]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer abc")

    def test_t5c_payload_has_model(self):
        """Payload содержит model из настроек."""
        import llm_client
        session = _FakeSession()
        with patch("aiohttp.ClientSession", return_value=session):
            asyncio.run(llm_client.complete([{"role": "user", "content": "x"}]))
        self.assertEqual(session.captured[0]["json"]["model"], "GigaChat-Pro")


class TestCompleteHttpErrors(unittest.TestCase):
    """T6, T7: HTTP-ошибки 401/429 корректно обрабатываются."""

    def setUp(self):
        _reset_llm_client_state()
        _patch_settings(is_active=True, api_key="abc")

    def tearDown(self):
        _reset_llm_client_state()

    def test_t6_unauthorized(self):
        import llm_client
        session = _FakeSession()
        # 401 + несколько 429 для ретраев не делаем — 401 не ретраится.
        session.add_response(_FakeResponse(401, {"message": "Unauthorized"}))
        with patch("aiohttp.ClientSession", return_value=session):
            result = asyncio.run(llm_client.complete(
                [{"role": "user", "content": "x"}]
            ))
        self.assertFalse(result.ok)
        self.assertIn("401", result.error)
        self.assertIn("unauthorized", result.error.lower())

    def test_t7_rate_limit_retries_then_fails(self):
        import llm_client
        # 429 трижды — все попытки (1 начальная + 2 ретрая) исчерпаны.
        session = _FakeSession()
        for _ in range(5):  # больше, чем MAX_RETRIES+1, на всякий случай
            session.add_response(_FakeResponse(429, {"message": "rate limit"}))
        with patch("aiohttp.ClientSession", return_value=session):
            with patch("asyncio.sleep", new=AsyncMock()):
                result = asyncio.run(llm_client.complete(
                    [{"role": "user", "content": "x"}]
                ))
        self.assertFalse(result.ok)
        self.assertIn("429", result.error)

    def test_t7b_500_error_no_retry(self):
        """500-е ошибки — ретраятся, но быстро сдаемся."""
        import llm_client
        session = _FakeSession()
        for _ in range(5):
            session.add_response(_FakeResponse(503, {"message": "service unavailable"}))
        with patch("aiohttp.ClientSession", return_value=session):
            with patch("asyncio.sleep", new=AsyncMock()):
                result = asyncio.run(llm_client.complete(
                    [{"role": "user", "content": "x"}]
                ))
        self.assertFalse(result.ok)
        self.assertIn("503", result.error)


class TestCompleteTimeoutAndErrors(unittest.TestCase):
    """T8, T10: таймаут и сетевые ошибки не роняют complete()."""

    def setUp(self):
        _reset_llm_client_state()
        _patch_settings(is_active=True, api_key="abc")

    def tearDown(self):
        _reset_llm_client_state()

    def test_t8_timeout(self):
        import llm_client
        import aiohttp

        class _SlowSession(_FakeSession):
            def post(self, *args, **kwargs):
                raise aiohttp.ServerTimeoutError("slow")

        with patch("aiohttp.ClientSession", _SlowSession):
            with patch("asyncio.sleep", new=AsyncMock()):
                result = asyncio.run(llm_client.complete(
                    [{"role": "user", "content": "x"}]
                ))
        self.assertFalse(result.ok)
        # Может быть либо "таймаут" (от asyncio.wait_for), либо "ServerTimeoutError".
        self.assertTrue(
            "таймаут" in (result.error or "").lower()
            or "timeout" in (result.error or "").lower()
            or "server" in (result.error or "").lower(),
            f"ожидалась ошибка таймаута/сервера, получили: {result.error}",
        )

    def test_t10_network_error_no_exception(self):
        """T10: aiohttp.ClientError не пробрасывается наружу."""
        import llm_client
        import aiohttp

        class _BoomSession(_FakeSession):
            def post(self, *args, **kwargs):
                raise aiohttp.ClientConnectorError(
                    "key", aiohttp.ClientConnectorCertificateError(
                        connection_key="key", certificate_error=OSError("bad cert")
                    )
                )

        with patch("aiohttp.ClientSession", _BoomSession):
            with patch("asyncio.sleep", new=AsyncMock()):
                # Не должно бросать исключение.
                result = asyncio.run(llm_client.complete(
                    [{"role": "user", "content": "x"}]
                ))
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)


class TestCompleteNonJsonResponse(unittest.TestCase):
    """T9: не-JSON ответ — ok=False с внятной причиной."""

    def setUp(self):
        _reset_llm_client_state()
        _patch_settings(is_active=True, api_key="abc")

    def tearDown(self):
        _reset_llm_client_state()

    def test_t9_non_json(self):
        import llm_client
        session = _FakeSession()
        session.add_response(_FakeResponse(500, "<html>500 Server Error</html>"))
        with patch("aiohttp.ClientSession", return_value=session):
            with patch("asyncio.sleep", new=AsyncMock()):
                result = asyncio.run(llm_client.complete(
                    [{"role": "user", "content": "x"}]
                ))
        self.assertFalse(result.ok)


class TestCache(unittest.TestCase):
    """T11, T12: кеш одинаковых запросов и его сброс."""

    def setUp(self):
        _reset_llm_client_state()
        _patch_settings(is_active=True, api_key="abc")

    def tearDown(self):
        _reset_llm_client_state()

    def test_t11_cache_hit_on_second_call(self):
        import llm_client
        session = _FakeSession()
        with patch("aiohttp.ClientSession", return_value=session):
            r1 = asyncio.run(llm_client.complete([{"role": "user", "content": "спам"}]))
            r2 = asyncio.run(llm_client.complete([{"role": "user", "content": "спам"}]))
        self.assertTrue(r1.ok)
        self.assertFalse(r1.cached, "первый вызов — не из кеша")
        self.assertTrue(r2.ok)
        self.assertTrue(r2.cached, "второй вызов — из кеша")
        # Только один сетевой запрос.
        self.assertEqual(len(session.captured), 1,
                         f"должен быть 1 запрос, не {len(session.captured)}")

    def test_t11b_different_prompts_no_cache(self):
        import llm_client
        session = _FakeSession()
        with patch("aiohttp.ClientSession", return_value=session):
            r1 = asyncio.run(llm_client.complete([{"role": "user", "content": "спам"}]))
            r2 = asyncio.run(llm_client.complete([{"role": "user", "content": "мат"}]))
        self.assertFalse(r1.cached)
        self.assertFalse(r2.cached)
        self.assertEqual(len(session.captured), 2)

    def test_t12_reset_cache(self):
        import llm_client
        session = _FakeSession()
        with patch("aiohttp.ClientSession", return_value=session):
            asyncio.run(llm_client.complete([{"role": "user", "content": "x"}]))
            llm_client.reset_cache()
            asyncio.run(llm_client.complete([{"role": "user", "content": "x"}]))
        self.assertEqual(len(session.captured), 2, "после reset_cache — новый запрос")


class TestExpandReason(unittest.TestCase):
    """T13: expand_reason() строит messages с system+user, не падает при None полях."""

    def setUp(self):
        _reset_llm_client_state()
        _patch_settings(is_active=True, api_key="abc")

    def tearDown(self):
        _reset_llm_client_state()

    def test_t13_messages_structure(self):
        import llm_client
        session = _FakeSession()
        with patch("aiohttp.ClientSession", return_value=session):
            result = asyncio.run(llm_client.expand_reason(
                action_type="warn",
                mod_reason="спам",
                offender_text="реклама казино, жми сюда",
                reply_context_text=None,
                rules_md="## Правила",
                recent_reasons=None,
            ))
        self.assertTrue(result.ok)
        # Проверяем структуру отправленного payload.
        payload = session.captured[0]["json"]
        self.assertEqual(len(payload["messages"]), 2)
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertIn("Правила чата", payload["messages"][0]["content"])
        self.assertEqual(payload["messages"][1]["role"], "user")
        self.assertIn("варн", payload["messages"][1]["content"])
        self.assertIn("спам", payload["messages"][1]["content"])
        self.assertIn("реклама казино", payload["messages"][1]["content"])

    def test_t13b_handles_none_offender_text(self):
        import llm_client
        session = _FakeSession()
        with patch("aiohttp.ClientSession", return_value=session):
            result = asyncio.run(llm_client.expand_reason(
                action_type="ban",
                mod_reason="",
                offender_text=None,
                reply_context_text=None,
                rules_md="",
                recent_reasons=None,
            ))
        # LLM вернёт ответ — expand_reason не падает от пустых входов.
        self.assertTrue(result.ok)


class TestTestConnection(unittest.TestCase):
    """T14, T15: test_connection() шлёт короткий промпт, возвращает ok или ошибку."""

    def setUp(self):
        _reset_llm_client_state()

    def tearDown(self):
        _reset_llm_client_state()

    def test_t14_success(self):
        import llm_client
        session = _FakeSession()
        session.add_response(_FakeResponse(200, {
            "choices": [{"message": {"content": "ok"}}],
        }))
        with patch("aiohttp.ClientSession", return_value=session):
            result = asyncio.run(llm_client.test_connection("test-key"))
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "ok")
        # Промпт короткий.
        payload = session.captured[0]["json"]
        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["messages"][0]["content"], "Ответь одним словом: ok")

    def test_t15_unauthorized(self):
        import llm_client
        session = _FakeSession()
        session.add_response(_FakeResponse(401, {"message": "Unauthorized"}))
        with patch("aiohttp.ClientSession", return_value=session):
            result = asyncio.run(llm_client.test_connection("bad-key"))
        self.assertFalse(result.ok)
        self.assertIn("401", result.error)
        self.assertIn("unauthorized", result.error.lower())


class TestExtractHelpers(unittest.TestCase):
    """T16: _extract_content и _extract_usage корректно парсят тело GigaChat."""

    def test_t16a_extract_content(self):
        import llm_client
        body = {"choices": [{"message": {"role": "assistant", "content": "ответ"}}]}
        self.assertEqual(llm_client._extract_content(body), "ответ")

    def test_t16b_extract_content_empty(self):
        import llm_client
        self.assertEqual(llm_client._extract_content({}), "")
        self.assertEqual(llm_client._extract_content({"choices": []}), "")
        self.assertEqual(llm_client._extract_content({"choices": [{}]}), "")

    def test_t16c_extract_usage(self):
        import llm_client
        body = {"usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}
        usage = llm_client._extract_usage(body)
        self.assertEqual(usage, {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8})

    def test_t16d_extract_usage_empty(self):
        import llm_client
        self.assertEqual(llm_client._extract_usage({}), {})


class TestTruncate(unittest.TestCase):
    """MAX_PROMPT_CHARS — промпт обрезается."""

    def test_truncate_short(self):
        import llm_client
        self.assertEqual(llm_client._truncate("короткий"), "короткий")

    def test_truncate_long(self):
        import llm_client
        long_text = "x" * (llm_client.MAX_PROMPT_CHARS + 100)
        truncated = llm_client._truncate(long_text)
        self.assertLessEqual(len(truncated), llm_client.MAX_PROMPT_CHARS)
        self.assertIn("обрезано", truncated)


if __name__ == "__main__":
    unittest.main(verbosity=2)
