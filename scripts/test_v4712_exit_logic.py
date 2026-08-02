"""
v4.7.12 — Comprehensive тесты выхода из Night Mode и Sanitary Day.

КОНТЕКСТ:
Что происходит при окончании санитарных дней / ночного режима?
Работает ли этот функционал?

Это КРИТИЧЕСКИ ВАЖНЫЙ функционал — если выход сломан, чат остаётся
замьюченным навсегда (никто кроме модераторов не может писать).

Тестируем:
  1-14. is_sanitary_day_today: date и datetime-проверки
  15-17. is_sanitary_active_now_at: edge cases времени
  18-23. _night_mode_in_window: weekday/weekend, edge cases
  24-26. _night_mode_tick: enter/exit/skip dispatch
  27-29. _sanitary_day_tick: enter/exit dispatch
  30-31. _exit_sanitary_day: side effects (last_sanitary_month, JSON cleanup)
  32.   _enter_sanitary_day: priority over night mode (exits night first)
  33.   _startup_recovery: clears stuck active flags
  34-35. _exit_*_mode: fallback when snapshot=None → system 'Day default' preset
  36.   _exit_night_mode: granular day_permissions takes priority over snapshot
  37-42. v4.7.12: auto-transition (sanitary→night, night→night), day preset priority
"""

from __future__ import annotations
import os
import sys
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta, date
from freezegun import freeze_time

sys.path.insert(0, "/home/z/my-project/v4.5")
sys.path.insert(0, "/home/z/my-project/v4.5/scripts")

_DB_PATH = "/tmp/test_v4712_exit_logic.db"
if os.path.exists(_DB_PATH):
    os.remove(_DB_PATH)

os.environ["BOT_TOKEN"] = "0:fake"
os.environ["ADMIN_IDS"] = "1"
os.environ["SU_IDS"] = "1"
os.environ["WEB_PASSWORD"] = "test_password_123"
os.environ["DB_PATH"] = _DB_PATH

import bot_handlers as bh
from bot_handlers import (
    is_sanitary_day_today, is_sanitary_active_now_at,
    _parse_sanitary_date, _parse_sanitary_time,
    _night_mode_in_window, _time_str_in_range,
    parse_sanitary_days_json, serialize_sanitary_days_monthly,
)

# Импортируем bot ОДИН раз (aiogram Dispatcher не позволяет повторный include_router).
import db as _db
if os.path.exists(_DB_PATH):
    os.remove(_DB_PATH)
import asyncio as _asyncio
_asyncio.get_event_loop().run_until_complete(_db.init_db())
from db import async_session as _async_session, ChatSettings as _ChatSettings
import bot as bot_module

# Все 13 полей ChatPermissions
_PERM_FIELDS = [
    "can_send_messages", "can_send_audios", "can_send_documents",
    "can_send_photos", "can_send_videos", "can_send_video_notes",
    "can_send_voice_notes", "can_send_polls", "can_send_other_messages",
    "can_add_web_page_previews", "can_change_info", "can_invite_users",
    "can_pin_messages",
]


def _all_true_perms_dict():
    return {f: True for f in _PERM_FIELDS}


def _all_false_perms_dict():
    return {f: False for f in _PERM_FIELDS}


def _make_pairs(s, e, st=None, et=None):
    out = [s, e]
    if st:
        out.append(st)
    if et:
        out.append(et)
    return out


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _make_mock_perms(all_true=True):
    """Создаёт mock ChatPermissions со всеми True или False."""
    m = MagicMock()
    for f in _PERM_FIELDS:
        setattr(m, f, all_true)
    return m


# ─── Tests: is_sanitary_day_today (core decision logic) ────────────────────


class TestIsSanitaryDayToday(unittest.TestCase):
    """Решение: активен ли санитарный день сейчас?"""

    def test_01_single_day_no_time_in_range(self):
        pairs = [_make_pairs("2026-08-15", "2026-08-15")]
        self.assertTrue(is_sanitary_day_today(pairs, today=date(2026, 8, 15)))

    def test_02_single_day_no_time_outside(self):
        pairs = [_make_pairs("2026-08-15", "2026-08-15")]
        self.assertFalse(is_sanitary_day_today(pairs, today=date(2026, 8, 16)))

    def test_03_range_no_time_in_middle(self):
        pairs = [_make_pairs("2026-08-10", "2026-08-20")]
        self.assertTrue(is_sanitary_day_today(pairs, today=date(2026, 8, 15)))

    def test_04_range_no_time_at_start_edge(self):
        pairs = [_make_pairs("2026-08-10", "2026-08-20")]
        self.assertTrue(is_sanitary_day_today(pairs, today=date(2026, 8, 10)))

    def test_05_range_no_time_at_end_edge(self):
        pairs = [_make_pairs("2026-08-10", "2026-08-20")]
        self.assertTrue(is_sanitary_day_today(pairs, today=date(2026, 8, 20)))

    def test_06_range_no_time_day_after_end(self):
        """Сегодня на следующий день после end → False (вышли!)."""
        pairs = [_make_pairs("2026-08-10", "2026-08-20")]
        self.assertFalse(is_sanitary_day_today(pairs, today=date(2026, 8, 21)))

    # ── С временем (datetime-логика) ────────────────────────────────────

    def test_07_datetime_in_range_middle(self):
        pairs = [_make_pairs("2026-08-15", "2026-08-15", "09:00", "18:00")]
        now = _dt("2026-08-15T12:00:00+00:00")
        self.assertTrue(is_sanitary_day_today(pairs, today=date(2026, 8, 15), now_dt=now))

    def test_08_datetime_at_start_edge(self):
        """Datetime ровно в start_time → True (inclusive)."""
        pairs = [_make_pairs("2026-08-15", "2026-08-15", "09:00", "18:00")]
        now = _dt("2026-08-15T09:00:00+00:00")
        self.assertTrue(is_sanitary_day_today(pairs, today=date(2026, 8, 15), now_dt=now))

    def test_09_datetime_at_end_edge(self):
        """Datetime ровно в end_time → True (inclusive, до HH:MM:59)."""
        pairs = [_make_pairs("2026-08-15", "2026-08-15", "09:00", "18:00")]
        now = _dt("2026-08-15T18:00:00+00:00")
        self.assertTrue(is_sanitary_day_today(pairs, today=date(2026, 8, 15), now_dt=now))

    def test_10_datetime_after_end_minute(self):
        """Datetime в 18:01:00 (после end_time=18:00:59) → False (вышли!)."""
        pairs = [_make_pairs("2026-08-15", "2026-08-15", "09:00", "18:00")]
        # 18:00:59 всё ещё включено (end_dt = HH:MM:59).
        # 18:01:00 — точно вышли.
        now = _dt("2026-08-15T18:01:00+00:00")
        self.assertFalse(is_sanitary_day_today(pairs, today=date(2026, 8, 15), now_dt=now))

    def test_11_datetime_just_before_start(self):
        """Datetime за секунду до start_time → False."""
        pairs = [_make_pairs("2026-08-15", "2026-08-15", "09:00", "18:00")]
        now = _dt("2026-08-15T08:59:59+00:00")
        self.assertFalse(is_sanitary_day_today(pairs, today=date(2026, 8, 15), now_dt=now))

    def test_12_datetime_single_day_cross_midnight_in_range(self):
        """Single-day с 23:00-09:00 (cross-midnight): 02:00 → True.

        Период: 2026-08-15 23:00 → 2026-08-15 09:00... но это невалидно
        (end < start того же дня). Реально код воспринимает это буквально:
        start_dt=2026-08-15 23:00:00, end_dt=2026-08-15 09:00:59.
        02:00 НЕ попадает (02:00 < 23:00 и 02:00 > 09:00:59).

        Для cross-midnight нужно использовать multi-day range:
        2026-08-15 23:00 → 2026-08-16 09:00.
        """
        pairs = [_make_pairs("2026-08-15", "2026-08-16", "23:00", "09:00")]
        now = _dt("2026-08-16T02:00:00+00:00")
        self.assertTrue(is_sanitary_day_today(pairs, today=date(2026, 8, 16), now_dt=now))

    def test_13_datetime_multi_day_after_end(self):
        """Multi-day период 2026-08-15 23:00 → 2026-08-16 09:00; сейчас
        2026-08-16 10:00 → False (вышли)."""
        pairs = [_make_pairs("2026-08-15", "2026-08-16", "23:00", "09:00")]
        now = _dt("2026-08-16T10:00:00+00:00")
        self.assertFalse(is_sanitary_day_today(pairs, today=date(2026, 8, 16), now_dt=now))

    def test_14_mixed_pairs_some_with_time_some_without(self):
        """Смешанный список: без времени + со временем — корректное ИЛИ."""
        pairs = [
            _make_pairs("2026-08-15", "2026-08-15"),
            _make_pairs("2026-08-20", "2026-08-20", "09:00", "18:00"),
        ]
        self.assertTrue(is_sanitary_day_today(pairs, today=date(2026, 8, 15)))
        now = _dt("2026-08-20T12:00:00+00:00")
        self.assertTrue(is_sanitary_day_today(pairs, today=date(2026, 8, 20), now_dt=now))
        now = _dt("2026-08-20T22:00:00+00:00")
        self.assertFalse(is_sanitary_day_today(pairs, today=date(2026, 8, 20), now_dt=now))


# ─── Tests: is_sanitary_active_now_at ──────────────────────────────────────


class TestIsSanitaryActiveNowAt(unittest.TestCase):

    def test_15_only_start_time_uses_default_end_235959(self):
        """Период с только start_time — end_time считается 23:59:59."""
        entry = _make_pairs("2026-08-15", "2026-08-15", "09:00")
        now = _dt("2026-08-15T15:00:00+00:00")
        self.assertTrue(is_sanitary_active_now_at(entry, now))

    def test_16_only_end_time_uses_default_start_000000(self):
        """Период с только end_time (entry len=4, start_time='00:00')."""
        entry = _make_pairs("2026-08-15", "2026-08-15", "00:00", "18:00")
        now = _dt("2026-08-15T10:00:00+00:00")
        self.assertTrue(is_sanitary_active_now_at(entry, now))

    def test_17_default_end_time_inclusive_to_235959(self):
        """entry len=3 (только start_time) — end_time=None → 23:59:59."""
        entry = _make_pairs("2026-08-15", "2026-08-15", "00:00")
        now = _dt("2026-08-15T23:59:59+00:00")
        self.assertTrue(is_sanitary_active_now_at(entry, now))


# ─── Tests: _night_mode_in_window ──────────────────────────────────────────


class TestNightModeInWindow(unittest.TestCase):

    def test_18_simple_window_in_range(self):
        """23:00-07:00 — сейчас 02:00 UTC = 05:00 MSK → в окне."""
        now = _dt("2026-08-15T02:00:00+00:00")
        self.assertTrue(_night_mode_in_window(now, "23:00", "07:00", None, None))

    def test_19_simple_window_out_of_range(self):
        """23:00-07:00 — сейчас 10:00 UTC = 13:00 MSK → НЕ в окне."""
        now = _dt("2026-08-15T10:00:00+00:00")
        self.assertFalse(_night_mode_in_window(now, "23:00", "07:00", None, None))

    def test_20_window_at_exact_start(self):
        """23:00-07:00 — сейчас ровно 23:00 MSK (=20:00 UTC) → в окне (inclusive start)."""
        now = _dt("2026-08-15T20:00:00+00:00")
        self.assertTrue(_night_mode_in_window(now, "23:00", "07:00", None, None))

    def test_21_window_at_exact_end_excluded(self):
        """23:00-07:00 — сейчас ровно 07:00 MSK (=04:00 UTC) → НЕ в окне (end exclusive)."""
        now = _dt("2026-08-15T04:00:00+00:00")
        self.assertFalse(_night_mode_in_window(now, "23:00", "07:00", None, None))

    def test_22_window_just_before_start(self):
        """23:00-07:00 — сейчас 22:59 MSK (=19:59 UTC) → НЕ в окне."""
        now = _dt("2026-08-15T19:59:00+00:00")
        self.assertFalse(_night_mode_in_window(now, "23:00", "07:00", None, None))

    def test_23_weekend_schedule_used_on_saturday(self):
        """Суббота — используется weekend schedule если задан."""
        # 2026-08-15 — суббота
        # 22:00 UTC = 01:00 MSK Sat
        now = _dt("2026-08-15T22:00:00+00:00")
        # weekday: 23:00-07:00 (not in window at 01:00? — да, в окне 23-07)
        # Поменяем weekday на 02:00-03:00 чтобы точно было не в окне
        # weekend: 00:00-10:00 (in window at 01:00)
        self.assertTrue(_night_mode_in_window(
            now, "02:00", "03:00", "00:00", "10:00"
        ))


# ─── Tests: _night_mode_tick dispatch (with freezegun) ─────────────────────


class TestNightModeTickDispatch(unittest.IsolatedAsyncioTestCase):
    """Проверяем что _night_mode_tick корректно вызывает enter/exit."""

    async def asyncSetUp(self):
        # Очищаем таблицу chat_settings перед каждым тестом.
        async with _async_session() as s:
            from sqlalchemy import delete
            await s.execute(delete(_ChatSettings))
            await s.commit()
        self.async_session = _async_session
        self.ChatSettings = _ChatSettings
        self.bot_module = bot_module

    async def test_24_night_tick_calls_exit_when_out_of_window(self):
        """Если night_mode_currently_active=True и время вышло из окна → exit."""
        # Окно 02:00-03:00 (узкое). Freeze time на 12:00 UTC = 15:00 MSK — вне окна.
        async with self.async_session() as s:
            cs = self.ChatSettings(
                chat_id=-100123,
                is_enabled=True,
                night_mode_enabled=True,
                night_mode_currently_active=True,
                night_mode_saved_permissions=json.dumps(_all_true_perms_dict()),
                night_mode_start="02:00",
                night_mode_end="03:00",
                night_mode_tz="Europe/Moscow",
            )
            s.add(cs)
            await s.commit()

        with freeze_time("2026-08-15 12:00:00+00:00"):
            with patch.object(self.bot_module, "bot") as mock_bot:
                mock_bot.set_chat_permissions = AsyncMock()
                mock_bot.get_chat = AsyncMock(return_value=MagicMock(
                    permissions=_make_mock_perms(all_true=True)
                ))
                mock_bot.send_message = AsyncMock()
                await self.bot_module._night_mode_tick()

                self.assertTrue(mock_bot.set_chat_permissions.called,
                                "set_chat_permissions should be called for exit")

        async with self.async_session() as s:
            from sqlalchemy import select
            cs2 = (await s.execute(
                select(self.ChatSettings).where(self.ChatSettings.chat_id == -100123)
            )).scalar_one()
            self.assertFalse(cs2.night_mode_currently_active,
                              "night_mode_currently_active should be False after exit")
            self.assertIsNone(cs2.night_mode_saved_permissions,
                              "saved_permissions should be cleared after exit")

    async def test_25_night_tick_calls_enter_when_in_window(self):
        """Если night_mode_currently_active=False и время в окне → enter."""
        # Окно 23:00-07:00; freeze 02:00 UTC = 05:00 MSK — в окне
        async with self.async_session() as s:
            cs = self.ChatSettings(
                chat_id=-100124,
                is_enabled=True,
                night_mode_enabled=True,
                night_mode_currently_active=False,
                night_mode_start="23:00",
                night_mode_end="07:00",
                night_mode_tz="Europe/Moscow",
            )
            s.add(cs)
            await s.commit()

        with freeze_time("2026-08-15 02:00:00+00:00"):
            with patch.object(self.bot_module, "bot") as mock_bot:
                mock_bot.set_chat_permissions = AsyncMock()
                mock_bot.get_chat = AsyncMock(return_value=MagicMock(
                    permissions=_make_mock_perms(all_true=True)
                ))
                mock_bot.send_message = AsyncMock()
                await self.bot_module._night_mode_tick()

                self.assertTrue(mock_bot.set_chat_permissions.called)

        async with self.async_session() as s:
            from sqlalchemy import select
            cs2 = (await s.execute(
                select(self.ChatSettings).where(self.ChatSettings.chat_id == -100124)
            )).scalar_one()
            self.assertTrue(cs2.night_mode_currently_active)
            self.assertIsNotNone(cs2.night_mode_saved_permissions)

    async def test_26_night_tick_skips_when_sanitary_active(self):
        """Если sanitary_days_currently_active=True → night_tick пропускает чат."""
        async with self.async_session() as s:
            cs = self.ChatSettings(
                chat_id=-100125,
                is_enabled=True,
                night_mode_enabled=True,
                night_mode_currently_active=False,
                sanitary_days_currently_active=True,
                night_mode_start="23:00",
                night_mode_end="07:00",
                night_mode_tz="Europe/Moscow",
            )
            s.add(cs)
            await s.commit()

        with freeze_time("2026-08-15 02:00:00+00:00"):
            with patch.object(self.bot_module, "bot") as mock_bot:
                mock_bot.set_chat_permissions = AsyncMock()
                mock_bot.get_chat = AsyncMock()
                mock_bot.send_message = AsyncMock()
                await self.bot_module._night_mode_tick()

                self.assertFalse(mock_bot.set_chat_permissions.called,
                                 "night_tick should NOT touch chat in sanitary day")


# ─── Tests: _sanitary_day_tick dispatch ────────────────────────────────────


class TestSanitaryDayTickDispatch(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # Очищаем таблицу chat_settings перед каждым тестом.
        async with _async_session() as s:
            from sqlalchemy import delete
            await s.execute(delete(_ChatSettings))
            await s.commit()
        self.async_session = _async_session
        self.ChatSettings = _ChatSettings
        self.bot_module = bot_module

    async def test_27_sanitary_tick_calls_exit_when_period_ended(self):
        """Период сан. дня закончился (date) → _exit_sanitary_day вызывается."""
        # Sanitary: 2026-08-10 .. 2026-08-12. Freeze на 2026-08-13 → period ended.
        async with self.async_session() as s:
            cs = self.ChatSettings(
                chat_id=-100200,
                is_enabled=True,
                sanitary_days_enabled=True,
                sanitary_days_currently_active=True,
                sanitary_days_saved_permissions=json.dumps(_all_true_perms_dict()),
                sanitary_days=serialize_sanitary_days_monthly({
                    "2026-08": [_make_pairs("2026-08-10", "2026-08-12")]
                }),
                night_mode_tz="Europe/Moscow",
            )
            s.add(cs)
            await s.commit()

        with freeze_time("2026-08-13 12:00:00+00:00"):
            with patch.object(self.bot_module, "bot") as mock_bot:
                mock_bot.set_chat_permissions = AsyncMock()
                mock_bot.send_message = AsyncMock()
                await self.bot_module._sanitary_day_tick()

                self.assertTrue(mock_bot.set_chat_permissions.called,
                                "set_chat_permissions should be called for exit")

        async with self.async_session() as s:
            from sqlalchemy import select
            cs2 = (await s.execute(
                select(self.ChatSettings).where(self.ChatSettings.chat_id == -100200)
            )).scalar_one()
            self.assertFalse(cs2.sanitary_days_currently_active)
            self.assertIsNone(cs2.sanitary_days_saved_permissions)

    async def test_28_sanitary_tick_calls_exit_when_datetime_period_ended(self):
        """Период сан. дня со временем закончился (datetime) → exit."""
        # Период: 2026-08-15 09:00-18:00; freeze на 19:00 — вышли
        async with self.async_session() as s:
            cs = self.ChatSettings(
                chat_id=-100201,
                is_enabled=True,
                sanitary_days_enabled=True,
                sanitary_days_currently_active=True,
                sanitary_days_saved_permissions=json.dumps(_all_true_perms_dict()),
                sanitary_days=serialize_sanitary_days_monthly({
                    "2026-08": [_make_pairs("2026-08-15", "2026-08-15", "09:00", "18:00")]
                }),
                night_mode_tz="Europe/Moscow",
            )
            s.add(cs)
            await s.commit()

        with freeze_time("2026-08-15 19:00:00+00:00"):
            with patch.object(self.bot_module, "bot") as mock_bot:
                mock_bot.set_chat_permissions = AsyncMock()
                mock_bot.send_message = AsyncMock()
                await self.bot_module._sanitary_day_tick()

                self.assertTrue(mock_bot.set_chat_permissions.called)

        async with self.async_session() as s:
            from sqlalchemy import select
            cs2 = (await s.execute(
                select(self.ChatSettings).where(self.ChatSettings.chat_id == -100201)
            )).scalar_one()
            self.assertFalse(cs2.sanitary_days_currently_active)

    async def test_29_sanitary_tick_calls_enter_when_in_period(self):
        """Период сан. дня активен, но sanitary_days_currently_active=False → enter."""
        async with self.async_session() as s:
            cs = self.ChatSettings(
                chat_id=-100202,
                is_enabled=True,
                sanitary_days_enabled=True,
                sanitary_days_currently_active=False,
                sanitary_days=serialize_sanitary_days_monthly({
                    "2026-08": [_make_pairs("2026-08-15", "2026-08-15")]
                }),
                night_mode_tz="Europe/Moscow",
            )
            s.add(cs)
            await s.commit()

        with freeze_time("2026-08-15 12:00:00+00:00"):
            with patch.object(self.bot_module, "bot") as mock_bot:
                mock_bot.set_chat_permissions = AsyncMock()
                mock_bot.get_chat = AsyncMock(return_value=MagicMock(
                    permissions=_make_mock_perms(all_true=True)
                ))
                mock_bot.send_message = AsyncMock()
                await self.bot_module._sanitary_day_tick()

                self.assertTrue(mock_bot.set_chat_permissions.called)

        async with self.async_session() as s:
            from sqlalchemy import select
            cs2 = (await s.execute(
                select(self.ChatSettings).where(self.ChatSettings.chat_id == -100202)
            )).scalar_one()
            self.assertTrue(cs2.sanitary_days_currently_active)
            self.assertIsNotNone(cs2.sanitary_days_saved_permissions)


# ─── Tests: _exit_sanitary_day side effects ────────────────────────────────


class TestExitSanitaryDaySideEffects(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # Очищаем таблицу chat_settings перед каждым тестом.
        async with _async_session() as s:
            from sqlalchemy import delete
            await s.execute(delete(_ChatSettings))
            await s.commit()
        self.async_session = _async_session
        self.ChatSettings = _ChatSettings
        self.bot_module = bot_module

    async def test_30_exit_sanitary_sets_last_sanitary_month(self):
        """При выходе — last_sanitary_month = текущий месяц."""
        async with self.async_session() as s:
            cs = self.ChatSettings(
                chat_id=-100300,
                is_enabled=True,
                sanitary_days_enabled=True,
                sanitary_days_currently_active=True,
                sanitary_days_saved_permissions=json.dumps(_all_true_perms_dict()),
                sanitary_days=serialize_sanitary_days_monthly({
                    "2026-08": [_make_pairs("2026-08-10", "2026-08-12")]
                }),
                night_mode_tz="Europe/Moscow",
            )
            s.add(cs)
            await s.commit()

        with freeze_time("2026-08-13 12:00:00+00:00"):
            with patch.object(self.bot_module, "bot") as mock_bot:
                mock_bot.set_chat_permissions = AsyncMock()
                await self.bot_module._exit_sanitary_day(cs)

        async with self.async_session() as s:
            from sqlalchemy import select
            cs2 = (await s.execute(
                select(self.ChatSettings).where(self.ChatSettings.chat_id == -100300)
            )).scalar_one()
            self.assertEqual(cs2.last_sanitary_month, "2026-08")

    async def test_31_exit_sanitary_clears_current_month_from_json(self):
        """При выходе — текущий месяц удаляется из JSON."""
        async with self.async_session() as s:
            cs = self.ChatSettings(
                chat_id=-100301,
                is_enabled=True,
                sanitary_days_currently_active=True,
                sanitary_days_saved_permissions=json.dumps(_all_true_perms_dict()),
                sanitary_days=json.dumps({
                    "2026-07": [["2026-07-15", "2026-07-15"]],
                    "2026-08": [["2026-08-10", "2026-08-12"]]
                }),
                night_mode_tz="Europe/Moscow",
            )
            s.add(cs)
            await s.commit()

        with freeze_time("2026-08-13 12:00:00+00:00"):
            with patch.object(self.bot_module, "bot") as mock_bot:
                mock_bot.set_chat_permissions = AsyncMock()
                await self.bot_module._exit_sanitary_day(cs)

        async with self.async_session() as s:
            from sqlalchemy import select
            cs2 = (await s.execute(
                select(self.ChatSettings).where(self.ChatSettings.chat_id == -100301)
            )).scalar_one()
            data = json.loads(cs2.sanitary_days)
            self.assertNotIn("2026-08", data,
                              "Current month should be removed from JSON after exit")
            self.assertIn("2026-07", data,
                          "Past months should be preserved")


# ─── Tests: _enter_sanitary_day priority over night mode ──────────────────


class TestSanitaryPriorityOverNightMode(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # Очищаем таблицу chat_settings перед каждым тестом.
        async with _async_session() as s:
            from sqlalchemy import delete
            await s.execute(delete(_ChatSettings))
            await s.commit()
        self.async_session = _async_session
        self.ChatSettings = _ChatSettings
        self.bot_module = bot_module

    async def test_32_enter_sanitary_exits_night_first(self):
        """Если night_mode_currently_active=True при входе в sanitary →
        _exit_night_mode вызывается ПЕРЕД snapshot."""
        async with self.async_session() as s:
            cs = self.ChatSettings(
                chat_id=-100400,
                is_enabled=True,
                night_mode_currently_active=True,
                night_mode_saved_permissions=json.dumps(_all_true_perms_dict()),
                night_mode_start="23:00",
                night_mode_end="07:00",
                night_mode_tz="Europe/Moscow",
            )
            s.add(cs)
            await s.commit()

        with patch.object(self.bot_module, "bot") as mock_bot:
            mock_bot.set_chat_permissions = AsyncMock()
            mock_bot.get_chat = AsyncMock(return_value=MagicMock(
                permissions=_make_mock_perms(all_true=True)
            ))
            mock_bot.send_message = AsyncMock()
            await self.bot_module._enter_sanitary_day(cs)
            # Минимум 2 вызова: exit night (restore) + apply lockdown
            self.assertGreaterEqual(
                mock_bot.set_chat_permissions.call_count, 2,
                f"Expected ≥2 set_chat_permissions calls (exit night + apply lockdown), "
                f"got {mock_bot.set_chat_permissions.call_count}"
            )

        async with self.async_session() as s:
            from sqlalchemy import select
            cs2 = (await s.execute(
                select(self.ChatSettings).where(self.ChatSettings.chat_id == -100400)
            )).scalar_one()
            self.assertFalse(cs2.night_mode_currently_active,
                              "Night mode should be exited before entering sanitary")
            self.assertIsNone(cs2.night_mode_saved_permissions)
            self.assertTrue(cs2.sanitary_days_currently_active,
                            "Sanitary day should be active after enter")


# ─── Tests: _startup_recovery ─────────────────────────────────────────────


class TestStartupRecovery(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # Очищаем таблицу chat_settings перед каждым тестом.
        async with _async_session() as s:
            from sqlalchemy import delete
            await s.execute(delete(_ChatSettings))
            await s.commit()
        self.async_session = _async_session
        self.ChatSettings = _ChatSettings
        self.bot_module = bot_module

    async def test_33_startup_recovery_clears_stuck_night_mode(self):
        """Чат с зависшим night_mode_currently_active=True (окно вышло) → recovery."""
        async with self.async_session() as s:
            cs = self.ChatSettings(
                chat_id=-100500,
                is_enabled=True,
                night_mode_enabled=True,
                night_mode_currently_active=True,  # ЗАВИС
                night_mode_saved_permissions=json.dumps(_all_true_perms_dict()),
                night_mode_start="02:00",
                night_mode_end="03:00",
                night_mode_tz="Europe/Moscow",
            )
            s.add(cs)
            await s.commit()

        # 12:00 UTC = 15:00 MSK — вне окна 02:00-03:00
        with freeze_time("2026-08-15 12:00:00+00:00"):
            with patch.object(self.bot_module, "bot") as mock_bot:
                mock_bot.set_chat_permissions = AsyncMock()
                mock_bot.get_chat = AsyncMock(return_value=MagicMock(
                    permissions=_make_mock_perms(all_true=True)
                ))
                mock_bot.send_message = AsyncMock()
                await self.bot_module._startup_recovery()

        async with self.async_session() as s:
            from sqlalchemy import select
            cs2 = (await s.execute(
                select(self.ChatSettings).where(self.ChatSettings.chat_id == -100500)
            )).scalar_one()
            self.assertFalse(cs2.night_mode_currently_active,
                              "Stuck night_mode_currently_active should be cleared")


# ─── Tests: Fallback when snapshot is None ────────────────────────────────


class TestExitFallback(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # Очищаем таблицу chat_settings перед каждым тестом.
        async with _async_session() as s:
            from sqlalchemy import delete
            await s.execute(delete(_ChatSettings))
            await s.commit()
        self.async_session = _async_session
        self.ChatSettings = _ChatSettings
        self.bot_module = bot_module

    async def test_34_exit_night_with_no_snapshot_uses_day_default(self):
        """v4.7.12: если day_permissions=None и snapshot=None → восстанавливает
        из системного пресета 'Day default'. admin-права ВСЕГДА False."""
        async with self.async_session() as s:
            cs = self.ChatSettings(
                chat_id=-100600,
                is_enabled=True,
                night_mode_currently_active=True,
                night_mode_saved_permissions=None,
                day_permissions=None,
                # night_mode_enabled НЕ установлен → auto-enter не сработает.
            )
            s.add(cs)
            await s.commit()

        with patch.object(self.bot_module, "bot") as mock_bot:
            mock_bot.set_chat_permissions = AsyncMock()
            mock_bot.send_message = AsyncMock()
            await self.bot_module._exit_night_mode(cs)

            self.assertTrue(mock_bot.set_chat_permissions.called)
            args, kwargs = mock_bot.set_chat_permissions.call_args
            perms = kwargs.get("permissions") or (args[1] if len(args) > 1 else None)
            self.assertIsNotNone(perms)
            # Day default: text/music/photos/videos/other=True.
            for field in ["can_send_messages", "can_send_audios", "can_send_photos",
                          "can_send_videos", "can_send_other_messages"]:
                self.assertTrue(getattr(perms, field, False),
                                f"{field} should be True in Day default")
            # Day default: video_notes/voice_notes/documents/polls=False.
            for field in ["can_send_video_notes", "can_send_voice_notes",
                          "can_send_documents", "can_send_polls",
                          "can_add_web_page_previews"]:
                self.assertFalse(getattr(perms, field, True),
                                 f"{field} should be False in Day default")
            # ГЛАВНОЕ: admin-права ВСЕГДА False (защита v4.7.12).
            for field in ["can_change_info", "can_invite_users", "can_pin_messages"]:
                self.assertFalse(getattr(perms, field, True),
                                 f"{field} must NEVER be True in fallback")

    async def test_35_exit_sanitary_with_no_snapshot_uses_day_default(self):
        """v4.7.12: если day_permissions=None и snapshot=None → восстанавливает
        из системного пресета 'Day default'. admin-права ВСЕГДА False."""
        async with self.async_session() as s:
            cs = self.ChatSettings(
                chat_id=-100601,
                is_enabled=True,
                sanitary_days_currently_active=True,
                sanitary_days_saved_permissions=None,
                day_permissions=None,
                night_mode_tz="Europe/Moscow",
                # night_mode_enabled НЕ установлен → auto-enter не сработает.
            )
            s.add(cs)
            await s.commit()

        with freeze_time("2026-08-13 12:00:00+00:00"):
            with patch.object(self.bot_module, "bot") as mock_bot:
                mock_bot.set_chat_permissions = AsyncMock()
                await self.bot_module._exit_sanitary_day(cs)

                self.assertTrue(mock_bot.set_chat_permissions.called)
                args, kwargs = mock_bot.set_chat_permissions.call_args
                perms = kwargs.get("permissions") or (args[1] if len(args) > 1 else None)
                self.assertIsNotNone(perms)
                # Day default: text/music/photos/videos/other=True.
                for field in ["can_send_messages", "can_send_audios", "can_send_photos",
                              "can_send_videos", "can_send_other_messages"]:
                    self.assertTrue(getattr(perms, field, False),
                                    f"{field} should be True in Day default")
                # ГЛАВНОЕ: admin-права ВСЕГДА False (защита v4.7.12).
                for field in ["can_change_info", "can_invite_users", "can_pin_messages"]:
                    self.assertFalse(getattr(perms, field, True),
                                     f"{field} must NEVER be True in fallback")


# ─── Tests: Granular day_permissions takes priority ──────────────────────


class TestGranularDayPermissionsPriority(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # Очищаем таблицу chat_settings перед каждым тестом.
        async with _async_session() as s:
            from sqlalchemy import delete
            await s.execute(delete(_ChatSettings))
            await s.commit()
        self.async_session = _async_session
        self.ChatSettings = _ChatSettings
        self.bot_module = bot_module

    async def test_36_exit_night_uses_day_permissions_when_set(self):
        """day_permissions задан → restore берётся из него, не из snapshot."""
        day_perms = {
            "can_send_messages": True, "can_send_audios": False,
            "can_send_documents": False, "can_send_photos": False,
            "can_send_videos": False, "can_send_video_notes": False,
            "can_send_voice_notes": False, "can_send_polls": False,
            "can_send_other_messages": False, "can_add_web_page_previews": False,
            "can_change_info": False, "can_invite_users": False, "can_pin_messages": False,
        }
        async with self.async_session() as s:
            cs = self.ChatSettings(
                chat_id=-100700,
                is_enabled=True,
                night_mode_currently_active=True,
                night_mode_saved_permissions=json.dumps(_all_true_perms_dict()),
                day_permissions=json.dumps(day_perms),
            )
            s.add(cs)
            await s.commit()

        with patch.object(self.bot_module, "bot") as mock_bot:
            mock_bot.set_chat_permissions = AsyncMock()
            mock_bot.send_message = AsyncMock()
            await self.bot_module._exit_night_mode(cs)

            self.assertTrue(mock_bot.set_chat_permissions.called)
            args, kwargs = mock_bot.set_chat_permissions.call_args
            perms = kwargs.get("permissions") or (args[1] if len(args) > 1 else None)
            self.assertTrue(getattr(perms, "can_send_messages", False),
                            "can_send_messages should be True from day_permissions")
            self.assertFalse(getattr(perms, "can_send_audios", True),
                             "can_send_audios should be False from day_permissions (not snapshot)")


# ─── Tests: v4.7.12 Auto-transition & preset priority ────────────────────


class TestAutoTransitionV4712(unittest.IsolatedAsyncioTestCase):
    """v4.7.12: автопереходы sanitary→night, night→night, и приоритет day preset."""

    async def asyncSetUp(self):
        async with _async_session() as s:
            from sqlalchemy import delete
            await s.execute(delete(_ChatSettings))
            await s.commit()
        self.async_session = _async_session
        self.ChatSettings = _ChatSettings
        self.bot_module = bot_module

    async def test_37_exit_sanitary_auto_enters_night_when_in_window(self):
        """Выход из sanitary day при включённом night mode и попадании в окно →
        сразу _enter_night_mode (не ждём tick)."""
        async with self.async_session() as s:
            cs = self.ChatSettings(
                chat_id=-100800,
                is_enabled=True,
                night_mode_enabled=True,
                night_mode_start="23:00",
                night_mode_end="07:00",
                night_mode_tz="Europe/Moscow",
                sanitary_days_currently_active=True,
                sanitary_days_saved_permissions=None,
                day_permissions=None,
            )
            s.add(cs)
            await s.commit()

        # 23:30 UTC = 02:30 MSK — попадает в окно 23:00-07:00.
        with freeze_time("2026-08-13 23:30:00+00:00"):
            with patch.object(self.bot_module, "bot") as mock_bot:
                mock_bot.set_chat_permissions = AsyncMock()
                mock_bot.get_chat = AsyncMock(return_value=MagicMock(
                    permissions=_make_mock_perms(all_true=True)
                ))
                mock_bot.send_message = AsyncMock()
                await self.bot_module._exit_sanitary_day(cs)

                # После exit sanitary → enter night → set_chat_permissions вызван.
                self.assertTrue(mock_bot.set_chat_permissions.called)

        async with self.async_session() as s:
            from sqlalchemy import select
            cs2 = (await s.execute(
                select(self.ChatSettings).where(self.ChatSettings.chat_id == -100800)
            )).scalar_one()
            self.assertFalse(cs2.sanitary_days_currently_active,
                             "Sanitary should be cleared")
            self.assertTrue(cs2.night_mode_currently_active,
                            "Night mode should be auto-entered after sanitary exit")
            self.assertIsNotNone(cs2.night_mode_saved_permissions,
                                 "Night snapshot should be saved")

    async def test_38_exit_sanitary_restores_day_when_night_disabled(self):
        """Выход из sanitary day когда night_mode_enabled=False →
        восстанавливает day preset (НЕ входит в night)."""
        async with self.async_session() as s:
            cs = self.ChatSettings(
                chat_id=-100801,
                is_enabled=True,
                night_mode_enabled=False,
                night_mode_start="23:00",
                night_mode_end="07:00",
                night_mode_tz="Europe/Moscow",
                sanitary_days_currently_active=True,
                sanitary_days_saved_permissions=None,
                day_permissions=None,
            )
            s.add(cs)
            await s.commit()

        # 23:30 UTC = 02:30 MSK — попадает в окно, но night_mode_enabled=False.
        with freeze_time("2026-08-13 23:30:00+00:00"):
            with patch.object(self.bot_module, "bot") as mock_bot:
                mock_bot.set_chat_permissions = AsyncMock()
                mock_bot.send_message = AsyncMock()
                await self.bot_module._exit_sanitary_day(cs)

                self.assertTrue(mock_bot.set_chat_permissions.called)

        async with self.async_session() as s:
            from sqlalchemy import select
            cs2 = (await s.execute(
                select(self.ChatSettings).where(self.ChatSettings.chat_id == -100801)
            )).scalar_one()
            self.assertFalse(cs2.sanitary_days_currently_active)
            self.assertFalse(cs2.night_mode_currently_active,
                             "Night mode should NOT be entered when disabled")

    async def test_39_exit_night_auto_reenters_when_still_in_window(self):
        """_exit_night_mode при включённом night mode и попадании в окно →
        сразу _enter_night_mode (свежий snapshot)."""
        async with self.async_session() as s:
            cs = self.ChatSettings(
                chat_id=-100802,
                is_enabled=True,
                night_mode_enabled=True,
                night_mode_start="23:00",
                night_mode_end="07:00",
                night_mode_tz="Europe/Moscow",
                night_mode_currently_active=True,
                night_mode_saved_permissions=json.dumps(_all_true_perms_dict()),
                day_permissions=None,
            )
            s.add(cs)
            await s.commit()

        # 23:30 UTC = 02:30 MSK — в окне.
        with freeze_time("2026-08-13 23:30:00+00:00"):
            with patch.object(self.bot_module, "bot") as mock_bot:
                mock_bot.set_chat_permissions = AsyncMock()
                mock_bot.get_chat = AsyncMock(return_value=MagicMock(
                    permissions=_make_mock_perms(all_true=True)
                ))
                mock_bot.send_message = AsyncMock()
                await self.bot_module._exit_night_mode(cs)

                self.assertTrue(mock_bot.set_chat_permissions.called)

        async with self.async_session() as s:
            from sqlalchemy import select
            cs2 = (await s.execute(
                select(self.ChatSettings).where(self.ChatSettings.chat_id == -100802)
            )).scalar_one()
            # Флаги были сняты, потом _enter_night_mode поставил заново.
            self.assertTrue(cs2.night_mode_currently_active,
                            "Night should be re-entered (auto-transition)")
            self.assertIsNotNone(cs2.night_mode_saved_permissions,
                                 "Fresh snapshot should be saved")

    async def test_40_exit_night_restores_day_when_out_of_window(self):
        """_exit_night_mode при включённом night mode но НЕ в окне →
        восстанавливает day preset."""
        async with self.async_session() as s:
            cs = self.ChatSettings(
                chat_id=-100803,
                is_enabled=True,
                night_mode_enabled=True,
                night_mode_start="23:00",
                night_mode_end="07:00",
                night_mode_tz="Europe/Moscow",
                night_mode_currently_active=True,
                night_mode_saved_permissions=json.dumps(_all_true_perms_dict()),
                day_permissions=None,
            )
            s.add(cs)
            await s.commit()

        # 12:00 UTC = 15:00 MSK — вне окна.
        with freeze_time("2026-08-13 12:00:00+00:00"):
            with patch.object(self.bot_module, "bot") as mock_bot:
                mock_bot.set_chat_permissions = AsyncMock()
                mock_bot.send_message = AsyncMock()
                await self.bot_module._exit_night_mode(cs)

                self.assertTrue(mock_bot.set_chat_permissions.called)

        async with self.async_session() as s:
            from sqlalchemy import select
            cs2 = (await s.execute(
                select(self.ChatSettings).where(self.ChatSettings.chat_id == -100803)
            )).scalar_one()
            self.assertFalse(cs2.night_mode_currently_active,
                             "Night should be OFF when out of window")
            self.assertIsNone(cs2.night_mode_saved_permissions,
                              "Snapshot should be cleared")

    async def test_41_resolve_day_perms_priority_chat_preset_first(self):
        """_resolve_day_perms: chat_preset имеет приоритет над system_default."""
        custom_day_perms = {
            "can_send_messages": True, "can_send_audios": True,
            "can_send_documents": False, "can_send_photos": True,
            "can_send_videos": False, "can_send_video_notes": False,
            "can_send_voice_notes": False, "can_send_polls": False,
            "can_send_other_messages": False, "can_add_web_page_previews": False,
            "can_change_info": False, "can_invite_users": False, "can_pin_messages": False,
        }
        async with self.async_session() as s:
            cs = self.ChatSettings(
                chat_id=-100804,
                is_enabled=True,
                day_permissions=json.dumps(custom_day_perms),
            )
            s.add(cs)
            await s.commit()

        perms, source = await self.bot_module._resolve_day_perms(cs)
        self.assertEqual(source, "chat_preset")
        # video_notes=False (chat_preset), но videos=False (chat_preset).
        # Если бы взяли system_default — videos было бы True.
        self.assertFalse(getattr(perms, "can_send_videos", True),
                         "videos should be False from chat_preset")
        self.assertFalse(getattr(perms, "can_send_other_messages", True),
                         "other_messages should be False from chat_preset")

    async def test_42_resolve_day_perms_falls_back_to_system_default(self):
        """_resolve_day_perms: если day_permissions=None → system_default."""
        async with self.async_session() as s:
            cs = self.ChatSettings(
                chat_id=-100805,
                is_enabled=True,
                day_permissions=None,
            )
            s.add(cs)
            await s.commit()

        perms, source = await self.bot_module._resolve_day_perms(cs)
        self.assertEqual(source, "system_default",
                         "Should use system 'Day default' when day_permissions=None")
        # Day default: videos=True, other_messages=True, video_notes=False.
        self.assertTrue(getattr(perms, "can_send_videos", False),
                        "videos should be True in Day default")
        self.assertTrue(getattr(perms, "can_send_other_messages", False),
                        "other_messages should be True in Day default")
        self.assertFalse(getattr(perms, "can_send_video_notes", True),
                         "video_notes should be False in Day default")
        # admin perms OFF.
        self.assertFalse(getattr(perms, "can_change_info", True))
        self.assertFalse(getattr(perms, "can_invite_users", True))
        self.assertFalse(getattr(perms, "can_pin_messages", True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
