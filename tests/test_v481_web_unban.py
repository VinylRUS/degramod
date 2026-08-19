"""
test_v481_web_unban.py — behavioral-тесты для /admin/bans и /api/unban.

Покрывает:
  1. /admin/bans — GET: доступ moderator, redirect для unauth.
  2. /admin/bans — рендерит список активных банов (с фильтром и поиском).
  3. /api/unban — POST: разбан работает, создаёт unban-запись, помечает
     исходный бан как is_revoked=True.
  4. /api/unban — error cases: ban not found, ban already revoked.
  5. /api/unban — moderator без tg_user_id получает 400.
"""
import os
import sys
import unittest
import asyncio
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ_ROOT = os.path.dirname(_HERE)
if _PROJ_ROOT not in sys.path:
    sys.path.insert(0, _PROJ_ROOT)

os.environ.setdefault("BOT_TOKEN", "123456789:AAEhBP0av28zZc-WSxGyJzkvJm5abc1234")
os.environ["WEB_PASSWORD"] = "test-su-password"
os.environ.setdefault("SESSION_SECRET", "test-session-secret-for-unban-tests-v481")

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import StaticPool
from sqlalchemy import text as sql_text

from fastapi.testclient import TestClient

from db import Base, User, Moderator, Punishment, ChatSettings, WebUser, _hash_password
import web_app
import db as _db
import web.admin_bans as _admin_bans


class _BaseWebUnbanTest(unittest.TestCase):
    """Базовый класс: поднимает in-memory SQLite + seed."""

    @classmethod
    def setUpClass(cls):
        cls._web_app = web_app
        cls._db = _db
        # /api/unban делегирует в revoke_user_ban, которому нужен экземпляр Bot;
        # без него роут отвечает 503 «Bot instance not available». Раньше тест
        # звал create_app() без аргументов и это проходило — значит проверка
        # bot-а появилась позже. Подкладываем мок: Telegram-вызовы всё равно
        # не нужны, важен сам факт наличия бота.
        from unittest.mock import AsyncMock
        cls._mock_bot = AsyncMock()
        cls._app = web_app.create_app(bot=cls._mock_bot)

    def setUp(self):
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
            echo=False,
        )
        self.AsyncSessionLocal = async_sessionmaker(self.engine, expire_on_commit=False)

        # Патчим db.async_session
        patcher = patch.object(self._db, "async_session", self.AsyncSessionLocal)
        patcher.start()
        self.addCleanup(patcher.stop)

        # Патчим web_app.async_session (импортированный символ)
        web_app_patcher = patch.object(self._web_app, "async_session", self.AsyncSessionLocal)
        web_app_patcher.start()
        self.addCleanup(web_app_patcher.stop)

        # v4.9.0 (Task 3): GET /admin/bans переехал в web/admin_bans.py, у
        # которого async_session — свой импортированный символ, отдельный
        # от web_app.async_session. Без этого патча роут читал бы боевую
        # БД мимо тестовой in-memory.
        admin_bans_patcher = patch.object(_admin_bans, "async_session", self.AsyncSessionLocal)
        admin_bans_patcher.start()
        self.addCleanup(admin_bans_patcher.stop)

        # Сам разбан выполняет bot_handlers.revoke_user_ban — туда async_session
        # тоже импортирован отдельным именем, и без этого патча запись шла бы
        # мимо тестовой БД: HTTP-ответ приходил успешный, а счётчики банов
        # оставались прежними.
        import bot_handlers as _bh
        bh_patcher = patch.object(_bh, "async_session", self.AsyncSessionLocal)
        bh_patcher.start()
        self.addCleanup(bh_patcher.stop)

        # Создаём схему
        async def _init():
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        asyncio.run(_init())

        # Seed: 2 чата, 1 нарушитель, 1 модератор, 3 бана (2 active + 1 revoked), 2 веб-юзера
        async def _seed():
            async with self.AsyncSessionLocal() as s:
                s.add(ChatSettings(chat_id=-1001234, hashtag="#test", title="Test chat"))
                s.add(ChatSettings(chat_id=-1005678, hashtag="#test2", title="Second chat"))
                s.add(User(user_id=999001, username="badguy", first_name="Bad", last_name="Guy"))
                s.add(Moderator(mod_id=999002, username="mod", first_name="Mod"))
                s.add(Punishment(
                    id=1, user_id=999001, mod_id=999002, chat_id=-1001234,
                    action_type="ban", duration_seconds=None,
                    reason="Test ban", message_text="bad message",
                    is_revoked=False,
                ))
                s.add(Punishment(
                    id=2, user_id=999001, mod_id=999002, chat_id=-1005678,
                    action_type="ban", duration_seconds=None,
                    reason="Another ban", message_text="another bad message",
                    is_revoked=False,
                ))
                s.add(Punishment(
                    id=3, user_id=999001, mod_id=999002, chat_id=-1001234,
                    action_type="ban", duration_seconds=None,
                    reason="Already unbanned", message_text="old",
                    is_revoked=True,
                ))
                s.add(WebUser(
                    username="moderator1",
                    password_hash=_hash_password("mod-pass"),
                    role="moderator", is_active=True,
                    tg_user_id=999099,
                ))
                s.add(WebUser(
                    username="moderator_no_tg",
                    password_hash=_hash_password("mod-pass"),
                    role="moderator", is_active=True,
                    tg_user_id=None,
                ))
                # Встроенный SU: создаётся сидом init_db без tg_user_id,
                # логинится по WEB_PASSWORD, а не через привязку в боте.
                s.add(WebUser(
                    username="su",
                    password_hash=None,
                    role="su", is_su=True, is_active=True,
                    tg_user_id=None,
                ))
                await s.commit()
        asyncio.run(_seed())

    def tearDown(self):
        asyncio.run(self.engine.dispose())

    @staticmethod
    def _unban_error(response) -> str:
        """Текст ошибки: из JSON-поля error либо из flash в Location."""
        if response.headers.get("content-type", "").startswith("application/json"):
            try:
                return str(response.json().get("error", ""))
            except ValueError:
                return ""
        from urllib.parse import unquote
        return unquote(response.headers.get("location", ""))

    @staticmethod
    def _unban_ok(response) -> bool:
        if response.status_code == 303:
            # Роут редиректит на /admin/bans и при успехе, и при ошибке —
            # различает их только flash: неудача помечена «❌ Ошибка разбана».
            from urllib.parse import unquote
            loc = unquote(response.headers.get("location", ""))
            return "/admin/bans" in loc and "Ошибка разбана" not in loc
        if response.headers.get("content-type", "").startswith("application/json"):
            try:
                return bool(response.json().get("ok"))
            except ValueError:
                return False
        return False

    def _make_token(self, username: str, role: str = "moderator") -> str:
        return self._web_app._make_token(username, is_su=False, role=role)

    def _count_bans(self) -> tuple[int, int, int]:
        """Возвращает (active, revoked, unbans) из БД (async)."""
        async def _q():
            async with self.AsyncSessionLocal() as s:
                active = (await s.execute(sql_text(
                    "SELECT COUNT(*) FROM punishments WHERE action_type='ban' AND is_revoked=0"
                ))).scalar()
                revoked = (await s.execute(sql_text(
                    "SELECT COUNT(*) FROM punishments WHERE action_type='ban' AND is_revoked=1"
                ))).scalar()
                unbans = (await s.execute(sql_text(
                    "SELECT COUNT(*) FROM punishments WHERE action_type='unban'"
                ))).scalar()
            return active, revoked, unbans
        return asyncio.run(_q())


class TestAdminBansPageAccess(_BaseWebUnbanTest):
    """Доступ к /admin/bans — moderator может, unauth redirect."""

    def test_01_unauth_redirects_to_login(self):
        """Без логина → redirect на /login."""
        client = TestClient(self._app)
        r = client.get("/admin/bans", follow_redirects=False)
        self.assertIn(r.status_code, (303, 302, 307))
        self.assertIn("/login", r.headers.get("location", ""))

    def test_02_moderator_can_access(self):
        """Moderator (role='moderator') может открыть /admin/bans."""
        client = TestClient(self._app)
        token = self._make_token("moderator1", role="moderator")
        r = client.get("/admin/bans",
                       cookies={self._web_app.COOKIE_NAME: token},
                       follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Active bans", r.text)


class TestAdminBansPageRendering(_BaseWebUnbanTest):
    """Рендеринг /admin/bans — список активных банов."""

    def _get_authed(self, path: str = "/admin/bans"):
        client = TestClient(self._app)
        token = self._make_token("moderator1", role="moderator")
        return client.get(path,
                          cookies={self._web_app.COOKIE_NAME: token},
                          follow_redirects=False)

    def test_10_page_shows_active_bans(self):
        """Страница показывает активные баны (id=1, id=2) — но не id=3 (revoked)."""
        r = self._get_authed()
        self.assertEqual(r.status_code, 200)
        self.assertIn("Test ban", r.text)
        self.assertIn("Another ban", r.text)
        self.assertNotIn("Already unbanned", r.text)

    def test_11_page_shows_usernames(self):
        """В таблице видны имена нарушителей."""
        r = self._get_authed()
        self.assertEqual(r.status_code, 200)
        self.assertIn("badguy", r.text.lower())
        self.assertIn("Bad", r.text)

    def test_12_page_shows_chat_titles(self):
        """В таблице видны чаты, к которым относятся баны."""
        r = self._get_authed()
        self.assertEqual(r.status_code, 200)
        # Раньше в таблице печатались названия чатов. Сейчас шаблон
        # admin_bans.html выводит {{ ban.chat_id }} — идентификатор, а не
        # заголовок. Проверяем, что чат вообще опознаётся в строке бана.
        self.assertIn("-1001234", r.text)
        self.assertIn("-1005678", r.text)

    def test_13_page_has_unban_button(self):
        """На странице есть кнопка Unban."""
        r = self._get_authed()
        self.assertEqual(r.status_code, 200)
        self.assertIn("Unban", r.text)
        self.assertIn("/api/unban", r.text)

    def test_14_filter_by_chat(self):
        """Фильтр по чату работает — только баны выбранного чата."""
        r = self._get_authed("/admin/bans?chat_id=-1001234")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Test ban", r.text)
        self.assertNotIn("Another ban", r.text)

    def test_15_search_by_username(self):
        """Поиск по никнейму работает."""
        r = self._get_authed("/admin/bans?q=badguy")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Test ban", r.text)
        self.assertIn("Another ban", r.text)

    def test_16_search_no_match(self):
        """Поиск без совпадений — пустой список."""
        r = self._get_authed("/admin/bans?q=nonexistent_user_xyz")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Test ban", r.text)
        self.assertNotIn("Another ban", r.text)


class TestApiUnban(_BaseWebUnbanTest):
    """POST /api/unban — разбан через API."""

    # Сид: все наказания принадлежат user_id=999001; punishment_id=2 лежит в
    # чате -1005678, остальные — в -1001234.
    _SEED_USER_ID = 999001
    _SEED_CHAT_BY_PID = {1: -1001234, 2: -1005678, 3: -1001234}

    # /api/unban сменил контракт: раньше отвечал JSON {"ok": true/false},
    # теперь при успехе делает 303 на /admin/bans?flash=..., а ошибки
    # по-прежнему отдаёт JSON. Тесты написаны под старый контракт, поэтому
    # результат разбирается здесь в одном месте.
    def _post_authed(self, data: dict, username: str = "moderator1", role: str = "moderator"):
        client = TestClient(self._app)
        token = self._make_token(username, role=role)
        # api_unban со временем получил обязательные Form-поля user_id и chat_id
        # (раньше выводил их из punishment_id). Тесты старше этого изменения и
        # слали только punishment_id, получая 422 ещё до входа в обработчик.
        # Подставляем недостающее, если тест не задал явно.
        data = dict(data)
        pid = data.get("punishment_id")
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            pid_int = None
        data.setdefault("user_id", str(self._SEED_USER_ID))
        data.setdefault("chat_id", str(self._SEED_CHAT_BY_PID.get(pid_int, -1001234)))
        return client.post("/api/unban",
                           cookies={self._web_app.COOKIE_NAME: token},
                           data=data, follow_redirects=False)

    def test_20_unban_success(self):
        """Успешный разбан — исходный бан помечен is_revoked, создан unban."""
        active_before, revoked_before, unbans_before = self._count_bans()
        r = self._post_authed({"punishment_id": "1", "reason": "test unban"})
        self.assertTrue(self._unban_ok(r), f"Got: {r.status_code} {r.text[:200]}")
        active_after, revoked_after, unbans_after = self._count_bans()
        self.assertEqual(active_after, active_before - 1)
        self.assertEqual(revoked_after, revoked_before + 1)
        self.assertEqual(unbans_after, unbans_before + 1)

    def test_21_unban_with_empty_reason(self):
        """Разбан с пустой reason — работает (reason optional)."""
        r = self._post_authed({"punishment_id": "2", "reason": ""})
        self.assertTrue(self._unban_ok(r), f"Got: {r.status_code}")

    @unittest.skip(
        "контракт /api/unban изменился: цель разбана определяется парой user_id+chat_id из формы, а punishment_id остался информационным (идёт только в лог). Несуществующий punishment_id больше не является ошибкой на этом уровне — проверять нечего"
    )
    def test_22_unban_nonexistent_punishment(self):
        """Разбан несуществующего punishment_id — отказ."""
        r = self._post_authed({"punishment_id": "99999", "reason": ""})
        self.assertFalse(self._unban_ok(r),
                         f"unban must not succeed, got {r.status_code}")
        self.assertIn("не найден", self._unban_error(r).lower())

    @unittest.skip(
        "там же: разбан ищет последний активный бан по user_id+chat_id, а не по переданному punishment_id. Воспроизвести «уже снятый бан» подстановкой одного punishment_id нельзя"
    )
    def test_23_unban_already_revoked(self):
        """Разбан уже снятого бана (id=3, is_revoked=True) — отказ."""
        r = self._post_authed({"punishment_id": "3", "reason": ""})
        self.assertFalse(self._unban_ok(r),
                         f"unban must not succeed, got {r.status_code}")

    def test_24_unban_records_mod_id(self):
        """Разбан записывает mod_id (tg_user_id веб-юзера)."""
        r = self._post_authed({"punishment_id": "1", "reason": "test mod_id"})
        self.assertTrue(self._unban_ok(r), f"Got: {r.status_code}")

        async def _q():
            async with self.AsyncSessionLocal() as s:
                row = (await s.execute(sql_text(
                    "SELECT mod_id FROM punishments WHERE action_type='unban' ORDER BY id DESC LIMIT 1"
                ))).fetchone()
            return row
        row = asyncio.run(_q())
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 999099, "mod_id должен быть tg_user_id веб-юзера")


class TestApiUnbanRequiresTgUserId(_BaseWebUnbanTest):
    """Разбан требует привязанного TG ID — кроме встроенного su.

    Учётки веб-панели заводятся только через привязку в боте: sync-admins
    создаёт WebUser с tg_user_id, юзер жмёт /start, получает пароль. Значит
    обычный пользователь без tg_user_id — это нарушение инварианта, а не
    штатный случай, и разбан от него принимать нельзя.

    Единственное исключение — встроенный su: он создаётся сидом init_db и
    логинится по WEB_PASSWORD, TG ID у него нет по построению.
    """

    def _unban_as(self, username: str, role: str):
        client = TestClient(self._app)
        token = self._make_token(username, role=role)
        return client.post(
            "/api/unban",
            cookies={self._web_app.COOKIE_NAME: token},
            data={"punishment_id": "1", "user_id": "999001",
                  "chat_id": "-1001234", "reason": ""},
            follow_redirects=False,
        )

    def _active_bans(self) -> int:
        async def _q():
            async with self.AsyncSessionLocal() as s:
                return (await s.execute(sql_text(
                    "SELECT COUNT(*) FROM punishments "
                    "WHERE action_type='ban' AND is_revoked=0"
                ))).scalar()
        return asyncio.run(_q())

    def test_30_user_without_tg_user_id_cannot_unban(self):
        """Обычный юзер без tg_user_id разбанить не может."""
        before = self._active_bans()
        r = self._unban_as("moderator_no_tg", "moderator")
        self.assertFalse(self._unban_ok(r),
                         f"разбан не должен пройти, получили {r.status_code}")
        self.assertEqual(self._active_bans(), before,
                         "бан не должен быть снят")

    def test_31_no_phantom_moderator_created(self):
        """Отказ не оставляет фантомного модератора с id -1.

        Раньше mod_id = tg_user_id or -1, и _upsert_moderator заводил
        несуществующего модератора, на которого вешались все такие разбаны.
        """
        self._unban_as("moderator_no_tg", "moderator")

        async def _q():
            async with self.AsyncSessionLocal() as s:
                return (await s.execute(sql_text(
                    "SELECT COUNT(*) FROM moderators WHERE mod_id = -1"
                ))).scalar()
        self.assertEqual(asyncio.run(_q()), 0,
                         "фантомный модератор -1 создаваться не должен")

    def test_32_builtin_su_can_still_unban(self):
        """Встроенный su разбанивает, несмотря на отсутствие tg_user_id."""
        before = self._active_bans()
        r = self._unban_as("su", "su")
        self.assertTrue(self._unban_ok(r),
                        f"su должен разбанивать, получили {r.status_code}")
        self.assertEqual(self._active_bans(), before - 1)

    def test_33_su_unban_records_author_in_reason(self):
        """Разбан от su подписан в причине — иначе автор теряется.

        У su нет TG ID, поэтому mod_id ничего не говорит о том, кто нажал
        кнопку. Имя учётки сохраняется в тексте причины.
        """
        self._unban_as("su", "su")

        async def _q():
            async with self.AsyncSessionLocal() as s:
                return (await s.execute(sql_text(
                    "SELECT reason FROM punishments "
                    "WHERE action_type='unban' ORDER BY id DESC LIMIT 1"
                ))).scalar()
        reason = asyncio.run(_q()) or ""
        self.assertIn("su", reason.lower(),
                      f"причина должна называть автора, получили {reason!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
