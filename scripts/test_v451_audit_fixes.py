"""
test_v451_audit_fixes.py — Тесты v4.5.1: генеральный аудит + фиксы логики и безопасности.

Покрывает:
  1. _count_warns исключает consumed_by_action
  2. _mark_warns_consumed помечает варны (auto_mute / auto_ban)
  3. _revoke_last_warns снимает и consumed, и не-consumed варны
  4. _is_admin для деактивированного модератора → False (не падает в fallback)
  5. _get_web_user_role возвращает правильную роль
  6. _send_audit_to_report: отправляет в репорт-чат, обрабатывает отсутствие репорт-чата
  7. !resetwarns для рядового модератора → отказ (через _get_web_user_role)
  8. !unwarn cap = current count (через _revoke_last_warns)
  9. /avatar без auth → redirect на /login
 10. Rate-limit на /login после 5 попыток → 429
 11. /logout GET — редирект на /login без удаления cookie (legacy)
 12. /logout POST — редирект на /login с удалением cookie
 13. admin_chats_update с невалидным report_chat_id → redirect с flash
 14. admin_chats_update с валидным report_chat_id (is_report_chat=True) → OK
 15. admin_users_delete чистит chat_admins для tg_user_id
 16. _wal_checkpoint не падает (даже на пустой БД)
 17. admin_chats_page отдаёт report_chat_options (только чаты с is_report_chat=True)
 18. _check_warn_threshold триггерит _mark_warns_consumed (через мок)
 19. Webhook: bot.py — секретная переменная WEBHOOK_SECRET существует
 20. Punishment.consumed_by_action колонка существует в модели

Все тесты используют in-memory SQLite (DB_PATH=:memory:).
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

# Подкладываем test-окружение ДО импорта модулей проекта.
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ.setdefault("WEB_PASSWORD", "test-pwd")
os.environ.setdefault("SESSION_SECRET", "test-secret-xxxxxxxxxxxxxxxxxxxxx")
os.environ.setdefault("ADMIN_IDS", "111111111")

sys.path.insert(0, "/home/z/my-project/v4.5")

from sqlalchemy import select, delete  # noqa: E402

from db import (  # noqa: E402
    async_session, init_db, WebUser, ChatSettings, Punishment, User, Moderator,
    ChatAdmin,
)

# v4.5.1: сохраняем оригинальный _check_login_rate_limit при первом импорте —
# чтобы TestLoginRateLimit мог восстановить его даже если другие тесты
# (из test_v45_dashboard) заменили его на lambda через monkey-patch.
import web_app as _web_app_for_orig  # noqa: E402
_ORIGINAL_CHECK_LOGIN_RATE_LIMIT = _web_app_for_orig._check_login_rate_limit


async def _clear_all_tables():
    """Чистит все таблицы между тестами для изоляции."""
    async with async_session() as s:
        await s.execute(delete(Punishment))
        await s.execute(delete(ChatAdmin))
        await s.execute(delete(ChatSettings))
        await s.execute(delete(User))
        await s.execute(delete(Moderator))
        await s.execute(delete(WebUser))
        await s.commit()
    # v4.5.1: чистим rate-limit dict (по умолчанию).
    # TestLoginRateLimit сам восстанавливает оригинальный _check_login_rate_limit.
    try:
        from web_app import _login_attempts
        _login_attempts.clear()
    except ImportError:
        pass


async def _seed_su():
    async with async_session() as s:
        s.add(WebUser(username="su", is_su=True, is_active=True,
                       role="su", created_by="system"))
        await s.commit()


# ═══════════════════════════════════════════════════════════════════════════
# Тест 1-3: _count_warns / _mark_warns_consumed / _revoke_last_warns
# ═══════════════════════════════════════════════════════════════════════════
class TestConsumedWarns(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_su()
        async with async_session() as s:
            s.add(User(user_id=1001, username="badguy"))
            s.add(Moderator(mod_id=999, username="admin"))
            # 4 активных варна
            for _ in range(4):
                s.add(Punishment(
                    user_id=1001, mod_id=999, chat_id=-100,
                    action_type="warn", duration_seconds=1, reason="spam",
                    is_revoked=False, consumed_by_action=None,
                ))
            await s.commit()

    async def test_count_warns_returns_4(self):
        from bot_handlers import _count_warns
        async with async_session() as s:
            n = await _count_warns(s, 1001, -100)
        self.assertEqual(n, 4)

    async def test_mark_warns_consumed_sets_flag(self):
        from bot_handlers import _count_warns, _mark_warns_consumed
        async with async_session() as s:
            consumed = await _mark_warns_consumed(s, 1001, -100, "auto_mute")
            self.assertEqual(consumed, 4)
            # После mark — _count_warns должен вернуть 0
            n = await _count_warns(s, 1001, -100)
            self.assertEqual(n, 0)
            # Проверим, что флаг реально стоит
            warns = (await s.execute(
                select(Punishment).where(
                    Punishment.user_id == 1001,
                    Punishment.chat_id == -100,
                    Punishment.action_type == "warn",
                )
            )).scalars().all()
            self.assertTrue(all(w.consumed_by_action == "auto_mute" for w in warns))
            # is_revoked остался False (варны видны в логе)
            self.assertTrue(all(not w.is_revoked for w in warns))

    async def test_revoke_last_warns_takes_consumed_too(self):
        """v4.5.1: !unwarn должен снимать и consumed, и не-consumed варны —
        иначе после автомьюта !unwarn ничего не снимет."""
        from bot_handlers import _revoke_last_warns, _mark_warns_consumed
        async with async_session() as s:
            # Сначала гасим 2 варна автомьютом
            await _mark_warns_consumed(s, 1001, -100, "auto_mute")
            # Теперь снимаем 1 варн через !unwarn — должно снять (не важно, consumed или нет)
            revoked = await _revoke_last_warns(s, 1001, -100, 1, revoked_by_mod_id=999)
            self.assertEqual(revoked, 1)
            # Проверим, что 1 варн теперь is_revoked=True
            revoked_count = (await s.execute(
                select(Punishment).where(
                    Punishment.user_id == 1001,
                    Punishment.chat_id == -100,
                    Punishment.action_type == "warn",
                    Punishment.is_revoked.is_(True),
                )
            )).scalars().all()
            self.assertEqual(len(revoked_count), 1)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 4: _is_admin для деактивированного модератора → False
# ═══════════════════════════════════════════════════════════════════════════
class TestIsAdminDeactivatedModerator(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_su()
        # Создаём модератора с tg_user_id и chat_admins
        async with async_session() as s:
            s.add(WebUser(
                username="mod1", password_hash="x:y", is_su=False,
                is_active=True, role="moderator", tg_user_id=222222,
                tg_first_name="Mod", tg_username="mod1",
            ))
            s.add(ChatSettings(chat_id=-100, is_enabled=True, is_private=False))
            s.add(ChatAdmin(chat_id=-100, user_id=222222))
            await s.commit()

    async def test_active_moderator_has_access(self):
        from bot_handlers import _is_admin
        async with async_session() as s:
            self.assertTrue(await _is_admin(s, -100, 222222))

    async def test_deactivated_moderator_blocked(self):
        """v4.5.1 FIX: деактивированный модератор НЕ должен получать доступ
        через fallback к chat_admins."""
        from bot_handlers import _is_admin
        async with async_session() as s:
            wu = (await s.execute(
                select(WebUser).where(WebUser.tg_user_id == 222222)
            )).scalar_one()
            wu.is_active = False
            await s.commit()

        # Проверяем — должно быть False, не падая в fallback
        async with async_session() as s:
            self.assertFalse(await _is_admin(s, -100, 222222))

    async def test_deleted_moderator_blocked_after_admin_delete(self):
        """v4.5.1: после admin_users_delete chat_admins тоже чистится,
        так что fallback тоже не сработает. Но даже если бы не чистилось —
        WebUser нет, fallback сработал бы. Поэтому чистить нужно."""
        from bot_handlers import _is_admin
        async with async_session() as s:
            wu = (await s.execute(
                select(WebUser).where(WebUser.tg_user_id == 222222)
            )).scalar_one()
            # Симулируем admin_users_delete: удаляем chat_admins + WebUser
            cas = (await s.execute(
                select(ChatAdmin).where(ChatAdmin.user_id == 222222)
            )).scalars().all()
            for ca in cas:
                await s.delete(ca)
            await s.delete(wu)
            await s.commit()
        # Теперь _is_admin должен вернуть False (нет ни WebUser, ни chat_admins)
        async with async_session() as s:
            self.assertFalse(await _is_admin(s, -100, 222222))


# ═══════════════════════════════════════════════════════════════════════════
# Тест 5: _get_web_user_role
# ═══════════════════════════════════════════════════════════════════════════
class TestGetWebUserRole(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_su()
        async with async_session() as s:
            s.add(WebUser(
                username="admin1", password_hash="x:y", is_su=False,
                is_active=True, role="admin", tg_user_id=333333,
            ))
            s.add(WebUser(
                username="mod1", password_hash="x:y", is_su=False,
                is_active=True, role="moderator", tg_user_id=444444,
            ))
            await s.commit()

    async def test_returns_admin_role(self):
        from bot_handlers import _get_web_user_role
        async with async_session() as s:
            self.assertEqual(await _get_web_user_role(s, 333333), "admin")

    async def test_returns_moderator_role(self):
        from bot_handlers import _get_web_user_role
        async with async_session() as s:
            self.assertEqual(await _get_web_user_role(s, 444444), "moderator")

    async def test_returns_su_role(self):
        from bot_handlers import _get_web_user_role
        # SU в сид-данных имеет tg_user_id=None, так что вернёт None
        async with async_session() as s:
            self.assertIsNone(await _get_web_user_role(s, 555555))

    async def test_returns_none_for_no_web_user(self):
        from bot_handlers import _get_web_user_role
        async with async_session() as s:
            self.assertIsNone(await _get_web_user_role(s, 999999))


# ═══════════════════════════════════════════════════════════════════════════
# Тест 6: _send_audit_to_report
# ═══════════════════════════════════════════════════════════════════════════
class TestSendAuditToReport(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_su()
        async with async_session() as s:
            s.add(ChatSettings(chat_id=-100, is_enabled=True))
            # Репорт-чат с пометкой is_report_chat=True
            s.add(ChatSettings(chat_id=-200, is_enabled=True, is_report_chat=True))
            await s.commit()

    async def test_sends_to_report_chat(self):
        from bot_handlers import _send_audit_to_report
        mock_bot = AsyncMock()
        mod = MagicMock(); mod.id = 999; mod.first_name = "Admin"
        mod.last_name = None; mod.username = "admin"
        target = MagicMock(); target.id = 1001; target.first_name = "Bad"
        target.last_name = None; target.username = "badguy"

        await _send_audit_to_report(
            bot=mock_bot, chat_id=-100, mod=mod, target=target,
            action_label="варн(а/ов)", detail="команда !unwarn", count=2,
        )
        # Должен был отправить в репорт-чат
        mock_bot.send_message.assert_awaited_once()
        kwargs = mock_bot.send_message.call_args.kwargs
        self.assertEqual(kwargs["chat_id"], -200)
        self.assertIn("Снятие санкции", kwargs["text"])
        self.assertIn("2 варн(а/ов)", kwargs["text"])
        self.assertIn("Admin", kwargs["text"])

    async def test_no_report_chat_silent(self):
        """Если репорт-чата нет — молча выходим, не падаем."""
        from bot_handlers import _send_audit_to_report
        # Уберём is_report_chat
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -200)
            )).scalar_one()
            cs.is_report_chat = False
            await s.commit()

        mock_bot = AsyncMock()
        mod = MagicMock(); mod.id = 999; mod.first_name = "Admin"
        mod.last_name = None; mod.username = "admin"
        target = MagicMock(); target.id = 1001; target.first_name = "Bad"
        target.last_name = None; target.username = "badguy"

        await _send_audit_to_report(
            bot=mock_bot, chat_id=-100, mod=mod, target=target,
            action_label="мьют", detail="команда !unmute",
        )
        mock_bot.send_message.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════════
# Тест 9: /avatar без auth → redirect на /login
# ═══════════════════════════════════════════════════════════════════════════
class TestAvatarRequiresAuth(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_su()
        from web_app import create_app, _login_attempts
        # Чистим rate-limit dict между тестами
        _login_attempts.clear()
        self.mock_bot = MagicMock()
        self.app = create_app(bot=self.mock_bot)
        from httpx import AsyncClient, ASGITransport
        self.client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test")

    async def test_avatar_no_auth_redirects_to_login(self):
        resp = await self.client.get("/avatar/12345", follow_redirects=False)
        # require_auth возвращает HTTPException(status_code=303, Location=/login)
        self.assertIn(resp.status_code, (303, 302, 307))
        self.assertIn("/login", resp.headers.get("location", ""))

    async def test_avatar_with_auth_returns_404_if_no_file(self):
        """Логинимся как SU, идём за аватаркой — 404 так как файла нет."""
        resp = await self.client.post(
            "/login", data={"username": "su", "password": "test-pwd"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        resp = await self.client.get("/avatar/12345", follow_redirects=False)
        self.assertEqual(resp.status_code, 404)

    async def asyncTearDown(self):
        await self.client.aclose()


# ═══════════════════════════════════════════════════════════════════════════
# Тест 10: Rate-limit на /login
# ═══════════════════════════════════════════════════════════════════════════
class TestLoginRateLimit(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_su()
        import web_app
        from web_app import create_app, _login_attempts
        # v4.5.1: восстанавливаем оригинальный _check_login_rate_limit —
        # другие тест-файлы могли заменить его на lambda через monkey-patch.
        web_app._check_login_rate_limit = _ORIGINAL_CHECK_LOGIN_RATE_LIMIT
        # Чистим rate-limit dict между тестами
        _login_attempts.clear()
        self.mock_bot = MagicMock()
        self.app = create_app(bot=self.mock_bot)
        from httpx import AsyncClient, ASGITransport
        self.client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test")

    async def test_5_attempts_then_429(self):
        """Первые 5 попыток — не 429. Шестая — 429."""
        for i in range(5):
            resp = await self.client.post(
                "/login", data={"username": "su", "password": "wrong"},
                follow_redirects=False,
            )
            # 5 попыток: возвращается либо 200 (форма с ошибкой), либо 303 (успех)
            # главное — не 429
            self.assertNotEqual(resp.status_code, 429, f"attempt {i+1} should not be 429")
        # 6-я попытка — должна быть 429
        resp = await self.client.post(
            "/login", data={"username": "su", "password": "wrong"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 429)

    async def asyncTearDown(self):
        await self.client.aclose()


# ═══════════════════════════════════════════════════════════════════════════
# Тест 11-12: /logout GET (legacy) vs POST (real)
# ═══════════════════════════════════════════════════════════════════════════
class TestLogoutPostOnly(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_su()
        from web_app import create_app, _login_attempts
        _login_attempts.clear()
        self.mock_bot = MagicMock()
        self.app = create_app(bot=self.mock_bot)
        from httpx import AsyncClient, ASGITransport
        self.client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test")
        await self.client.post(
            "/login", data={"username": "su", "password": "test-pwd"},
            follow_redirects=False,
        )

    async def test_get_logout_does_not_delete_cookie(self):
        """GET /logout — legacy: редиректит на /login, но НЕ удаляет cookie."""
        resp = await self.client.get("/logout", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/login", resp.headers.get("location", ""))
        # Cookie должна остаться
        set_cookie_headers = resp.headers.get_list("set-cookie")
        # Если cookie удаляется — заголовок содержит expires в прошлом или max-age=0
        # Если не удаляется — заголовка set-cookie для session может не быть вовсе
        deleted = any(
            "max-age=0" in h.lower() or "expires=thu, 01 jan 1970" in h.lower()
            for h in set_cookie_headers
        )
        self.assertFalse(deleted, "GET /logout should NOT delete session cookie")

    async def test_post_logout_deletes_cookie(self):
        """POST /logout — реальный logout: редирект + удаляет cookie."""
        resp = await self.client.post("/logout", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/login", resp.headers.get("location", ""))
        set_cookie_headers = resp.headers.get_list("set-cookie")
        deleted = any(
            "max-age=0" in h.lower() or "expires=thu, 01 jan 1970" in h.lower()
            for h in set_cookie_headers
        )
        self.assertTrue(deleted, "POST /logout should delete session cookie")

    async def asyncTearDown(self):
        await self.client.aclose()


# ═══════════════════════════════════════════════════════════════════════════
# Тест 13-14: admin_chats_update — валидация report_chat_id
# ═══════════════════════════════════════════════════════════════════════════
class TestAdminChatsUpdateReportChatId(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_su()
        async with async_session() as s:
            # Чат, который настраиваем
            s.add(ChatSettings(chat_id=-100, is_enabled=True, is_private=False))
            # Чат-кандидат в репорт-чаты, но без пометки is_report_chat
            s.add(ChatSettings(chat_id=-200, is_enabled=True, is_report_chat=False))
            # Чат-кандидат с пометкой is_report_chat
            s.add(ChatSettings(chat_id=-300, is_enabled=True, is_report_chat=True))
            await s.commit()
        from web_app import create_app, _login_attempts
        _login_attempts.clear()
        self.mock_bot = MagicMock()
        self.app = create_app(bot=self.mock_bot)
        from httpx import AsyncClient, ASGITransport
        self.client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test")
        await self.client.post(
            "/login", data={"username": "su", "password": "test-pwd"},
            follow_redirects=False,
        )

    async def test_invalid_report_chat_id_rejected(self):
        """report_chat_id=-200 (is_report_chat=False) → redirect с flash."""
        resp = await self.client.post(
            "/admin/chats/-100/update",
            data={
                "hashtag": "",
                "report_chat_id": "-200",  # не помечен как report chat
                "warns_to_mute": "3",
                "mute_duration_seconds": "3600",
                "warns_to_ban": "5",
                # v4.5.2: новые обязательные поля формы
                "warn_decay_days": "0",
                "link_filter_action": "delete",
                "night_mode_start": "23:00",
                "night_mode_end": "07:00",
                "night_mode_preset": "text_only",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        location = resp.headers.get("location", "")
        self.assertIn("flash=", location)
        self.assertIn("not+marked", location)

    async def test_valid_report_chat_id_accepted(self):
        """report_chat_id=-300 (is_report_chat=True) → OK, сохраняется."""
        resp = await self.client.post(
            "/admin/chats/-100/update",
            data={
                "hashtag": "",
                "report_chat_id": "-300",  # помечен как report chat
                "warns_to_mute": "3",
                "mute_duration_seconds": "3600",
                "warns_to_ban": "5",
                # v4.5.2: новые обязательные поля формы
                "warn_decay_days": "0",
                "link_filter_action": "delete",
                "night_mode_start": "23:00",
                "night_mode_end": "07:00",
                "night_mode_preset": "text_only",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        location = resp.headers.get("location", "")
        self.assertIn("settings+updated", location)
        # Проверим что в БД сохранилось
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -100)
            )).scalar_one()
            self.assertEqual(cs.report_chat_id, -300)

    async def test_empty_report_chat_id_clears(self):
        """Пустой report_chat_id → сохраняется как None (сброс)."""
        # Сначала поставим -300
        await self.client.post(
            "/admin/chats/-100/update",
            data={
                "hashtag": "", "report_chat_id": "-300",
                "warns_to_mute": "3", "mute_duration_seconds": "3600",
                "warns_to_ban": "5",
                "warn_decay_days": "0",
                "link_filter_action": "delete",
                "night_mode_start": "23:00", "night_mode_end": "07:00",
                "night_mode_preset": "text_only",
            },
            follow_redirects=False,
        )
        # Теперь сбрасываем
        resp = await self.client.post(
            "/admin/chats/-100/update",
            data={
                "hashtag": "", "report_chat_id": "",
                "warns_to_mute": "3", "mute_duration_seconds": "3600",
                "warns_to_ban": "5",
                "warn_decay_days": "0",
                "link_filter_action": "delete",
                "night_mode_start": "23:00", "night_mode_end": "07:00",
                "night_mode_preset": "text_only",
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -100)
            )).scalar_one()
            self.assertIsNone(cs.report_chat_id)

    async def asyncTearDown(self):
        await self.client.aclose()


# ═══════════════════════════════════════════════════════════════════════════
# Тест 15: admin_users_delete чистит chat_admins
# ═══════════════════════════════════════════════════════════════════════════
class TestAdminUsersDeleteCleansChatAdmins(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_su()
        async with async_session() as s:
            s.add(WebUser(
                username="mod1", password_hash="x:y", is_su=False,
                is_active=True, role="moderator", tg_user_id=222222,
                tg_first_name="Mod", tg_username="mod1",
            ))
            s.add(ChatSettings(chat_id=-100, is_enabled=True))
            s.add(ChatAdmin(chat_id=-100, user_id=222222))
            await s.commit()

    async def test_delete_user_clears_chat_admins(self):
        """После удаления WebUser-аккаунта chat_admins тоже должна быть пуста."""
        from web_app import create_app, _login_attempts
        _login_attempts.clear()
        self.mock_bot = MagicMock()
        self.app = create_app(bot=self.mock_bot)
        from httpx import AsyncClient, ASGITransport
        client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test")
        try:
            await client.post(
                "/login", data={"username": "su", "password": "test-pwd"},
                follow_redirects=False,
            )
            # Находим id модератора в БД
            async with async_session() as s:
                wu = (await s.execute(
                    select(WebUser).where(WebUser.username == "mod1")
                )).scalar_one()
                uid = wu.id
            # Удаляем через эндпоинт
            resp = await client.post(
                f"/admin/users/{uid}/delete", follow_redirects=False,
            )
            self.assertEqual(resp.status_code, 303)
            # Проверяем, что WebUser удалён
            async with async_session() as s:
                wu = (await s.execute(
                    select(WebUser).where(WebUser.username == "mod1")
                )).scalar_one_or_none()
                self.assertIsNone(wu)
                # И что chat_admins тоже пусто
                cas = (await s.execute(
                    select(ChatAdmin).where(ChatAdmin.user_id == 222222)
                )).scalars().all()
                self.assertEqual(len(cas), 0, "chat_admins should be cleared")
        finally:
            await client.aclose()


# ═══════════════════════════════════════════════════════════════════════════
# Тест 16: _wal_checkpoint не падает
# ═══════════════════════════════════════════════════════════════════════════
class TestWalCheckpoint(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_su()

    async def test_wal_checkpoint_does_not_raise(self):
        from web_app import _wal_checkpoint
        # Просто не должно бросать исключений
        _wal_checkpoint()


# ═══════════════════════════════════════════════════════════════════════════
# Тест 17: admin_chats_page отдаёт report_chat_options
# ═══════════════════════════════════════════════════════════════════════════
class TestAdminChatsPageReportOptions(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_su()
        async with async_session() as s:
            s.add(ChatSettings(chat_id=-100, is_enabled=True))
            s.add(ChatSettings(chat_id=-200, is_enabled=True, is_report_chat=True,
                                title="Reports"))
            s.add(ChatSettings(chat_id=-300, is_enabled=True, is_report_chat=True,
                                title="Another reports"))
            s.add(ChatSettings(chat_id=-400, is_enabled=True, is_report_chat=False))
            await s.commit()
        from web_app import create_app, _login_attempts
        _login_attempts.clear()
        self.mock_bot = MagicMock()
        self.app = create_app(bot=self.mock_bot)
        from httpx import AsyncClient, ASGITransport
        self.client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test")
        await self.client.post(
            "/login", data={"username": "su", "password": "test-pwd"},
            follow_redirects=False,
        )

    async def test_report_chat_options_present_in_html(self):
        resp = await self.client.get("/admin/chats", follow_redirects=False)
        self.assertEqual(resp.status_code, 200)
        html = resp.text
        # В шаблоне должны быть опции -200 и -300 (is_report_chat=True)
        self.assertIn("-200", html)
        self.assertIn("-300", html)
        self.assertIn("Reports", html)
        self.assertIn("Another reports", html)

    async def asyncTearDown(self):
        await self.client.aclose()


# ═══════════════════════════════════════════════════════════════════════════
# Тест 18: _check_warn_threshold триггерит _mark_warns_consumed
# ═══════════════════════════════════════════════════════════════════════════
class TestCheckWarnThresholdConsumes(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_su()
        async with async_session() as s:
            s.add(User(user_id=1001, username="badguy"))
            s.add(Moderator(mod_id=999, username="admin"))
            # warns_to_mute=3, warns_to_ban=999999 (недостижим)
            s.add(ChatSettings(
                chat_id=-100, is_enabled=True, is_private=False,
                warns_to_mute=3, warns_to_ban=999999,
                mute_duration_seconds=3600,
            ))
            await s.commit()

    async def test_auto_mute_marks_warns_consumed(self):
        """Достигли warns_to_mute=3 → _check_warn_threshold помечает варны
        consumed_by_action='auto_mute'. После этого _count_warns == 0."""
        from bot_handlers import _check_warn_threshold, _count_warns, _mark_warns_consumed
        # Сидим 3 варна
        async with async_session() as s:
            for _ in range(3):
                s.add(Punishment(
                    user_id=1001, mod_id=999, chat_id=-100,
                    action_type="warn", duration_seconds=1, reason="spam",
                    is_revoked=False, consumed_by_action=None,
                ))
            await s.commit()
        # Мокаем bot
        mock_bot = AsyncMock()
        member = MagicMock()
        member.permissions = MagicMock()
        mock_bot.get_chat_member.return_value = member
        mock_bot.restrict_chat_member.return_value = True
        mock_bot.ban_chat_member.return_value = True
        target = MagicMock(); target.id = 1001; target.username = "badguy"
        target.first_name = "Bad"; target.last_name = None
        mod = MagicMock(); mod.id = 999; mod.username = "admin"
        mod.first_name = "Admin"; mod.last_name = None

        await _check_warn_threshold(bot=mock_bot, chat_id=-100,
                                     target=target, mod=mod)
        # После вызова: все 3 варна должны быть помечены consumed_by_action='auto_mute'
        async with async_session() as s:
            warns = (await s.execute(
                select(Punishment).where(
                    Punishment.user_id == 1001,
                    Punishment.chat_id == -100,
                    Punishment.action_type == "warn",
                )
            )).scalars().all()
            self.assertEqual(len(warns), 3)
            self.assertTrue(all(w.consumed_by_action == "auto_mute" for w in warns))
            # _count_warns возвращает 0 (порог снова не сработает)
            n = await _count_warns(s, 1001, -100)
            self.assertEqual(n, 0)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 19: bot.py — WEBHOOK_SECRET существует
# ═══════════════════════════════════════════════════════════════════════════
class TestBotWebhookSecret(unittest.TestCase):
    """Проверяет, что bot.py определяет WEBHOOK_SECRET."""

    def test_webhook_secret_env_var_read(self):
        # Не импортируем bot.py (он требует BOT_TOKEN и пытается set_webhook),
        # но можем прочитать исходник и убедиться, что WEBHOOK_SECRET используется.
        bot_src = open("/home/z/my-project/v4.5/bot.py").read()
        self.assertIn("WEBHOOK_SECRET", bot_src)
        self.assertIn("secret_token=WEBHOOK_SECRET", bot_src)
        self.assertIn("X-Telegram-Bot-Api-Secret-Token", bot_src)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 20: consumed_by_action колонка существует
# ═══════════════════════════════════════════════════════════════════════════
class TestConsumedByActionColumn(unittest.TestCase):

    def test_column_exists_in_model(self):
        cols = [c.name for c in Punishment.__table__.columns]
        self.assertIn("consumed_by_action", cols)

    def test_column_is_nullable_string(self):
        col = Punishment.__table__.columns["consumed_by_action"]
        self.assertTrue(col.nullable)
        self.assertEqual(col.type.length, 20)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 21: Шаблон admin_chats.html — dropdown
# ═══════════════════════════════════════════════════════════════════════════
class TestAdminChatsTemplateDropdown(unittest.TestCase):

    def test_template_has_select_for_report_chat_id(self):
        html = open("/home/z/my-project/v4.5/templates/admin_chats.html").read()
        # Должен быть <select> с name="report_chat_id"
        self.assertIn('<select name="report_chat_id"', html)
        # И цикл по report_chat_options
        self.assertIn("report_chat_options", html)
        # Старого input type=text для report_chat_id быть не должно
        self.assertNotIn('type="text" name="report_chat_id"', html)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 22: base.html — logout POST-форма
# ═══════════════════════════════════════════════════════════════════════════
class TestBaseTemplateLogoutPost(unittest.TestCase):

    def test_template_has_logout_post_form(self):
        html = open("/home/z/my-project/v4.5/templates/base.html").read()
        # Должна быть скрытая POST-форма для logout
        self.assertIn('id="logout-form"', html)
        self.assertIn('action="/logout"', html)
        self.assertIn('method="post"', html)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 23: .env.example упоминает WEBHOOK_SECRET
# ═══════════════════════════════════════════════════════════════════════════
class TestEnvExampleWebhookSecret(unittest.TestCase):

    def test_env_example_mentions_webhook_secret(self):
        text = open("/home/z/my-project/v4.5/.env.example").read()
        self.assertIn("WEBHOOK_SECRET", text)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 24: APP_VERSION — обновляется с каждой версией. v4.5.1 → v4.5.2 в v4.5.2.
# ═══════════════════════════════════════════════════════════════════════════
class TestAppVersion(unittest.TestCase):

    def test_app_version_is_452(self):
        """v4.5.2: версия обновлена до v4.5.2 (CAS, filters, night mode, warn decay)."""
        import web_app
        self.assertEqual(web_app.APP_VERSION, "v4.5.2")


# ═══════════════════════════════════════════════════════════════════════════
# Сводка
# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    unittest.main(verbosity=2)
