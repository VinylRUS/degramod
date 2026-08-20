"""
test_v500_latency_alert.py — алерт SU при устойчивых задержках Telegram
(roadmap 5.0.0-08).

Зачем. Торможение Bot API — предвестник проблем: команды модераторов начинают
отваливаться по таймауту, наказания не применяются, а в логах это выглядит как
редкие несвязанные ошибки. Пока никто не смотрит в /healthz, деградация
остаётся незамеченной до момента, когда бот фактически перестаёт работать.

Источник замеров — фоновый пробник getMe, который ходит в Telegram раз в
минуту (health_probe.probe_tick). Взят он, а не обёртка над реальными
операциями, потому что замеры регулярные: «пять подряд» превращается в
понятные пять минут. Обёртка молчала бы ночью, когда модераторы спят и
вызовов нет — то есть ровно тогда, когда некому заметить проблему вручную.

Что проверяется:

  1. Серия из пяти медленных ответов подряд поднимает алерт.
  2. Четырёх мало — порог не срабатывает раньше времени.
  3. Быстрый ответ в середине обрывает серию: всплеск ≠ деградация.
  4. Обрыв связи не считается медленным ответом. Это другая авария, её
     показывает degraded в /healthz; смешивать нельзя, иначе при недоступном
     Telegram SU получит алерт «медленно», а не «связи нет».
  5. Антиспам: второй алерт внутри 30 минут не уходит, после — уходит.
  6. Алерт доходит до всех SU, и недоставка одному не мешает остальным.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from _paths import _P

os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("ADMIN_IDS", "111,222")
os.environ.setdefault("WEB_PASSWORD", "test-pwd")
os.environ.setdefault("WEB_ALLOW_NO_SECRET", "1")

sys.path.insert(0, _P())

import health_probe  # noqa: E402

_SLOW = 1500   # выше порога 1000 мс
_FAST = 120


class _ProbeCase(unittest.TestCase):
    def setUp(self):
        health_probe.reset_state()


class TestLatencyHistory(_ProbeCase):
    """История замеров — основа детекции."""

    def test_history_records_measurements(self):
        for _ in range(3):
            health_probe.record_latency(_FAST)
        self.assertEqual(len(health_probe.latency_history()), 3)

    def test_history_capped_at_ten(self):
        """deque(maxlen=10) из роадмапа: старые замеры вытесняются."""
        for i in range(25):
            health_probe.record_latency(i)
        self.assertEqual(len(health_probe.latency_history()), 10)

    def test_reset_clears_history(self):
        health_probe.record_latency(_FAST)
        health_probe.reset_state()
        self.assertEqual(len(health_probe.latency_history()), 0)


class TestAlertTrigger(_ProbeCase):
    """Порог: пять подряд медленных."""

    def test_five_slow_in_a_row_triggers(self):
        for _ in range(5):
            health_probe.record_latency(_SLOW)
        self.assertTrue(health_probe.should_alert())

    def test_four_slow_is_not_enough(self):
        """Четыре — ещё не серия. Иначе алерты полетят от любого всплеска."""
        for _ in range(4):
            health_probe.record_latency(_SLOW)
        self.assertFalse(health_probe.should_alert())

    def test_fast_measurement_breaks_the_streak(self):
        """Один быстрый ответ обнуляет серию — API оправился."""
        for _ in range(4):
            health_probe.record_latency(_SLOW)
        health_probe.record_latency(_FAST)
        health_probe.record_latency(_SLOW)
        self.assertFalse(health_probe.should_alert())

    def test_disconnect_does_not_count_as_slow(self):
        """Обрыв связи — не «медленно».

        При недоступном Telegram latency неизвестна (None). Если считать это
        медленным ответом, SU получит алерт про задержки вместо того, чтобы
        увидеть degraded по недоступности.
        """
        for _ in range(5):
            health_probe.record_latency(None)
        self.assertFalse(health_probe.should_alert())

    def test_borderline_value_does_not_trigger(self):
        """Ровно 1000 мс — не «больше 1000». Граница по роадмапу строгая."""
        for _ in range(5):
            health_probe.record_latency(1000)
        self.assertFalse(health_probe.should_alert())


class TestAlertThrottle(_ProbeCase):
    """Антиспам: не чаще раза в 30 минут."""

    def _make_streak(self):
        for _ in range(5):
            health_probe.record_latency(_SLOW)

    def test_second_alert_suppressed_within_window(self):
        self._make_streak()
        self.assertTrue(health_probe.should_alert())
        health_probe.mark_alert_sent()

        self._make_streak()
        self.assertFalse(
            health_probe.should_alert(),
            "повторный алерт внутри 30 минут — это спам в личку SU",
        )

    def test_alert_allowed_after_window(self):
        self._make_streak()
        health_probe.mark_alert_sent()

        # Сдвигаем отметку на 31 минуту назад.
        with patch.object(health_probe, "_alert_state", {"last_sent": 0.0}):
            self._make_streak()
            self.assertTrue(health_probe.should_alert())


class TestHealthzExposesAverage(_ProbeCase):
    """Среднее по истории видно в /healthz."""

    def test_average_in_payload(self):
        for value in (100, 200, 300):
            health_probe.record_latency(value)
        payload = health_probe.collect_health("v5.0.0", 0.0)
        self.assertIn("telegram_api_latency_avg_ms", payload)
        self.assertEqual(payload["telegram_api_latency_avg_ms"], 200)

    def test_average_is_none_without_history(self):
        payload = health_probe.collect_health("v5.0.0", 0.0)
        self.assertIsNone(payload["telegram_api_latency_avg_ms"])


class TestProbeFeedsHistory(_ProbeCase):
    """probe_tick пополняет историю — иначе детекция никогда не сработает."""

    def test_successful_probe_records_latency(self):
        bot = MagicMock()
        bot.get_me = AsyncMock(return_value=MagicMock())
        asyncio.run(health_probe.probe_tick(bot))
        self.assertEqual(len(health_probe.latency_history()), 1)

    def test_failed_probe_records_none(self):
        bot = MagicMock()
        bot.get_me = AsyncMock(side_effect=RuntimeError("нет сети"))
        asyncio.run(health_probe.probe_tick(bot))
        self.assertEqual(health_probe.latency_history(), [None])


class TestAlertDelivery(unittest.IsolatedAsyncioTestCase):
    """Доставка алерта в личку каждому SU."""

    async def test_sends_to_all_su(self):
        import bot_handlers

        bot = MagicMock()
        bot.send_message = AsyncMock()

        with patch.object(bot_handlers, "ADMIN_IDS", [111, 222]):
            await bot_handlers.send_latency_alert_to_su(
                bot, streak=5, avg_ms=1500, last_ms=1600,
            )

        targets = {c.kwargs["chat_id"] for c in bot.send_message.await_args_list}
        self.assertEqual(targets, {111, 222})

    async def test_undelivered_to_one_does_not_block_others(self):
        """SU мог не начать диалог с ботом — остальные должны получить алерт."""
        import bot_handlers
        from aiogram.exceptions import TelegramForbiddenError

        bot = MagicMock()

        async def flaky(**kwargs):
            if kwargs["chat_id"] == 111:
                raise TelegramForbiddenError(method=MagicMock(), message="blocked")
            return MagicMock()

        bot.send_message = AsyncMock(side_effect=flaky)

        with patch.object(bot_handlers, "ADMIN_IDS", [111, 222]):
            await bot_handlers.send_latency_alert_to_su(
                bot, streak=5, avg_ms=1500, last_ms=1600,
            )

        targets = {c.kwargs["chat_id"] for c in bot.send_message.await_args_list}
        self.assertIn(222, targets, "второй SU не получил алерт из-за первого")

    async def test_alert_text_contains_numbers(self):
        """Без цифр алерт бесполезен: «что-то тормозит» не действие."""
        import bot_handlers

        bot = MagicMock()
        bot.send_message = AsyncMock()

        with patch.object(bot_handlers, "ADMIN_IDS", [111]):
            await bot_handlers.send_latency_alert_to_su(
                bot, streak=5, avg_ms=1500, last_ms=1600,
            )

        text = bot.send_message.await_args.kwargs["text"]
        self.assertIn("1500", text)
        self.assertIn("5", text)


class TestProbeLoopWiring(unittest.TestCase):
    """Детекция должна вызываться из фонового цикла, иначе она мертва."""

    def test_loop_checks_and_sends(self):
        """_health_probe_loop зовёт should_alert и отправку алерта."""
        from pathlib import Path
        src = Path(_P("bot.py")).read_text(encoding="utf-8")
        start = src.index("async def _health_probe_loop")
        end = src.index("\nasync def ", start + 10)
        body = src[start:end]
        self.assertIn("should_alert", body,
                      "цикл не проверяет условие алерта")
        self.assertIn("send_latency_alert_to_su", body,
                      "цикл не отправляет алерт")
        self.assertIn("mark_alert_sent", body,
                      "цикл не отмечает отправку — антиспам не заработает")

    def test_alert_function_is_importable_in_bot(self):
        """Функция реально доступна в namespace bot.py, а не только в тексте.

        Регресс: вызов добавили в цикл, а в список импортов из bot_handlers
        — нет. Грep по исходнику это пропускал, а в рантайме первый же
        сработавший алерт падал бы с NameError, причём внутри фоновой
        задачи, где ошибку видно только в логах.
        """
        import bot
        self.assertTrue(
            hasattr(bot, "send_latency_alert_to_su"),
            "send_latency_alert_to_su не импортирована в bot.py — будет NameError",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
