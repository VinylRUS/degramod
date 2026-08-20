"""
test_v4102_healthz.py — /healthz для внешнего мониторинга (Task 16, 5.0.0-07).

Старый /health отдаёт четыре поля и всегда 200 — по нему нельзя отличить
живого бота от задыхающегося. /healthz добавляет метрики и градацию, чтобы
деградация была видна до краха: утечка памяти и торможение Telegram API
проявляются за десятки минут до того, как процесс ляжет.

Что проверяем:

  1. Градации: ok / degraded / down на всех порогах.
  2. Коды: ok и degraded → 200, down → 503. Мониторинг должен трубить
     только когда бот реально при смерти, иначе тревоги обесценятся.
  3. Эндпоинт не ходит в сеть. Данные о Telegram берутся из снимка,
     который пишет фоновый пробник — иначе мониторинг раз в 30 секунд
     дёргал бы Bot API и упёрся в rate limit.
  4. memory_percent = null, когда лимит контейнера неизвестен. Считать
     процент от памяти хоста нельзя: на машине с 32 ГБ бот всегда выглядел
     бы здоровым.
  5. Старый /health не изменился — его может опрашивать мониторинг Bothost.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from _paths import _P

os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("WEB_PASSWORD", "test-pwd")
os.environ.setdefault("SESSION_SECRET", "x" * 40)
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("WEB_ALLOW_NO_SECRET", "1")

sys.path.insert(0, _P())

from pathlib import Path  # noqa: E402

WORK_DIR = Path(_P())

import health_probe  # noqa: E402


class TestGrading(unittest.TestCase):
    """Градация складывается из памяти и состояния Telegram."""

    def test_ok_when_everything_healthy(self):
        self.assertEqual(
            health_probe.grade(memory_percent=40.0, tg_connected=True, tg_latency_ms=120),
            "ok",
        )

    def test_degraded_above_85_percent_memory(self):
        self.assertEqual(
            health_probe.grade(memory_percent=86.0, tg_connected=True, tg_latency_ms=120),
            "degraded",
        )

    def test_down_above_95_percent_memory(self):
        """95% — процесс вот-вот словит OOM, это уже не деградация."""
        self.assertEqual(
            health_probe.grade(memory_percent=96.0, tg_connected=True, tg_latency_ms=120),
            "down",
        )

    def test_degraded_when_telegram_unreachable(self):
        self.assertEqual(
            health_probe.grade(memory_percent=10.0, tg_connected=False, tg_latency_ms=None),
            "degraded",
        )

    def test_degraded_when_telegram_slow(self):
        """Больше секунды на getMe — Telegram тормозит, бот ещё жив."""
        self.assertEqual(
            health_probe.grade(memory_percent=10.0, tg_connected=True, tg_latency_ms=1500),
            "degraded",
        )

    def test_unknown_memory_does_not_trigger_degraded(self):
        """memory_percent=None — лимит неизвестен, а не «всё плохо».

        Иначе локальный запуск и любой хост без cgroup-лимита вечно
        числились бы деградировавшими.
        """
        self.assertEqual(
            health_probe.grade(memory_percent=None, tg_connected=True, tg_latency_ms=100),
            "ok",
        )

    def test_memory_wins_over_telegram(self):
        """down по памяти важнее degraded по Telegram — берём худшее."""
        self.assertEqual(
            health_probe.grade(memory_percent=99.0, tg_connected=False, tg_latency_ms=None),
            "down",
        )


class TestMemoryLimit(unittest.TestCase):
    """Знаменатель для memory_percent ищется по цепочке cgroup → None."""

    def test_returns_none_without_cgroup(self):
        """Нет cgroup-файлов — процент посчитать не от чего.

        Память хоста в знаменатель не годится: 300 МБ от 32 ГБ это 1%,
        и порог никогда не сработает, хотя контейнеру дали 512 МБ.
        """
        with patch("health_probe._read_first_int", return_value=None):
            self.assertIsNone(health_probe.memory_limit_bytes())

    def test_reads_cgroup_v2(self):
        with patch("health_probe._read_first_int", side_effect=[536870912]):
            self.assertEqual(health_probe.memory_limit_bytes(), 536870912)

    def test_ignores_unlimited_sentinel(self):
        """cgroup v2 пишет `max`, а v1 — огромное число, когда лимита нет."""
        with patch("health_probe._read_first_int", return_value=None):
            self.assertIsNone(health_probe.memory_limit_bytes())


class TestProbeSnapshot(unittest.TestCase):
    """Снимок Telegram обновляет фоновый пробник, не эндпоинт."""

    def setUp(self):
        health_probe.reset_state()

    def test_snapshot_before_first_probe(self):
        """До первого прогона связь неизвестна, а не «оборвана»."""
        snap = health_probe.snapshot()
        self.assertIsNone(snap["telegram_connected"])
        self.assertIsNone(snap["telegram_api_latency_ms"])

    def test_probe_records_success(self):
        import asyncio
        bot = MagicMock()
        bot.get_me = AsyncMock(return_value=MagicMock(username="test_bot"))
        asyncio.run(health_probe.probe_tick(bot))

        snap = health_probe.snapshot()
        self.assertTrue(snap["telegram_connected"])
        self.assertIsNotNone(snap["telegram_api_latency_ms"])
        self.assertGreaterEqual(snap["telegram_api_latency_ms"], 0)

    def test_probe_records_failure(self):
        """Сбой getMe не поднимает исключение наружу — фоновая таска не должна падать."""
        import asyncio
        bot = MagicMock()
        bot.get_me = AsyncMock(side_effect=RuntimeError("network down"))
        asyncio.run(health_probe.probe_tick(bot))

        snap = health_probe.snapshot()
        self.assertFalse(snap["telegram_connected"])

    def test_probe_survives_bot_none(self):
        """create_app(bot=None) — штатная ситуация в тестах."""
        import asyncio
        asyncio.run(health_probe.probe_tick(None))
        self.assertIsNone(health_probe.snapshot()["telegram_connected"])


class TestHealthzEndpoint(unittest.TestCase):
    """HTTP-контракт: поля, коды, отсутствие похода в сеть."""

    def setUp(self):
        health_probe.reset_state()
        import web_app
        from fastapi.testclient import TestClient
        self.client = TestClient(web_app.create_app())

    def test_returns_required_fields(self):
        r = self.client.get("/healthz")
        body = r.json()
        for field in (
            "status", "bot_id", "container_id", "version", "uptime_seconds",
            "memory_mb", "memory_percent", "telegram_connected",
            "telegram_api_latency_ms", "timestamp",
        ):
            self.assertIn(field, body, f"нет поля {field}")

    def test_public_no_auth_required(self):
        """Мониторинг ходит без куки — 200, а не редирект на /login."""
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_does_not_call_telegram(self):
        """Эндпоинт читает снимок пробника и в сеть не ходит.

        Без этого мониторинг раз в 30 секунд превратился бы в постоянный
        поток getMe к Bot API.
        """
        import web_app
        from fastapi.testclient import TestClient
        bot = MagicMock()
        bot.get_me = AsyncMock()
        client = TestClient(web_app.create_app(bot=bot))
        client.get("/healthz")
        bot.get_me.assert_not_called()

    def test_503_when_down(self):
        with patch("health_probe.collect_health", return_value={
            "status": "down", "bot_id": None, "container_id": None,
            "version": "v4.10.2", "uptime_seconds": 5, "memory_mb": 999,
            "memory_percent": 97.0, "telegram_connected": True,
            "telegram_api_latency_ms": 50, "timestamp": "2026-08-20T00:00:00+00:00",
        }):
            self.assertEqual(self.client.get("/healthz").status_code, 503)

    def test_200_when_degraded(self):
        """degraded — предупреждение, а не авария: мониторинг не должен
        считать бота упавшим и дёргать авто-рестарт."""
        with patch("health_probe.collect_health", return_value={
            "status": "degraded", "bot_id": None, "container_id": None,
            "version": "v4.10.2", "uptime_seconds": 5, "memory_mb": 400,
            "memory_percent": 88.0, "telegram_connected": True,
            "telegram_api_latency_ms": 50, "timestamp": "2026-08-20T00:00:00+00:00",
        }):
            self.assertEqual(self.client.get("/healthz").status_code, 200)


class TestLegacyHealthUnchanged(unittest.TestCase):
    """Старый /health опрашивает мониторинг Bothost — контракт не меняем."""

    def setUp(self):
        import web_app
        from fastapi.testclient import TestClient
        self.client = TestClient(web_app.create_app())

    def test_legacy_fields_intact(self):
        body = self.client.get("/health").json()
        self.assertEqual(set(body), {"status", "service", "version", "time"})
        self.assertEqual(body["status"], "ok")

    def test_legacy_always_200(self):
        self.assertEqual(self.client.get("/health").status_code, 200)


class TestProbeLoopWired(unittest.TestCase):
    """Пробник должен реально запускаться, иначе поля Telegram вечно null."""

    def test_bot_module_defines_probe_loop(self):
        """В bot.py есть цикл, вызывающий probe_tick."""
        src = (WORK_DIR / "bot.py").read_text(encoding="utf-8")
        self.assertIn("health_probe", src,
                      "bot.py не импортирует health_probe — пробник не запускается")
        self.assertIn("probe_tick", src,
                      "bot.py не вызывает probe_tick")

    def test_probe_loop_is_separate_task(self):
        """Пробник — отдельная таска, не тик внутри _night_mode_loop.

        В _night_mode_loop инвариант порядка (alarm → sanitary → night,
        см. CLAUDE.md). Зависший get_me задержал бы снятие режимов чата.
        """
        src = (WORK_DIR / "bot.py").read_text(encoding="utf-8")
        night_start = src.index("async def _night_mode_loop")
        night_end = src.index("async def ", night_start + 10)
        night_body = src[night_start:night_end]
        self.assertNotIn("probe_tick", night_body,
                         "probe_tick вызывается внутри _night_mode_loop")

    def test_probe_task_cancelled_on_shutdown(self):
        """Таска пробника попадает в список отменяемых при shutdown.

        Регресс: сначала она создавалась, но в bg_tasks не добавлялась —
        TaskGroup ждал завершения бесконечного `while True`, и выключение
        зависало до hard-таймаута. На проде это означало бы, что каждый
        рестарт контейнера упирается в форс-килл.
        """
        src = (WORK_DIR / "bot.py").read_text(encoding="utf-8")
        self.assertRegex(
            src, r"bg_tasks\s*=\s*\[[^\]]*health_task",
            "health_task не входит в bg_tasks — shutdown подвиснет",
        )

    def test_probe_task_registered_in_taskgroup(self):
        """Таска создаётся через tg.create_task — иначе GC соберёт её на середине."""
        src = (WORK_DIR / "bot.py").read_text(encoding="utf-8")
        self.assertRegex(
            src, r"create_task\(\s*_health_probe_loop\(\)",
            "цикл пробника не зарегистрирован в TaskGroup",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
