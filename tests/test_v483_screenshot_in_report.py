"""v4.8.3: Тесты скриншотов от модератора (фото с caption-командой).

Проверяет:
  • sticker_cache.download_photo_bytes — скачивает largest photo size.
  • bot_handlers._build_screenshot_block — обёртка для Rich Message.
  • bot_handlers._send_report — Details «📷 Скриншот от модератора»
    добавляется только если moderator_screenshot.photo есть.
  • moderator_screenshot=message если message.photo есть, иначе None.

Запуск: python scripts/test_v483_screenshot_in_report.py
"""

import asyncio
import io
import sys
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from PIL import Image  # type: ignore[import-untyped]
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, _PROJECT_ROOT)
_V483_WORK = os.path.join(_PROJECT_ROOT, "v485_work")
if not os.path.isdir(_V483_WORK):
    _V483_WORK = os.path.join(_PROJECT_ROOT, "v483_work")
sys.path.insert(0, _V483_WORK)


def _make_jpeg_bytes(width=200, height=200) -> bytes:
    img = Image.new("RGB", (width, height), (100, 150, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


class TestDownloadPhotoBytes(unittest.TestCase):
    """Тесты sticker_cache.download_photo_bytes."""

    def setUp(self):
        import sticker_cache  # type: ignore
        self.sc = sticker_cache

    def _make_photo_sizes(self, sizes=((160, 160, "s1"), (320, 320, "s2"), (640, 640, "s3"))):
        """Список PhotoSize-объектов (самый большой — последний)."""
        photo_sizes = []
        for w, h, fid in sizes:
            ps = MagicMock()
            ps.width = w
            ps.height = h
            ps.file_id = fid
            photo_sizes.append(ps)
        return photo_sizes

    def _make_bot_with_jpeg(self, data: bytes):
        bot = MagicMock()
        tg_file = MagicMock()
        tg_file.file_path = "photos/file.jpg"

        async def _download(*args, **kwargs):
            dest = kwargs.get("destination")
            if dest is not None:
                dest.write(data)
            return dest

        bot.get_file = AsyncMock(return_value=tg_file)
        bot.download = AsyncMock(side_effect=_download)
        return bot

    def test_download_largest_photo_size(self):
        """Берётся последний (самый большой) PhotoSize."""
        jpeg = _make_jpeg_bytes()
        photo_sizes = self._make_photo_sizes()
        bot = self._make_bot_with_jpeg(jpeg)

        buf, err = asyncio.run(self.sc.download_photo_bytes(bot, photo_sizes))

        self.assertIsNone(err)
        self.assertIsNotNone(buf)
        # Проверяем что get_file вызван с file_id самого большого размера.
        bot.get_file.assert_called_once_with("s3")

    def test_empty_photo_sizes_error(self):
        """Пустой список photo_sizes → ошибка."""
        bot = MagicMock()
        buf, err = asyncio.run(self.sc.download_photo_bytes(bot, []))
        self.assertIsNone(buf)
        self.assertIn("no photo", err.lower())

    def test_download_failure_graceful(self):
        """Если bot.get_file падает — возвращаем (None, error)."""
        bot = MagicMock()
        bot.get_file = AsyncMock(side_effect=Exception("network error"))
        photo_sizes = self._make_photo_sizes(sizes=((100, 100, "x"),))

        buf, err = asyncio.run(self.sc.download_photo_bytes(bot, photo_sizes))
        self.assertIsNone(buf)
        self.assertIsNotNone(err)


class TestBuildScreenshotBlock(unittest.TestCase):
    """Тесты bot_handlers._build_screenshot_block."""

    def setUp(self):
        if not HAVE_PIL:
            self.skipTest("Pillow not installed")
        try:
            import bot_handlers as bh  # type: ignore
            self.bh = bh
        except ImportError:
            self.skipTest("aiogram not installed")

    def test_screenshot_returns_photo_block(self):
        """Скриншот → InputRichBlockPhoto."""
        jpeg = _make_jpeg_bytes()
        ps = MagicMock()
        ps.file_id = "shot1"
        photo_sizes = [ps]

        bot = MagicMock()
        tg_file = MagicMock()
        tg_file.file_path = "photos/shot.jpg"

        async def _download(*args, **kwargs):
            dest = kwargs.get("destination")
            if dest is not None:
                dest.write(jpeg)
            return dest

        bot.get_file = AsyncMock(return_value=tg_file)
        bot.download = AsyncMock(side_effect=_download)

        block, err = asyncio.run(self.bh._build_screenshot_block(bot, photo_sizes))

        self.assertIsNone(err)
        self.assertIsNotNone(block)
        # InputRichBlockPhoto имеет атрибут photo.
        self.assertTrue(hasattr(block, "photo"))

    def test_download_failure_returns_none(self):
        """Если скачивание падает — (None, error)."""
        ps = MagicMock()
        ps.file_id = "shot_fail"
        photo_sizes = [ps]

        bot = MagicMock()
        bot.get_file = AsyncMock(side_effect=Exception("404"))

        block, err = asyncio.run(self.bh._build_screenshot_block(bot, photo_sizes))
        self.assertIsNone(block)
        self.assertIsNotNone(err)


class TestSendReportScreenshotIntegration(unittest.TestCase):
    """Тесты _send_report — скриншот в отдельном Details."""

    def setUp(self):
        if not HAVE_PIL:
            self.skipTest("Pillow not installed")
        try:
            import bot_handlers as bh  # type: ignore
            self.bh = bh
        except ImportError:
            self.skipTest("aiogram not installed")

    def test_screenshot_adds_separate_details(self):
        """Если moderator_screenshot.photo есть — добавляется Details «📷 Скриншот»."""
        jpeg = _make_jpeg_bytes()

        # v4.8.6: aiogram 3.30+ strict-валидирует blocks в InputRichBlockDetails.
        # MagicMock больше не проходит — используем реальный InputRichBlockPhoto.
        from aiogram.types import InputRichBlockPhoto, InputMediaPhoto, BufferedInputFile
        fake_block = InputRichBlockPhoto(
            photo=InputMediaPhoto(media=BufferedInputFile(jpeg, filename="screenshot.jpg"))
        )
        with patch.object(self.bh, "_build_screenshot_block", AsyncMock(return_value=(fake_block, None))):
            with patch.object(self.bh, "_get_report_chat_id", AsyncMock(return_value=-100999)):
                settings = MagicMock()
                settings.hashtag = "#test"
                with patch.object(self.bh, "_get_chat_settings", AsyncMock(return_value=settings)):
                    bot = MagicMock()
                    captured = {}

                    async def _capture_send(*args, **kwargs):
                        captured["rich_message"] = kwargs.get("rich_message") or (args[1] if len(args) > 1 else None)

                    bot.send_rich_message = AsyncMock(side_effect=_capture_send)
                    bot.send_sticker = AsyncMock()

                    session = MagicMock()
                    session.__aenter__ = AsyncMock(return_value=session)
                    session.__aexit__ = AsyncMock(return_value=None)
                    with patch.object(self.bh, "async_session", return_value=session):
                        target = MagicMock()
                        target.id = 12345
                        target.username = "spammer"
                        target.first_name = "Spammer"
                        target.last_name = ""

                        mod = MagicMock()
                        mod.id = 999
                        mod.username = "mod"
                        mod.first_name = "Mod"

                        # Мок moderator_screenshot с photo.
                        screenshot_msg = MagicMock()
                        screenshot_msg.photo = [MagicMock(file_id="shot123")]

                        asyncio.run(self.bh._send_report(
                            bot=bot,
                            chat_id=-100123,
                            target=target,
                            action_type="ban",
                            reason="Тест",
                            mod=mod,
                            reply_to_message=None,
                            moderator_screenshot=screenshot_msg,
                        ))

                # Проверяем что rich_message отправлен.
                self.assertIn("rich_message", captured)
                rm = captured["rich_message"]

                # Ищем Details с summary="📷 Скриншот от модератора".
                found_screenshot_details = False
                for block in rm.blocks:
                    if hasattr(block, "summary") and "Скриншот" in str(block.summary):
                        found_screenshot_details = True
                        break
                self.assertTrue(found_screenshot_details,
                                "Details «📷 Скриншот от модератора» должен быть в blocks")

    def test_no_screenshot_no_details(self):
        """Если moderator_screenshot=None — Details «📷 Скриншот» НЕ добавляется."""
        with patch.object(self.bh, "_get_report_chat_id", AsyncMock(return_value=-100999)):
            settings = MagicMock()
            settings.hashtag = "#test"
            with patch.object(self.bh, "_get_chat_settings", AsyncMock(return_value=settings)):
                bot = MagicMock()
                captured = {}

                async def _capture_send(*args, **kwargs):
                    captured["rich_message"] = kwargs.get("rich_message") or (args[1] if len(args) > 1 else None)

                bot.send_rich_message = AsyncMock(side_effect=_capture_send)
                bot.send_sticker = AsyncMock()

                session = MagicMock()
                session.__aenter__ = AsyncMock(return_value=session)
                session.__aexit__ = AsyncMock(return_value=None)
                with patch.object(self.bh, "async_session", return_value=session):
                    target = MagicMock()
                    target.id = 12345
                    target.username = "spammer"
                    target.first_name = "Spammer"
                    target.last_name = ""

                    mod = MagicMock()
                    mod.id = 999
                    mod.username = "mod"
                    mod.first_name = "Mod"

                    asyncio.run(self.bh._send_report(
                        bot=bot,
                        chat_id=-100123,
                        target=target,
                        action_type="ban",
                        reason="Тест",
                        mod=mod,
                        reply_to_message=None,
                        moderator_screenshot=None,  # без скриншота
                    ))

            rm = captured["rich_message"]
            for block in rm.blocks:
                if hasattr(block, "summary"):
                    self.assertNotIn("Скриншот", str(block.summary))


def run_all_tests():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestDownloadPhotoBytes))
    suite.addTests(loader.loadTestsFromTestCase(TestBuildScreenshotBlock))
    suite.addTests(loader.loadTestsFromTestCase(TestSendReportScreenshotIntegration))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == "__main__":
    ok = run_all_tests()
    sys.exit(0 if ok else 1)
