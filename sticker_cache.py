"""v4.8.3: Стикеры и фото для inline-блоков в Rich Messages.

Все функции возвращают BytesIO — ничего не пишется на диск.
После использования BytesIO уничтожается GC, хостинг не засоряется.

Поддерживаемые типы стикеров:
  • Static (WebP)   → PNG через Pillow → InputRichBlockPhoto
  • Video (WebM)    → как есть → InputRichBlockAnimation
  • Animated (TGS)  → PNG через rlottie-python (если установлен)
                     иначе fallback: caller сам решает что делать
                     (обычно — отдельный send_sticker после rich-отчёта).

ТGS-конвертация — через rlottie-python (~5MB в Docker-образе). Если
rlottie не установлен или падает на конкретном стикере — функции
возвращают (None, error_message), caller fallback'ит на send_sticker.
"""

from __future__ import annotations

import logging
from io import BytesIO
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram import types
    from aiogram.client.bot import Bot

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
#  rlottie — опциональная зависимость для TGS → PNG конвертации
# ──────────────────────────────────────────────────────────────────────────
try:
    # rlottie-python — precompiled биндинги к библиотеке rlottie (C++).
    # В отличие от lottie+cairosvg (~50MB), rlottie-python весит ~5MB.
    # Может не работать на некоторых платформах (precompiled wheels не под
    # все Linux-дистрибутивы). Если не импортируется — fallback на send_sticker.
    from rlottie_python import LottieAnimation  # type: ignore[import-untyped]
    _HAVE_RLOTTIE = True
    _RLOTTIE_IMPORT_ERROR: str | None = None
except ImportError as e:
    _HAVE_RLOTTIE = False
    _RLOTTIE_IMPORT_ERROR = str(e)
    logger.info(
        "v4.8.3: rlottie-python не установлен — TGS-стикеры будут отправлены "
        "fallback'ом (отдельным send_sticker после rich-отчёта). "
        "Install: pip install rlottie-python. Import error: %s",
        _RLOTTIE_IMPORT_ERROR,
    )


# ──────────────────────────────────────────────────────────────────────────
#  Pillow — для WebP → PNG конвертации
# ──────────────────────────────────────────────────────────────────────────
try:
    from PIL import Image  # type: ignore[import-untyped]
    _HAVE_PILLOW = True
except ImportError as e:
    _HAVE_PILLOW = False
    logger.warning(
        "v4.8.3: Pillow не установлен — WebP-стикеры нельзя конвертировать в PNG. "
        "Install: pip install Pillow. Import error: %s",
        e,
    )


# ──────────────────────────────────────────────────────────────────────────
#  Утилиты скачивания
# ──────────────────────────────────────────────────────────────────────────

async def _download_file_to_bytes(
    bot: "Bot",
    file_id: str,
) -> BytesIO | None:
    """Скачивает файл по file_id в BytesIO (в памяти, без диска).

    Возвращает None если скачать не удалось (с логированием warning).
    """
    try:
        tg_file = await bot.get_file(file_id)
    except Exception as e:
        logger.warning("get_file failed for file_id=%s: %s", file_id, e)
        return None

    buf = BytesIO()
    try:
        # download_to_memory в aiogram 3.x deprecated/не существует.
        # bot.download(...) в aiogram 3.x принимает destination (file-like).
        # Если передать BytesIO — пишется в него.
        await bot.download(tg_file, destination=buf)
    except Exception as e:
        logger.warning("download failed for file_id=%s: %s", file_id, e)
        return None

    buf.seek(0)
    return buf


# ──────────────────────────────────────────────────────────────────────────
#  Стикеры
# ──────────────────────────────────────────────────────────────────────────

async def download_sticker_as_png(
    bot: "Bot",
    sticker: "types.Sticker",
) -> tuple[BytesIO | None, str | None]:
    """Скачивает статический WebP-стикер, конвертирует в PNG через Pillow.

    Returns:
        (BytesIO с PNG, None) при успехе.
        (None, error_message) при неудаче.
    """
    if sticker.is_animated:
        # TGS — это не WebP, нужна другая конвертация (см. download_tgs_as_png).
        return None, "sticker is animated (TGS), use download_tgs_as_png()"
    if sticker.is_video:
        # WebM — это не WebP, отдаём как есть для animation-блока.
        return None, "sticker is video (WebM), use download_sticker_as_webm()"

    if not _HAVE_PILLOW:
        return None, "Pillow not installed, cannot convert WebP to PNG"

    buf = await _download_file_to_bytes(bot, sticker.file_id)
    if buf is None:
        return None, "failed to download sticker file"

    try:
        img = Image.open(buf)
        # WebP может быть RGBA (с прозрачностью) — сохраняем как RGBA PNG.
        if img.mode not in ("RGBA", "RGB"):
            img = img.convert("RGBA")
        png_buf = BytesIO()
        img.save(png_buf, format="PNG")
        png_buf.seek(0)
        return png_buf, None
    except Exception as e:
        logger.warning("Pillow failed to convert WebP→PNG: %s", e)
        return None, f"Pillow conversion failed: {e}"
    finally:
        buf.close()


async def download_sticker_as_webm(
    bot: "Bot",
    sticker: "types.Sticker",
) -> tuple[BytesIO | None, str | None]:
    """Скачивает video-стикер (WebM) как есть, для InputRichBlockAnimation.

    Returns:
        (BytesIO с WebM, None) при успехе.
        (None, error_message) при неудаче.
    """
    if not sticker.is_video:
        return None, "sticker is not video (WebM)"

    buf = await _download_file_to_bytes(bot, sticker.file_id)
    if buf is None:
        return None, "failed to download sticker file"
    return buf, None


async def download_tgs_as_png(
    bot: "Bot",
    sticker: "types.Sticker",
) -> tuple[BytesIO | None, str | None]:
    """Скачивает анимированный TGS-стикер, конвертирует в PNG через rlottie.

    TGS — это gzipped Lottie JSON. rlottie-python рендерит его в PNG.

    Returns:
        (BytesIO с PNG, None) при успехе.
        (None, error_message) при неудаче (включая «rlottie не установлен»).
    """
    if not sticker.is_animated:
        return None, "sticker is not animated (TGS)"

    if not _HAVE_RLOTTIE:
        return None, (
            "rlottie-python not installed, cannot convert TGS to PNG. "
            "Install: pip install rlottie-python"
        )

    buf = await _download_file_to_bytes(bot, sticker.file_id)
    if buf is None:
        return None, "failed to download sticker file"

    try:
        # TGS — это gzipped JSON. LottieAnimation принимает raw bytes TGS.
        # rlottie-python должен сам разжимать.
        anim = LottieAnimation.from_tgs_bytes(buf.read())
        try:
            # Рендерим первый кадр (TGS-стикеры обычно зацикленные анимации).
            # api: anim.save_animation('out.gif') — для GIF.
            # Для PNG — render_pillow() если есть, либо save_frame().
            # Реальный API rlottie-python: anim.render_animation(width, height)
            # возвращает list[Pillow.Image]. Берём первый кадр.
            frames = anim.render_animation(width=512, height=512)
            if not frames:
                return None, "rlottie returned no frames"
            first_frame = frames[0]  # PIL.Image
            if first_frame.mode not in ("RGBA", "RGB"):
                first_frame = first_frame.convert("RGBA")
            png_buf = BytesIO()
            first_frame.save(png_buf, format="PNG")
            png_buf.seek(0)
            return png_buf, None
        finally:
            anim.close() if hasattr(anim, "close") else None
    except Exception as e:
        logger.warning("rlottie failed to convert TGS→PNG: %s", e)
        return None, f"rlottie conversion failed: {e}"
    finally:
        buf.close()


async def download_sticker_for_rich_message(
    bot: "Bot",
    sticker: "types.Sticker",
) -> tuple[BytesIO | None, str | None, str | None]:
    """Универсальный скачатель стикера для rich message.

    Определяет тип стикера и вызывает соответствующую функцию.

    Returns:
        (BytesIO, format, None) — где format: 'png' или 'webm'.
        (None, None, error_message) — при неудаче.
    """
    if sticker.is_animated:
        # TGS → PNG через rlottie (если есть).
        png_buf, err = await download_tgs_as_png(bot, sticker)
        if png_buf is not None:
            return png_buf, "png", None
        return None, None, err
    if sticker.is_video:
        # WebM как есть.
        webm_buf, err = await download_sticker_as_webm(bot, sticker)
        if webm_buf is not None:
            return webm_buf, "webm", None
        return None, None, err
    # Static WebP → PNG.
    png_buf, err = await download_sticker_as_png(bot, sticker)
    if png_buf is not None:
        return png_buf, "png", None
    return None, None, err


# ──────────────────────────────────────────────────────────────────────────
#  Фото (для скриншотов модератора)
# ──────────────────────────────────────────────────────────────────────────

async def download_photo_bytes(
    bot: "Bot",
    photo_sizes: list,
) -> tuple[BytesIO | None, str | None]:
    """Скачивает largest photo size как есть (обычно JPEG).

    Args:
        photo_sizes: list of PhotoSize (message.photo). Берём последний —
                     самый большой.

    Returns:
        (BytesIO, None) — успех.
        (None, error_message) — неудача.
    """
    if not photo_sizes:
        return None, "no photo sizes provided"

    # photo_sizes отсортированы по возрастанию размера — последний largest.
    largest = photo_sizes[-1]
    buf = await _download_file_to_bytes(bot, largest.file_id)
    if buf is None:
        return None, "failed to download photo"
    return buf, None
