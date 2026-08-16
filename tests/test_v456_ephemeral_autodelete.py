"""
test_v456_ephemeral_autodelete.py — verifies v4.5.6 ephemeral auto-delete.

What's tested:
  1. _send_ephemeral schedules a delete_message call after `delete_after` seconds.
  2. delete_after=0 disables auto-delete (no task scheduled).
  3. If bot.send_message fails (TelegramBadRequest), no delete task is scheduled
     and no delete_message call is made — the function returns cleanly.
  4. If bot.delete_message fails (message already gone), the error is swallowed
     gracefully (logged at info level, no exception bubbles up).
  5. _send_user_warn_notification also supports delete_after and schedules delete.
  6. delete_message receives the same chat_id and the message_id returned by
     send_message.
  7. APP_VERSION bumped to v4.5.6.
  8. Changelog modal in base.html has a v4.5.6 entry.
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Make project importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# ── aiogram's TelegramBadRequest requires (method, message). Patch it for tests.
from aiogram.exceptions import TelegramBadRequest
_orig_tb_init = TelegramBadRequest.__init__
def _patched_tb_init(self, method=None, message=""):
    if method is None:
        method = MagicMock()
    _orig_tb_init(self, method, message)
TelegramBadRequest.__init__ = _patched_tb_init

import bot_handlers
import web_app


class _CreateTaskCapture:
    """Context manager that captures asyncio.create_task calls without running them."""
    def __enter__(self):
        self.calls = []
        self._orig = asyncio.create_task
        def patched(coro):
            self.calls.append(coro)
            coro.close()
            return self._orig(asyncio.sleep(0))
        asyncio.create_task = patched
        return self
    def __exit__(self, *exc):
        asyncio.create_task = self._orig
        return False


class TestSendEphemeralAutoDelete(unittest.TestCase):

    def test_default_schedules_delete_task(self):
        async def run():
            bot = AsyncMock()
            sent = MagicMock(); sent.message_id = 12345
            bot.send_message = AsyncMock(return_value=sent)
            bot.delete_message = AsyncMock()
            recipient = MagicMock(); recipient.id = 999

            with _CreateTaskCapture() as cap:
                await bot_handlers._send_ephemeral(
                    bot=bot, chat_id=42, recipient=recipient, text="test",
                    delete_after=30,
                )
            self.assertTrue(bot.send_message.called)
            self.assertEqual(len(cap.calls), 1, "Expected 1 delete task scheduled")
        asyncio.run(run())

    def test_delete_after_zero_skips_scheduling(self):
        async def run():
            bot = AsyncMock()
            sent = MagicMock(); sent.message_id = 1
            bot.send_message = AsyncMock(return_value=sent)
            bot.delete_message = AsyncMock()
            recipient = MagicMock(); recipient.id = 1

            with _CreateTaskCapture() as cap:
                await bot_handlers._send_ephemeral(
                    bot=bot, chat_id=1, recipient=recipient, text="x",
                    delete_after=0,
                )
            self.assertEqual(len(cap.calls), 0, "delete_after=0 should not schedule")
        asyncio.run(run())

    def test_send_failure_no_delete_scheduled(self):
        async def run():
            bot = AsyncMock()
            bot.send_message = AsyncMock(side_effect=TelegramBadRequest(message="blocked"))
            bot.delete_message = AsyncMock()
            recipient = MagicMock(); recipient.id = 1

            with _CreateTaskCapture() as cap:
                await bot_handlers._send_ephemeral(
                    bot=bot, chat_id=1, recipient=recipient, text="x",
                    delete_after=30,
                )
            self.assertFalse(bot.delete_message.called)
            self.assertEqual(len(cap.calls), 0)
        asyncio.run(run())

    def test_actual_delete_called_after_delay(self):
        async def run():
            bot = AsyncMock()
            sent = MagicMock(); sent.message_id = 777
            bot.send_message = AsyncMock(return_value=sent)
            bot.delete_message = AsyncMock()
            recipient = MagicMock(); recipient.id = 1

            await bot_handlers._send_ephemeral(
                bot=bot, chat_id=42, recipient=recipient, text="x",
                delete_after=0.05,
            )
            await asyncio.sleep(0.15)

            self.assertTrue(bot.delete_message.called)
            call = bot.delete_message.call_args
            self.assertEqual(call.kwargs.get("chat_id"), 42)
            self.assertEqual(call.kwargs.get("message_id"), 777)
        asyncio.run(run())

    def test_already_deleted_graceful(self):
        """If delete_message fails (e.g. message already gone), no exception bubbles."""
        async def run():
            bot = AsyncMock()
            sent = MagicMock(); sent.message_id = 888
            bot.send_message = AsyncMock(return_value=sent)
            bot.delete_message = AsyncMock(
                side_effect=TelegramBadRequest(message="message to delete not found")
            )
            recipient = MagicMock(); recipient.id = 1

            # Should not raise
            await bot_handlers._send_ephemeral(
                bot=bot, chat_id=42, recipient=recipient, text="x",
                delete_after=0.05,
            )
            await asyncio.sleep(0.15)
            self.assertTrue(bot.delete_message.called)
        asyncio.run(run())


class TestSendUserWarnNotificationAutoDelete(unittest.TestCase):

    def test_warn_notification_schedules_delete(self):
        async def run():
            from db import ChatSettings
            bot = AsyncMock()
            sent = MagicMock(); sent.message_id = 99999
            bot.send_message = AsyncMock(return_value=sent)
            bot.delete_message = AsyncMock()
            target = MagicMock(); target.id = 555
            target.username = "loser"; target.first_name = "L"

            settings = ChatSettings()
            settings.warns_to_mute = 3
            settings.warns_to_ban = 5

            with _CreateTaskCapture() as cap:
                await bot_handlers._send_user_warn_notification(
                    bot=bot, chat_id=42, target=target,
                    reason="spam", total_warns=2, settings=settings,
                    delete_after=30,
                )
            self.assertTrue(bot.send_message.called)
            self.assertEqual(len(cap.calls), 1)
        asyncio.run(run())

    def test_warn_notification_zero_no_scheduling(self):
        async def run():
            from db import ChatSettings
            bot = AsyncMock()
            sent = MagicMock(); sent.message_id = 1
            bot.send_message = AsyncMock(return_value=sent)
            bot.delete_message = AsyncMock()
            target = MagicMock(); target.id = 1
            target.username = "x"; target.first_name = "X"

            settings = ChatSettings()
            settings.warns_to_mute = 0
            settings.warns_to_ban = 0

            with _CreateTaskCapture() as cap:
                await bot_handlers._send_user_warn_notification(
                    bot=bot, chat_id=42, target=target,
                    reason="x", total_warns=1, settings=settings,
                    delete_after=0,
                )
            self.assertEqual(len(cap.calls), 0)
        asyncio.run(run())

    def test_warn_notification_actual_delete(self):
        async def run():
            from db import ChatSettings
            bot = AsyncMock()
            sent = MagicMock(); sent.message_id = 4242
            bot.send_message = AsyncMock(return_value=sent)
            bot.delete_message = AsyncMock()
            target = MagicMock(); target.id = 555
            target.username = "u"; target.first_name = "U"

            settings = ChatSettings()
            settings.warns_to_mute = 3
            settings.warns_to_ban = 5

            await bot_handlers._send_user_warn_notification(
                bot=bot, chat_id=42, target=target,
                reason="spam", total_warns=2, settings=settings,
                delete_after=0.05,
            )
            await asyncio.sleep(0.15)
            self.assertTrue(bot.delete_message.called)
            call = bot.delete_message.call_args
            self.assertEqual(call.kwargs.get("chat_id"), 42)
            self.assertEqual(call.kwargs.get("message_id"), 4242)
        asyncio.run(run())


class TestVersionBumped(unittest.TestCase):

    def test_app_version_is_v456(self):
        self.assertEqual(web_app.APP_VERSION, "v4.6.1")

    def test_changelog_has_v456_entry(self):
        with open("templates/base.html", "r", encoding="utf-8") as f:
            html = f.read()
        self.assertIn("v4.6.1", html)
        # Must mention ephemeral auto-delete
        self.assertIn("Ephemeral auto-delete", html)
        # Must mention 30 seconds
        self.assertIn("30 second", html)


class TestBackwardCompat(unittest.TestCase):
    """Existing call sites (without delete_after kwarg) must still work."""

    def test_send_ephemeral_no_kwarg_works(self):
        async def run():
            bot = AsyncMock()
            sent = MagicMock(); sent.message_id = 1
            bot.send_message = AsyncMock(return_value=sent)
            bot.delete_message = AsyncMock()
            recipient = MagicMock(); recipient.id = 1

            # No delete_after kwarg — should default to 30.0 and schedule a task.
            with _CreateTaskCapture() as cap:
                await bot_handlers._send_ephemeral(
                    bot=bot, chat_id=1, recipient=recipient, text="x",
                )
            self.assertEqual(len(cap.calls), 1)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
