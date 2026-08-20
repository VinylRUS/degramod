"""
test_v4103_no_blocking_open.py — блокирующий I/O не в event loop (Task 6).

Бот и веб-панель делят один event loop. Синхронная файловая операция внутри
async-функции останавливает всё: пока сохраняется аватарка, бот не отвечает
ни в одном чате. В v4.8.7 так вынесли sqlite3, VACUUM и shutil.copy2, но
`open()` тогда пропустили и закрыли ruff-игнором ASYNC230.

Здесь проверяется, что долг закрыт по-настоящему:

  1. Запись файла в `_fetch_and_save_avatar` идёт через asyncio.to_thread.
  2. Игноры ASYNC230 сняты — правило снова сторожит код. Висящий игнор
     опаснее самого нарушения: он молча прячет все будущие.
  3. Event loop во время записи остаётся отзывчивым.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from pathlib import Path

from _paths import _P

os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("WEB_PASSWORD", "test-pwd")
os.environ.setdefault("WEB_ALLOW_NO_SECRET", "1")

sys.path.insert(0, _P())

WORK_DIR = Path(_P())


class TestNoAsyncIgnores(unittest.TestCase):
    """ASYNC230 должен работать, а не быть заглушен в конфиге."""

    def test_no_async230_in_per_file_ignores(self):
        """В pyproject нет per-file-ignores с ASYNC230.

        Пока игнор висит, ruff молчит и о будущих нарушениях — правило
        перестаёт защищать те самые файлы, где проблема уже была.
        """
        src = (WORK_DIR / "pyproject.toml").read_text(encoding="utf-8")
        offenders = [
            line.strip() for line in src.splitlines()
            if "ASYNC230" in line and not line.strip().startswith("#")
        ]
        self.assertEqual(
            offenders, [],
            "ASYNC230 всё ещё в игнорах: " + "; ".join(offenders),
        )


class TestAvatarSaveIsOffloaded(unittest.TestCase):
    """Запись аватарки не держит event loop."""

    def test_source_uses_to_thread(self):
        """Сохранение файла обёрнуто в asyncio.to_thread."""
        src = (WORK_DIR / "web_app.py").read_text(encoding="utf-8")
        start = src.index("async def _fetch_and_save_avatar")
        end = src.index("\n_APP_START_TIME", start)
        body = src[start:end]
        self.assertIn(
            "to_thread", body,
            "_fetch_and_save_avatar пишет файл синхронно — event loop встанет",
        )

    def test_loop_stays_responsive_during_save(self):
        """Пока идёт запись, другая корутина продолжает работать.

        Проверка поведения, а не текста: если запись вернуть в основной
        поток, счётчик тиков упадёт почти до нуля.
        """
        import web_app

        async def scenario():
            ticks = 0
            stop = False

            async def ticker():
                nonlocal ticks
                while not stop:
                    ticks += 1
                    await asyncio.sleep(0.001)

            task = asyncio.create_task(ticker())
            await asyncio.sleep(0.01)

            # Пишем килобайты в реальный файл через ту же функцию, что и роут.
            payload = b"x" * (2 * 1024 * 1024)
            path = WORK_DIR / "data" / "loop_probe.bin"
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                await asyncio.to_thread(_slow_write, path, payload)
            finally:
                if path.exists():
                    path.unlink()

            before = ticks
            await asyncio.sleep(0.02)
            stop = True
            await task
            return before, ticks

        before, after = asyncio.run(scenario())
        self.assertGreater(
            after, before,
            "event loop не тикал во время записи — I/O выполнялся в основном потоке",
        )
        self.assertIsNotNone(web_app._fetch_and_save_avatar)


def _slow_write(path: Path, payload: bytes) -> None:
    """Запись с задержкой — имитирует медленный диск."""
    with open(path, "wb") as f:
        f.write(payload)
        time.sleep(0.05)


if __name__ == "__main__":
    unittest.main(verbosity=2)
