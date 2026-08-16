"""v4.8.3: Тесты интеграции стикеров inline в Rich Messages.

Проверяет:
  • sticker_cache.py: download_sticker_as_png (WebP→PNG через Pillow),
    download_sticker_as_webm (WebM как есть), download_tgs_as_png (TGS→PNG
    через rlottie — с fallback если не установлен).
  • bot_handlers._build_sticker_block — обёртка над sticker_cache.
  • bot_handlers._send_report — стикер inline в Details «📎 Сообщение юзера»,
    fallback на send_sticker если inline не сработал (TGS без rlottie).
  • Стикерпак-нотификация (sticker_pack_info) в List.

Запуск: python scripts/test_v483_sticker_inline_in_report.py
"""

import asyncio
import io
import sys
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# PIL может не быть установлен — тесты с PIL будут skipped.
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


def _make_webp_bytes(width=100, height=100) -> bytes:
    """Создаёт валидный WebP-файл в памяти."""
    img = Image.new("RGBA", (width, height), (255, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="WEBP")
    return buf.getvalue()


def _make_png_bytes(width=100, height=100) -> bytes:
    img = Image.new("RGBA", (width, height), (0, 255, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestStickerCacheWebP(unittest.TestCase):
    """Тесты download_sticker_as_png (static WebP → PNG)."""

    def setUp(self):
        if not HAVE_PIL:
            self.skipTest("Pillow not installed")
        import sticker_cache  # type: ignore
        self.sc = sticker_cache

    def _make_sticker(self, is_animated=False, is_video=False, file_id="abc123"):
        sticker = MagicMock()
        sticker.is_animated = is_animated
        sticker.is_video = is_video
        sticker.file_id = file_id
        return sticker

    def _make_bot_with_webp(self, webp_bytes: bytes):
        bot = MagicMock()
        tg_file = MagicMock()
        tg_file.file_path = "stickers/file.webp"

        async def _download(*args, **kwargs):
            # aiogram 3.x: bot.download(file, destination=buf)
            dest = kwargs.get("destination")
            if dest is not None:
                dest.write(webp_bytes)
            return dest

        bot.get_file = AsyncMock(return_value=tg_file)
        bot.download = AsyncMock(side_effect=_download)
        return bot

    def test_webp_to_png_success(self):
        """WebP-стикер → PNG-BytesIO через Pillow."""
        webp = _make_webp_bytes()
        sticker = self._make_sticker(is_animated=False, is_video=False)
        bot = self._make_bot_with_webp(webp)

        png_buf, err = asyncio.run(self.sc.download_sticker_as_png(bot, sticker))

        self.assertIsNone(err, f"expected no error, got: {err}")
        self.assertIsNotNone(png_buf)
        # Проверяем что это валидный PNG.
        png_buf.seek(0)
        img = Image.open(png_buf)
        self.assertEqual(img.format, "PNG")

    def test_animated_sticker_rejected_in_png_func(self):
        """Анимированный стикер в download_sticker_as_png → ошибка."""
        sticker = self._make_sticker(is_animated=True)
        bot = MagicMock()
        png_buf, err = asyncio.run(self.sc.download_sticker_as_png(bot, sticker))
        self.assertIsNone(png_buf)
        self.assertIn("animated", err.lower())

    def test_video_sticker_rejected_in_png_func(self):
        """Video-стикер в download_sticker_as_png → ошибка."""
        sticker = self._make_sticker(is_video=True)
        bot = MagicMock()
        png_buf, err = asyncio.run(self.sc.download_sticker_as_png(bot, sticker))
        self.assertIsNone(png_buf)
        self.assertIn("video", err.lower())

    def test_pillow_not_installed_graceful(self):
        """Если Pillow не установлен — graceful error."""
        sticker = self._make_sticker()
        bot = self._make_bot_with_webp(_make_webp_bytes())

        with patch.object(self.sc, "_HAVE_PILLOW", False):
            png_buf, err = asyncio.run(self.sc.download_sticker_as_png(bot, sticker))
        self.assertIsNone(png_buf)
        self.assertIn("Pillow", err)


class TestStickerCacheWebM(unittest.TestCase):
    """Тесты download_sticker_as_webm (video WebM как есть)."""

    def setUp(self):
        import sticker_cache  # type: ignore
        self.sc = sticker_cache

    def _make_video_sticker(self, file_id="vid456"):
        sticker = MagicMock()
        sticker.is_animated = False
        sticker.is_video = True
        sticker.file_id = file_id
        return sticker

    def _make_bot_with_webm(self, webm_bytes: bytes):
        bot = MagicMock()
        tg_file = MagicMock()
        tg_file.file_path = "stickers/file.webm"

        async def _download(*args, **kwargs):
            dest = kwargs.get("destination")
            if dest is not None:
                dest.write(webm_bytes)
            return dest

        bot.get_file = AsyncMock(return_value=tg_file)
        bot.download = AsyncMock(side_effect=_download)
        return bot

    def test_webm_download_success(self):
        webm = b"\x1a\x45\xdf\xa3dummy_webm_data"
        sticker = self._make_video_sticker()
        bot = self._make_bot_with_webm(webm)

        buf, err = asyncio.run(self.sc.download_sticker_as_webm(bot, sticker))

        self.assertIsNone(err)
        self.assertIsNotNone(buf)
        buf.seek(0)
        self.assertEqual(buf.read(), webm)

    def test_non_video_sticker_rejected(self):
        sticker = MagicMock()
        sticker.is_video = False
        bot = MagicMock()
        buf, err = asyncio.run(self.sc.download_sticker_as_webm(bot, sticker))
        self.assertIsNone(buf)
        self.assertIn("not video", err)


class TestStickerCacheTGS(unittest.TestCase):
    """Тесты download_tgs_as_png (animated TGS → PNG через rlottie)."""

    def setUp(self):
        import sticker_cache  # type: ignore
        self.sc = sticker_cache

    def _make_tgs_sticker(self, file_id="tgs789"):
        sticker = MagicMock()
        sticker.is_animated = True
        sticker.is_video = False
        sticker.file_id = file_id
        return sticker

    def test_tgs_without_rlottie_graceful_error(self):
        """Если rlottie не установлен — ошибка, caller fallback'ит."""
        sticker = self._make_tgs_sticker()
        bot = MagicMock()

        with patch.object(self.sc, "_HAVE_RLOTTIE", False):
            buf, err = asyncio.run(self.sc.download_tgs_as_png(bot, sticker))

        self.assertIsNone(buf)
        self.assertIn("rlottie", err.lower())

    def test_non_animated_sticker_rejected_in_tgs_func(self):
        sticker = MagicMock()
        sticker.is_animated = False
        bot = MagicMock()
        buf, err = asyncio.run(self.sc.download_tgs_as_png(bot, sticker))
        self.assertIsNone(buf)
        self.assertIn("not animated", err)


class TestDownloadStickerForRichMessage(unittest.TestCase):
    """Тесты универсальной функции download_sticker_for_rich_message."""

    def setUp(self):
        if not HAVE_PIL:
            self.skipTest("Pillow not installed")
        import sticker_cache  # type: ignore
        self.sc = sticker_cache

    def _make_static_sticker(self):
        s = MagicMock()
        s.is_animated = False
        s.is_video = False
        s.file_id = "static123"
        return s

    def _make_video_sticker(self):
        s = MagicMock()
        s.is_animated = False
        s.is_video = True
        s.file_id = "video123"
        return s

    def _make_bot_with_data(self, data: bytes):
        bot = MagicMock()
        tg_file = MagicMock()
        tg_file.file_path = "stickers/file"

        async def _download(*args, **kwargs):
            dest = kwargs.get("destination")
            if dest is not None:
                dest.write(data)
            return dest

        bot.get_file = AsyncMock(return_value=tg_file)
        bot.download = AsyncMock(side_effect=_download)
        return bot

    def test_static_returns_png_format(self):
        webp = _make_webp_bytes()
        sticker = self._make_static_sticker()
        bot = self._make_bot_with_data(webp)

        buf, fmt, err = asyncio.run(self.sc.download_sticker_for_rich_message(bot, sticker))

        self.assertIsNone(err)
        self.assertEqual(fmt, "png")
        self.assertIsNotNone(buf)

    def test_video_returns_webm_format(self):
        webm = b"dummy_webm"
        sticker = self._make_video_sticker()
        bot = self._make_bot_with_data(webm)

        buf, fmt, err = asyncio.run(self.sc.download_sticker_for_rich_message(bot, sticker))

        self.assertIsNone(err)
        self.assertEqual(fmt, "webm")
        self.assertIsNotNone(buf)


class TestBuildStickerBlock(unittest.TestCase):
    """Тесты bot_handlers._build_sticker_block — обёртка над sticker_cache."""

    def setUp(self):
        if not HAVE_PIL:
            self.skipTest("Pillow not installed")
        try:
            import bot_handlers as bh  # type: ignore
            self.bh = bh
        except ImportError:
            self.skipTest("aiogram not installed")

    def test_static_sticker_returns_photo_block(self):
        """Static WebP-стикер → InputRichBlockPhoto."""
        webp = _make_webp_bytes()
        sticker = MagicMock()
        sticker.is_animated = False
        sticker.is_video = False
        sticker.file_id = "test123"

        bot = MagicMock()
        tg_file = MagicMock()
        tg_file.file_path = "stickers/file"

        async def _download(*args, **kwargs):
            dest = kwargs.get("destination")
            if dest is not None:
                dest.write(webp)
            return dest

        bot.get_file = AsyncMock(return_value=tg_file)
        bot.download = AsyncMock(side_effect=_download)

        block, err = asyncio.run(self.bh._build_sticker_block(bot, sticker))

        self.assertIsNone(err)
        self.assertIsNotNone(block)
        # Проверяем что это InputRichBlockPhoto (имеет атрибут photo).
        self.assertTrue(hasattr(block, "photo"))

    def test_tgs_without_rlottie_returns_none(self):
        """TGS-стикер без rlottie → (None, error) — caller fallback'ит."""
        sticker = MagicMock()
        sticker.is_animated = True
        sticker.is_video = False
        sticker.file_id = "tgs123"
        bot = MagicMock()

        # Патчим _HAVE_RLOTTIE в sticker_cache
        import sticker_cache  # type: ignore
        with patch.object(sticker_cache, "_HAVE_RLOTTIE", False):
            block, err = asyncio.run(self.bh._build_sticker_block(bot, sticker))

        self.assertIsNone(block)
        self.assertIsNotNone(err)


class TestSendReportStickerInline(unittest.TestCase):
    """Тесты _send_report — стикер inline в Details.

    Эти тесты проверяют интеграцию: что _send_report использует
    _build_sticker_block и не вызывает send_sticker (если inline OK).
    """

    def setUp(self):
        if not HAVE_PIL:
            self.skipTest("Pillow not installed")
        try:
            import bot_handlers as bh  # type: ignore
            self.bh = bh
        except ImportError:
            self.skipTest("aiogram not installed")

    def test_sticker_inline_no_send_sticker_fallback(self):
        """Если стикер встроен inline — send_sticker НЕ вызывается."""
        # v4.8.6: aiogram 3.30+ strict-валидирует blocks в InputRichBlockDetails.
        # MagicMock больше не проходит — используем реальный InputRichBlockPhoto.
        from aiogram.types import InputRichBlockPhoto, InputMediaPhoto, BufferedInputFile
        fake_block = InputRichBlockPhoto(
            photo=InputMediaPhoto(media=BufferedInputFile(b"\x89PNG\r\n\x1a\n fake", filename="sticker.png"))
        )
        with patch.object(self.bh, "_build_sticker_block", AsyncMock(return_value=(fake_block, None))):
            # Мокаем _get_report_chat_id — возвращаем валидный ID.
            with patch.object(self.bh, "_get_report_chat_id", AsyncMock(return_value=-100999)):
                # Мокаем _get_chat_settings
                settings = MagicMock()
                settings.hashtag = "#test"
                with patch.object(self.bh, "_get_chat_settings", AsyncMock(return_value=settings)):
                    # Мокаем send_rich_message — успех.
                    bot = MagicMock()
                    bot.send_rich_message = AsyncMock(return_value=MagicMock())
                    bot.send_sticker = AsyncMock()

                    # Мокаем async_session
                    session = MagicMock()
                    session.__aenter__ = AsyncMock(return_value=session)
                    session.__aexit__ = AsyncMock(return_value=None)
                    with patch.object(self.bh, "async_session", return_value=session):
                        # Мокаем _count_warns (не нужен для ban, но вызывается в session).
                        # Подготавливаем reply_to_message со стикером.
                        sticker = MagicMock()
                        sticker.is_animated = False
                        sticker.is_video = False
                        sticker.file_id = "stk123"

                        reply = MagicMock()
                        reply.sticker = sticker
                        reply.text = None
                        reply.caption = None
                        # _get_message_content_desc может возвращать None для стикера.
                        # _build_media_block — не вызывается если sticker есть.

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
                            reply_to_message=reply,
                        ))

                    # send_rich_message был вызван.
                    bot.send_rich_message.assert_called_once()
                    # send_sticker НЕ был вызван — inline сработал.
                    bot.send_sticker.assert_not_called()

    def test_tgs_fallback_calls_send_sticker(self):
        """Если стикер не встроен inline (TGS без rlottie) — send_sticker вызывается."""
        with patch.object(self.bh, "_build_sticker_block", AsyncMock(return_value=(None, "rlottie not installed"))):
            with patch.object(self.bh, "_get_report_chat_id", AsyncMock(return_value=-100999)):
                settings = MagicMock()
                settings.hashtag = "#test"
                with patch.object(self.bh, "_get_chat_settings", AsyncMock(return_value=settings)):
                    bot = MagicMock()
                    bot.send_rich_message = AsyncMock(return_value=MagicMock())
                    bot.send_sticker = AsyncMock()

                    session = MagicMock()
                    session.__aenter__ = AsyncMock(return_value=session)
                    session.__aexit__ = AsyncMock(return_value=None)
                    with patch.object(self.bh, "async_session", return_value=session):
                        sticker = MagicMock()
                        sticker.is_animated = True
                        sticker.is_video = False
                        sticker.file_id = "tgs_fallback_123"

                        reply = MagicMock()
                        reply.sticker = sticker
                        reply.text = None
                        reply.caption = None

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
                            reason="Тест TGS",
                            mod=mod,
                            reply_to_message=reply,
                        ))

                    # send_sticker был вызван с тем же file_id.
                    bot.send_sticker.assert_called_once()
                    args, kwargs = bot.send_sticker.call_args
                    self.assertEqual(kwargs.get("sticker") or args[0], "tgs_fallback_123")


class TestStickerPackNotificationInList(unittest.TestCase):
    """Тесты стикерпак-нотификации в List (через sticker_pack_info)."""

    def setUp(self):
        if not HAVE_PIL:
            self.skipTest("Pillow not installed")
        try:
            import bot_handlers as bh  # type: ignore
            self.bh = bh
        except ImportError:
            self.skipTest("aiogram not installed")

    def test_sticker_pack_newly_added_appears_in_blocks(self):
        """Если sticker_pack_info=(pack_name, True) — в List добавляется пункт."""
        # Мокаем всё нужное для _send_report.
        with patch.object(self.bh, "_get_report_chat_id", AsyncMock(return_value=-100999)):
            settings = MagicMock()
            settings.hashtag = "#test"
            with patch.object(self.bh, "_get_chat_settings", AsyncMock(return_value=settings)):
                bot = MagicMock()
                # Захватываем rich_message для проверки.
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
                        reason="За стикер",
                        mod=mod,
                        reply_to_message=None,  # без reply — только sticker_pack_info
                        sticker_pack_info=("BadPack", True),
                    ))

                # Проверяем что rich_message отправлен.
                self.assertIn("rich_message", captured)
                # Ищем в blocks текст про стикерпак.
                rm = captured["rich_message"]
                all_text = ""
                for block in rm.blocks:
                    # У List есть items, у ListItem есть blocks, у Paragraph есть text.
                    if hasattr(block, "items"):
                        for item in block.items:
                            for sub in item.blocks:
                                if hasattr(sub, "text"):
                                    if isinstance(sub.text, list):
                                        for t in sub.text:
                                            all_text += str(t) if not hasattr(t, "text") else t.text
                                    else:
                                        all_text += str(sub.text)
                self.assertIn("BadPack", all_text)
                self.assertIn("стикерпак забанен", all_text.lower())

    def test_sticker_pack_not_newly_added_not_in_list(self):
        """Если sticker_pack_info=(pack_name, False) — пункт НЕ добавляется."""
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
                        reason="За стикер",
                        mod=mod,
                        reply_to_message=None,
                        sticker_pack_info=("ExistingPack", False),  # already in ban-list
                    ))

                rm = captured["rich_message"]
                all_text = ""
                for block in rm.blocks:
                    if hasattr(block, "items"):
                        for item in block.items:
                            for sub in item.blocks:
                                if hasattr(sub, "text"):
                                    if isinstance(sub.text, list):
                                        for t in sub.text:
                                            all_text += str(t) if not hasattr(t, "text") else t.text
                                    else:
                                        all_text += str(sub.text)
                # ExistingPack не должен появиться в тексте.
                self.assertNotIn("ExistingPack", all_text)


def run_all_tests():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestStickerCacheWebP))
    suite.addTests(loader.loadTestsFromTestCase(TestStickerCacheWebM))
    suite.addTests(loader.loadTestsFromTestCase(TestStickerCacheTGS))
    suite.addTests(loader.loadTestsFromTestCase(TestDownloadStickerForRichMessage))
    suite.addTests(loader.loadTestsFromTestCase(TestBuildStickerBlock))
    suite.addTests(loader.loadTestsFromTestCase(TestSendReportStickerInline))
    suite.addTests(loader.loadTestsFromTestCase(TestStickerPackNotificationInList))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == "__main__":
    ok = run_all_tests()
    sys.exit(0 if ok else 1)
