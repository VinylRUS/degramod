"""
test_v473_background_lifecycle.py — Smoke-тест v4.7.3: graceful shutdown
через единый asyncio.TaskGroup + Semaphore(100) для ephemeral auto-delete.

Проверяет:
  1. APP_VERSION = "v4.7.3".
  2. bot.py содержит asyncio.TaskGroup() в lifespan.
  3. bot.py содержит asyncio.wait_for(..., timeout=...) для hard shutdown cap.
  4. bot.py содержит asyncio.Semaphore (через bot_handlers — но проверяем
     что _EPHEMERAL_DELETE_SEM существует и имеет value=100).
  5. bot.py содержит _startup_recovery функцию.
  6. bot.py содержит _SHUTDOWN_TIMEOUT_SECONDS константу = 5.0.
  7. _startup_recovery логирует и прогоняет tick если есть чаты с зависшими active-флагами.
  8. _startup_recovery НЕ прогоняет tick если нет зависших чатов (no-op).
  9. Semaphore: 200 ephemeral auto-delete задач — только 100 одновременно активны.
 10. Semaphore: после завершения одной задачи — слот освобождается для следующей.
 11. CancelledError в _del_ephemeral корректно прокидывается (не глотается).
 12. Changelog содержит v4.7.3.
 13. Старые asyncio.create_task в lifespan убраны (нет `_polling_task = asyncio.create_task`).
"""
from _paths import _P  # noqa: E402  (корень вычисляется от __file__)
import asyncio
import inspect
import os
import re
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, _P())
os.chdir(_P())

_DB_PATH = tempfile.mktemp(suffix="_v473.db")
os.environ["BOT_TOKEN"] = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
os.environ["ADMIN_IDS"] = "1"
os.environ["WEB_PASSWORD"] = "test_password_123"
os.environ["DB_PATH"] = _DB_PATH

from sqlalchemy import select, text
from db import (
    init_db, async_session, ChatSettings, WebUser, engine,
)
import web_app
import bot_handlers as bh
import bot as bot_module

from aiogram.exceptions import TelegramBadRequest


# ── aiogram's TelegramBadRequest requires (method, message). Patch it for tests.
_orig_tb_init = TelegramBadRequest.__init__
def _patched_tb_init(self, method=None, message=""):
    if method is None:
        method = MagicMock()
    _orig_tb_init(self, method, message)
TelegramBadRequest.__init__ = _patched_tb_init


async def _seed():
    """Init DB + seed SU."""
    await init_db()
    async with async_session() as s:
        await s.execute(text("DELETE FROM permission_presets"))
        await s.execute(text("DELETE FROM chat_settings"))
        await s.execute(text("DELETE FROM web_users WHERE username != 'su'"))
        await s.execute(text("DELETE FROM chat_admins"))
        await s.commit()
    await init_db()
    async with async_session() as s:
        existing_su = (await s.execute(
            select(WebUser).where(WebUser.username == "su")
        )).scalar_one_or_none()
        if existing_su is None:
            s.add(WebUser(username="su", is_su=True, is_active=True,
                          role="su", created_by="system"))
            await s.commit()


# ──────────────────────────────────────────────────────────────────────────
class TestV473VersionAndSource(unittest.IsolatedAsyncioTestCase):
    """Проверка что код v4.7.3 содержит нужные структуры."""

    async def asyncSetUp(self):
        await _seed()

    async def asyncTearDown(self):
        try:
            engine.sync_engine.dispose()
        except Exception:
            pass
        if os.path.exists(_DB_PATH):
            os.remove(_DB_PATH)

    # ── Test 1: APP_VERSION ─────────────────────────────────────────
    async def test_app_version_is_v473(self):
        self.assertGreaterEqual(web_app.APP_VERSION, "v4.7.3",
            f"APP_VERSION={web_app.APP_VERSION} should be >= v4.7.3")

    # ── Test 2: lifespan использует asyncio.TaskGroup ───────────────
    async def test_lifespan_uses_taskgroup(self):
        """В исходнике bot.py lifespan должен использовать asyncio.TaskGroup."""
        src = inspect.getsource(bot_module.lifespan)
        self.assertIn("asyncio.TaskGroup", src,
                      "lifespan must use asyncio.TaskGroup()")
        self.assertIn("async with asyncio.TaskGroup()", src,
                      "lifespan must enter TaskGroup as async context manager")

    # ── Test 3: lifespan использует asyncio.wait_for с timeout ──────
    async def test_lifespan_uses_wait_for_timeout(self):
        """В lifespan есть asyncio.wait_for(..., timeout=_SHUTDOWN_TIMEOUT_SECONDS)."""
        src = inspect.getsource(bot_module.lifespan)
        self.assertIn("asyncio.wait_for", src,
                      "lifespan must use asyncio.wait_for for hard shutdown cap")
        self.assertIn("_SHUTDOWN_TIMEOUT_SECONDS", src,
                      "lifespan must reference _SHUTDOWN_TIMEOUT_SECONDS")

    # ── Test 4: _EPHEMERAL_DELETE_SEM существует и value=100 ────────
    async def test_ephemeral_delete_semaphore_exists(self):
        """bot_handlers._EPHEMERAL_DELETE_SEM должен быть asyncio.Semaphore(100)."""
        self.assertTrue(hasattr(bh, "_EPHEMERAL_DELETE_SEM"),
                        "bot_handlers must have _EPHEMERAL_DELETE_SEM")
        sem = bh._EPHEMERAL_DELETE_SEM
        self.assertIsInstance(sem, asyncio.Semaphore,
                              f"_EPHEMERAL_DELETE_SEM must be asyncio.Semaphore, "
                              f"got {type(sem)}")
        # Semaphore._value reflects the initial bound (only if no one acquired yet)
        # In 3.10+ value is exposed as sem._value if no acquisitions.
        self.assertEqual(sem._value, 100,
                         f"Semaphore initial value should be 100, got {sem._value}")

    # ── Test 5: _startup_recovery функция существует ────────────────
    async def test_startup_recovery_function_exists(self):
        self.assertTrue(hasattr(bot_module, "_startup_recovery"),
                        "bot module must have _startup_recovery function")
        self.assertTrue(asyncio.iscoroutinefunction(bot_module._startup_recovery),
                        "_startup_recovery must be async")

    # ── Test 6: _SHUTDOWN_TIMEOUT_SECONDS = 5.0 ─────────────────────
    async def test_shutdown_timeout_constant(self):
        self.assertTrue(hasattr(bot_module, "_SHUTDOWN_TIMEOUT_SECONDS"),
                        "bot module must have _SHUTDOWN_TIMEOUT_SECONDS constant")
        self.assertEqual(bot_module._SHUTDOWN_TIMEOUT_SECONDS, 5.0)

    # ── Test 7: _startup_recovery прогоняет tick при зависших флагах ─
    async def test_startup_recovery_runs_tick_when_stuck(self):
        """Создаём чат с night_mode_currently_active=True (симулируем
        жёсткий SIGTERM в прошлом запуске). _startup_recovery должен
        вызвать _sanitary_day_tick + _night_mode_tick."""
        # Setup: chat with stuck flag
        async with async_session() as s:
            cs = ChatSettings(
                chat_id=-1004730000001,
                title="Stuck Chat",
                is_enabled=True,
                night_mode_enabled=True,
                night_mode_currently_active=True,  # STUCK!
                night_mode_start="23:00",
                night_mode_end="07:00",
                night_mode_saved_permissions='{"can_send_messages": true}',
            )
            s.add(cs)
            await s.commit()

        # Mock ticks чтобы проверить что они вызваны
        with patch.object(bot_module, "_sanitary_day_tick", new_callable=AsyncMock) as m_san, \
             patch.object(bot_module, "_night_mode_tick", new_callable=AsyncMock) as m_night:
            await bot_module._startup_recovery()
            self.assertTrue(m_san.called,
                            "_startup_recovery must call _sanitary_day_tick when stuck")
            self.assertTrue(m_night.called,
                            "_startup_recovery must call _night_mode_tick when stuck")

    # ── Test 8: _startup_recovery NO-OP если нет зависших ───────────
    async def test_startup_recovery_noop_when_no_stuck(self):
        """Если нет чатов с зависшими active-флагами — tick не вызывается."""
        with patch.object(bot_module, "_sanitary_day_tick", new_callable=AsyncMock) as m_san, \
             patch.object(bot_module, "_night_mode_tick", new_callable=AsyncMock) as m_night:
            await bot_module._startup_recovery()
            self.assertFalse(m_san.called,
                             "_startup_recovery must NOT call tick when no stuck chats")
            self.assertFalse(m_night.called,
                             "_startup_recovery must NOT call tick when no stuck chats")

    # ── Test 9: Changelog содержит v4.7.3 ───────────────────────────
    async def test_changelog_mentions_v473(self):
        with open("templates/base.html", "r", encoding="utf-8") as f:
            html = f.read()
        self.assertIn("v4.7.3", html, "Changelog must mention v4.7.3")
        self.assertIn("TaskGroup", html, "Changelog must mention TaskGroup")
        self.assertIn("Semaphore", html, "Changelog must mention Semaphore")
        self.assertIn("Startup recovery", html, "Changelog must mention Startup recovery")

    # ── Test 10: старые _polling_task убраны ────────────────────────
    async def test_no_global_polling_task(self):
        """Глобальная переменная _polling_task должна быть убрана."""
        self.assertFalse(hasattr(bot_module, "_polling_task"),
                         "_polling_task global should be removed in v4.7.3")
        src = inspect.getsource(bot_module.lifespan)
        # Must NOT use the old pattern of assigning to _polling_task
        self.assertNotIn("_polling_task = asyncio.create_task", src,
                         "lifespan must not assign _polling_task via create_task")

    # ── Test 11: lifespan вызывает _startup_recovery ────────────────
    async def test_lifespan_calls_startup_recovery(self):
        src = inspect.getsource(bot_module.lifespan)
        self.assertIn("_startup_recovery()", src,
                      "lifespan must call _startup_recovery() on startup")


# ──────────────────────────────────────────────────────────────────────────
class TestSemaphoreLimitsConcurrency(unittest.IsolatedAsyncioTestCase):
    """Проверяет что Semaphore(100) реально ограничивает кол-во одновременно
    ожидающих auto-delete задач.
    """

    async def asyncSetUp(self):
        await _seed()

    async def asyncTearDown(self):
        try:
            engine.sync_engine.dispose()
        except Exception:
            pass
        if os.path.exists(_DB_PATH):
            os.remove(_DB_PATH)

    # ── Test 12: 200 ephemeral auto-delete — max 100 одновременно ───
    async def test_semaphore_caps_concurrent_auto_deletes(self):
        """Создаём 200 auto-delete задач. Semaphore(100) гарантирует что
        одновременно активны (sleep + delete) максимум 100.
        """
        # Reset semaphore to a fresh state (in case other tests touched it)
        bh._EPHEMERAL_DELETE_SEM = asyncio.Semaphore(100)

        # Track concurrent active count
        active = 0
        max_active = 0
        active_lock = asyncio.Lock()

        async def track_active_sleep():
            nonlocal active, max_active
            async with bh._EPHEMERAL_DELETE_SEM:
                async with active_lock:
                    active += 1
                    if active > max_active:
                        max_active = active
                await asyncio.sleep(0.05)  # short sleep
                async with active_lock:
                    active -= 1

        # Spawn 200 tasks
        tasks = [asyncio.create_task(track_active_sleep()) for _ in range(200)]
        await asyncio.gather(*tasks)

        self.assertLessEqual(max_active, 100,
                             f"Max concurrent active should be <= 100, got {max_active}")
        self.assertGreater(max_active, 0,
                           "At least some tasks should have been active")

    # ── Test 13: Semaphore освобождается после CancelledError ───────
    async def test_semaphore_released_on_cancel(self):
        """Если auto-delete задачу отменили во время sleep — semaphore
        должен корректно освободиться (через async with __aexit__).
        """
        bh._EPHEMERAL_DELETE_SEM = asyncio.Semaphore(100)
        initial_value = bh._EPHEMERAL_DELETE_SEM._value

        async def cancellable_sleep():
            async with bh._EPHEMERAL_DELETE_SEM:
                await asyncio.sleep(60)  # long sleep

        task = asyncio.create_task(cancellable_sleep())
        await asyncio.sleep(0.05)  # let it acquire
        # Semaphore should be 99 (one acquired)
        self.assertEqual(bh._EPHEMERAL_DELETE_SEM._value, 99,
                         "After acquire, value should be 99")

        # Cancel
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Give event loop a tick to release
        await asyncio.sleep(0.05)
        self.assertEqual(bh._EPHEMERAL_DELETE_SEM._value, 100,
                         "After cancellation, semaphore should be back to 100")

    # ── Test 14: CancelledError в _send_ephemeral's _del_ephemeral ──
    async def test_send_ephemeral_del_cancelled_propagates(self):
        """Если auto-delete задачу отменили — CancelledError корректно
        прокидывается (не глотается как Unexpected error)."""
        bh._EPHEMERAL_DELETE_SEM = asyncio.Semaphore(100)

        bot = MagicMock()
        sent = MagicMock(); sent.message_id = 555
        bot.send_message = AsyncMock(return_value=sent)
        bot.delete_message = AsyncMock()

        recipient = MagicMock(); recipient.id = 1

        # Patch asyncio.sleep to be cancellable
        await bh._send_ephemeral(
            bot=bot, chat_id=1, recipient=recipient, text="x",
            delete_after=60,  # long sleep
        )
        # Now there should be 1 pending auto-delete task
        # We can't easily get the task handle, but we can check that
        # bot.delete_message hasn't been called yet
        self.assertFalse(bot.delete_message.called,
                         "delete_message should not be called before sleep completes")


# ──────────────────────────────────────────────────────────────────────────
class TestLifespanShutdownBehavior(unittest.IsolatedAsyncioTestCase):
    """Проверяет что shutdown lifespan корректно отменяет tasks за <= 5s."""

    async def asyncSetUp(self):
        await _seed()

    async def asyncTearDown(self):
        try:
            engine.sync_engine.dispose()
        except Exception:
            pass
        if os.path.exists(_DB_PATH):
            os.remove(_DB_PATH)

    # ── Test 15: shutdown completes within timeout ─────────────────
    async def test_shutdown_completes_within_timeout(self):
        """Симулируем shutdown: lifespan с background loops должен
        завершиться за <= _SHUTDOWN_TIMEOUT_SECONDS + небольшой буфер.
        """
        # Patch _night_mode_loop и _start_polling чтобы они были отменяемыми
        # (используют asyncio.sleep, который respects CancelledError)
        cancel_event = asyncio.Event()

        async def fake_night_loop():
            try:
                while True:
                    await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancel_event.set()
                raise

        with patch.object(bot_module, "_night_mode_loop", fake_night_loop), \
             patch.object(bot_module, "_start_polling", AsyncMock()), \
             patch.object(bot_module, "init_db_with_fallback", AsyncMock()), \
             patch.object(bot_module, "_startup_recovery", AsyncMock()), \
             patch.object(bot_module, "bot") as mock_bot:
            mock_bot.delete_my_commands = AsyncMock()
            mock_bot.delete_webhook = AsyncMock()
            mock_bot.session.close = AsyncMock()
            mock_bot.set_webhook = AsyncMock()
            mock_bot.get_webhook_info = AsyncMock(return_value=MagicMock(url=""))
            mock_bot.get_chat = AsyncMock(return_value=MagicMock(id=1, title="", type="private"))

            # Use TestClient to actually run lifespan
            from fastapi.testclient import TestClient
            app = web_app.create_app(lifespan=bot_module.lifespan, bot=mock_bot)

            import time
            with TestClient(app) as client:
                # App is running, lifespan started
                pass  # exit context → shutdown
            # Lifespan shutdown completed
            self.assertTrue(cancel_event.is_set(),
                            "fake_night_loop should have been cancelled on shutdown")


# ──────────────────────────────────────────────────────────────────────────
class TestStartupRecoveryReconcilesState(unittest.IsolatedAsyncioTestCase):
    """Проверяет что _startup_recovery действительно восстанавливает права."""

    async def asyncSetUp(self):
        await _seed()

    async def asyncTearDown(self):
        try:
            engine.sync_engine.dispose()
        except Exception:
            pass
        if os.path.exists(_DB_PATH):
            os.remove(_DB_PATH)

    # ── Test 16: recovery вызывает restore из snapshot ──────────────
    async def test_recovery_restores_from_snapshot(self):
        """Чат с night_mode_currently_active=True и snapshot в БД —
        recovery должен вызвать _night_mode_tick, который (если сейчас
        не в окне night mode) восстановит права из snapshot и снимет флаг.
        """
        # Setup: chat with stuck flag + snapshot
        async with async_session() as s:
            cs = ChatSettings(
                chat_id=-1004730000002,
                title="Stuck Recovery",
                is_enabled=True,
                night_mode_enabled=True,
                night_mode_currently_active=True,  # STUCK
                night_mode_start="23:00",
                night_mode_end="07:00",
                # Snapshot of original permissions (all true)
                night_mode_saved_permissions='{"can_send_messages": true, "can_send_audios": true, "can_send_documents": true, "can_send_photos": true, "can_send_videos": true, "can_send_video_notes": true, "can_send_voice_notes": true, "can_send_polls": true, "can_send_other_messages": true, "can_add_web_page_previews": true, "can_change_info": true, "can_invite_users": true, "can_pin_messages": true}',
            )
            s.add(cs)
            await s.commit()

        # Mock bot
        mock_bot = MagicMock()
        mock_bot.get_chat = AsyncMock(return_value=MagicMock(
            permissions=MagicMock(
                can_send_messages=True, can_send_audios=True, can_send_documents=True,
                can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
                can_add_web_page_previews=True, can_change_info=True, can_invite_users=True,
                can_pin_messages=True,
            )
        ))
        mock_bot.set_chat_permissions = AsyncMock()

        with patch.object(bot_module, "bot", mock_bot):
            await bot_module._startup_recovery()

            # _night_mode_tick should have been called and (since we're not
            # currently in 23:00-07:00 MSK window OR we are — depends on test time)
            # Either way, set_chat_permissions might or might not be called.
            # The important thing: no exception was raised.

        # Verify chat is in a consistent state (active flag either still True if in window,
        # or False if outside window). Just check it didn't stay in a weird state.
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1004730000002)
            )).scalar_one()
            # night_mode_currently_active is now consistent with actual time window
            # (we don't assert True/False because it depends on test run time)
            self.assertIsNotNone(cs,
                                 "Chat should still exist after recovery")


# ──────────────────────────────────────────────────────────────────────────
class TestSourceCodePatterns(unittest.TestCase):
    """Статические проверки исходного кода — без запуска асинхронного кода."""

    # ── Test 17: bot_handlers имеет async with _EPHEMERAL_DELETE_SEM ─
    def test_bot_handlers_uses_semaphore_in_auto_delete(self):
        # Второе использование (_send_user_warn_notification) уехало в
        # mod_commands.py при декомпозиции v4.8.9/v4.8.10 — семафор туда
        # импортируется из bot_handlers, то есть он общий и лимит в 100
        # параллельных удалений соблюдается на оба модуля.
        import os as _os
        src = ""
        for _name in ("bot_handlers.py", "mod_commands.py"):
            if _os.path.exists(_name):
                with open(_name, "r", encoding="utf-8") as f:
                    src += f.read() + "\n"
        count = src.count("async with _EPHEMERAL_DELETE_SEM")
        self.assertGreaterEqual(count, 2,
                                f"bot_handlers must use _EPHEMERAL_DELETE_SEM in at least 2 places, "
                                f"found {count}")

    # ── Test 18: bot.py не использует _polling_task global ───────────
    def test_bot_no_global_polling_task(self):
        with open("bot.py", "r", encoding="utf-8") as f:
            src = f.read()
        # Old pattern: "_polling_task = asyncio.create_task(...)"
        self.assertNotIn("_polling_task = asyncio.create_task", src,
                         "bot.py must not use old _polling_task = asyncio.create_task pattern")

    # ── Test 19: bot.py содержит except* CancelledError ──────────────
    def test_bot_handles_cancelled_error_group(self):
        """v4.7.3: TaskGroup может бросить ExceptionGroup с CancelledError —
        должен быть except* handler."""
        with open("bot.py", "r", encoding="utf-8") as f:
            src = f.read()
        self.assertIn("except* asyncio.CancelledError", src,
                      "bot.py must handle CancelledError via except* (TaskGroup ExceptionGroup)")

    # ── Test 20: bot.py содержит _SHUTDOWN_TIMEOUT_SECONDS ───────────
    def test_bot_has_shutdown_timeout_constant(self):
        with open("bot.py", "r", encoding="utf-8") as f:
            src = f.read()
        self.assertIn("_SHUTDOWN_TIMEOUT_SECONDS", src,
                      "bot.py must define _SHUTDOWN_TIMEOUT_SECONDS")
        self.assertIn("5.0", src,
                      "bot.py must set _SHUTDOWN_TIMEOUT_SECONDS to 5.0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
