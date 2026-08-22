"""v5.1.0 — вайтлист ботов: обход via-bot кулдауна и автомьюта.

Запуск: uv run python tools/run_tests.py -k v510_bot_whitelist
"""
from _paths import _P  # noqa: E402
import os
import sys
import unittest
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ["ADMIN_IDS"] = "111"
os.environ["WEB_ALLOW_NO_SECRET"] = "1"
os.environ["DB_PATH"] = "/tmp/degramod_v510_whitelist.db"

sys.path.insert(0, _P())

import bot_handlers  # noqa: E402
from db import BotWhitelist, ChatSettings, async_session, init_db  # noqa: E402
from sqlalchemy import select  # noqa: E402

CHAT = -1001234567890
OTHER_CHAT = -1009876543210
FILTER_CHAT = -1005555555555


class TestWhitelistMatching(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        async with async_session() as s:
            for row in (await s.execute(select(BotWhitelist))).scalars().all():
                await s.delete(row)
            await s.commit()

    async def _add(self, chat_id, username, bot_id=None):
        async with async_session() as s:
            s.add(BotWhitelist(chat_id=chat_id, bot_username=username, bot_id=bot_id))
            await s.commit()

    async def test_per_chat_match(self):
        await self._add(CHAT, "gif")
        async with async_session() as s:
            self.assertTrue(await bot_handlers._is_bot_whitelisted(s, CHAT, "gif", 42))

    async def test_per_chat_does_not_leak_to_other_chat(self):
        await self._add(CHAT, "gif")
        async with async_session() as s:
            self.assertFalse(
                await bot_handlers._is_bot_whitelisted(s, OTHER_CHAT, "gif", 42)
            )

    async def test_global_applies_everywhere(self):
        await self._add(0, "gif")
        async with async_session() as s:
            self.assertTrue(
                await bot_handlers._is_bot_whitelisted(s, OTHER_CHAT, "gif", 42)
            )

    async def test_match_by_bot_id_when_username_changed(self):
        await self._add(0, "oldname", bot_id=42)
        async with async_session() as s:
            self.assertTrue(
                await bot_handlers._is_bot_whitelisted(s, CHAT, "newname", 42)
            )

    async def test_username_match_is_case_insensitive(self):
        await self._add(0, "gif")
        async with async_session() as s:
            self.assertTrue(await bot_handlers._is_bot_whitelisted(s, CHAT, "GIF", 42))

    async def test_unknown_bot_not_whitelisted(self):
        await self._add(0, "gif")
        async with async_session() as s:
            self.assertFalse(
                await bot_handlers._is_bot_whitelisted(s, CHAT, "spammer", 99)
            )


class TestFilterBehavior(unittest.IsolatedAsyncioTestCase):
    """Поведенческая проверка ключевого инварианта задачи: белый бот не
    оставляет запись в _via_bot_rate_limit. Именно из-за этого проверка
    вайтлиста обязана идти ДО rate-limit — иначе белый бот занял бы слот
    окна и подставил бы под автомьют следующего отправителя через другого,
    не-белого бота.
    """

    async def asyncSetUp(self):
        await init_db()
        async with async_session() as s:
            for row in (await s.execute(select(BotWhitelist))).scalars().all():
                await s.delete(row)
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == FILTER_CHAT)
            )).scalar_one_or_none()
            if cs is None:
                cs = ChatSettings(chat_id=FILTER_CHAT)
                s.add(cs)
            cs.via_bot_filter_enabled = True
            cs.via_bot_rate_limit_seconds = 300
            cs.via_bot_mute_minutes = 10
            await s.commit()
        bot_handlers._via_bot_rate_limit.clear()

    @staticmethod
    def _make_message(user_id: int, bot_id: int, bot_username: str):
        # Функция обращается только к message.via_bot и message.from_user
        # до точки возврата — полноценный aiogram Message не нужен.
        return SimpleNamespace(
            via_bot=SimpleNamespace(id=bot_id, username=bot_username),
            from_user=SimpleNamespace(id=user_id),
        )

    async def test_whitelisted_bot_does_not_consume_rate_limit_slot(self):
        async with async_session() as s:
            s.add(BotWhitelist(chat_id=0, bot_username="gif"))
            await s.commit()

        message = self._make_message(user_id=222, bot_id=555, bot_username="gif")
        blocked = await bot_handlers._check_via_bot_filter(message, FILTER_CHAT)

        self.assertFalse(blocked, "белый бот не должен блокироваться")
        key = (FILTER_CHAT, 222, 555)
        self.assertNotIn(
            key, bot_handlers._via_bot_rate_limit,
            "белый бот не должен занимать слот кулдауна",
        )

    async def test_non_whitelisted_bot_still_consumes_slot(self):
        # Контрольный кейс: без вайтлиста поведение не изменилось —
        # первое сообщение разрешается и слот занимается как раньше.
        # Без этого теста предыдущий тест не доказывал бы ничего:
        # ключ мог бы отсутствовать просто потому, что функция вообще
        # перестала писать в словарь.
        message = self._make_message(user_id=223, bot_id=556, bot_username="spammer")
        blocked = await bot_handlers._check_via_bot_filter(message, FILTER_CHAT)

        self.assertFalse(blocked, "первое сообщение не-белого бота разрешено")
        key = (FILTER_CHAT, 223, 556)
        self.assertIn(
            key, bot_handlers._via_bot_rate_limit,
            "не-белый бот обязан занимать слот кулдауна как раньше",
        )


class TestBotIdBackfill(unittest.IsolatedAsyncioTestCase):
    """v5.1.0: bot_id проставляется оппортунистически при первом матче по
    username — иначе колонка вечно NULL и матч по id (переживающий смену
    username) никогда не срабатывает.
    """

    async def asyncSetUp(self):
        await init_db()
        async with async_session() as s:
            for row in (await s.execute(select(BotWhitelist))).scalars().all():
                await s.delete(row)
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == FILTER_CHAT)
            )).scalar_one_or_none()
            if cs is None:
                cs = ChatSettings(chat_id=FILTER_CHAT)
                s.add(cs)
            cs.via_bot_filter_enabled = True
            cs.via_bot_rate_limit_seconds = 300
            cs.via_bot_mute_minutes = 10
            await s.commit()
        bot_handlers._via_bot_rate_limit.clear()

    @staticmethod
    def _make_message(user_id: int, bot_id: int, bot_username: str):
        return SimpleNamespace(
            via_bot=SimpleNamespace(id=bot_id, username=bot_username),
            from_user=SimpleNamespace(id=user_id),
        )

    async def test_bot_id_recorded_on_first_username_match(self):
        async with async_session() as s:
            s.add(BotWhitelist(chat_id=0, bot_username="gif"))
            await s.commit()

        message = self._make_message(user_id=301, bot_id=777, bot_username="gif")
        blocked = await bot_handlers._check_via_bot_filter(message, FILTER_CHAT)
        self.assertFalse(blocked)

        async with async_session() as s:
            row = (await s.execute(
                select(BotWhitelist).where(BotWhitelist.bot_username == "gif")
            )).scalar_one_or_none()
            self.assertEqual(row.bot_id, 777)

    async def test_second_pass_does_not_break(self):
        async with async_session() as s:
            s.add(BotWhitelist(chat_id=0, bot_username="gif"))
            await s.commit()

        message = self._make_message(user_id=302, bot_id=778, bot_username="gif")
        await bot_handlers._check_via_bot_filter(message, FILTER_CHAT)
        # второй прогон — bot_id уже заполнен, UPDATE не должен ничего сломать
        blocked = await bot_handlers._check_via_bot_filter(message, FILTER_CHAT)
        self.assertFalse(blocked)

        async with async_session() as s:
            rows = (await s.execute(
                select(BotWhitelist).where(BotWhitelist.bot_username == "gif")
            )).scalars().all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].bot_id, 778)

    async def test_survives_username_rename_after_backfill(self):
        # Заполненный bot_id (как будто уже был backfill раньше) должен
        # продолжать матчиться, даже когда бот сменил username и старая
        # запись в вайтлисте больше не совпадает по имени.
        async with async_session() as s:
            s.add(BotWhitelist(chat_id=0, bot_username="oldname", bot_id=999))
            await s.commit()

        message = self._make_message(user_id=303, bot_id=999, bot_username="newname")
        blocked = await bot_handlers._check_via_bot_filter(message, FILTER_CHAT)
        self.assertFalse(blocked, "бот остаётся в вайтлисте после смены username")

        key = (FILTER_CHAT, 303, 999)
        self.assertNotIn(
            key, bot_handlers._via_bot_rate_limit,
            "матч по bot_id — белый бот всё ещё не занимает слот кулдауна",
        )


class TestFilterIntegration(unittest.TestCase):
    def test_check_runs_before_rate_limit(self):
        # Дешёвая структурная страховка поверх поведенческого теста выше:
        # проверка вайтлиста должна стоять в исходнике раньше записи
        # timestamp в _via_bot_rate_limit.
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        body = src[src.index("async def _check_via_bot_filter"):]
        body = body[:body.index("\nasync def ", 10)]
        wl = body.index("_is_bot_whitelisted")
        rl = body.index("_via_bot_rate_limit[key] = now")
        self.assertLess(wl, rl,
                        "вайтлист обязан проверяться до записи timestamp")


class TestMigrations(unittest.TestCase):
    def test_legacy_migration_present(self):
        with open(_P("db.py")) as f:
            src = f.read()
        self.assertIn("bot_whitelist", src)

    def test_alembic_revision_present(self):
        import pathlib
        found = [p for p in pathlib.Path(_P("migrations/versions")).glob("*.py")
                 if "bot_whitelist" in p.name]
        self.assertTrue(found, "нет ревизии Alembic для bot_whitelist")


if __name__ == "__main__":
    unittest.main(verbosity=2)
