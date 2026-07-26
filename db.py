"""
db.py — Асинхронный SQLAlchemy: модели, сессии.
База: /app/data/shadow_logs.db  |  WAL режим для конкурентного доступа.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timezone

from sqlalchemy import (
    Column, Integer, BigInteger, String, DateTime, ForeignKey, Text, Float, Boolean, event, text, select,
)
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, relationship


# ── Хэширование паролей для веб-панели ───────────────────────────────────────
# PBKDF2-HMAC-SHA256, 200 000 итераций, соль 16 байт. Возвращает 'salt:hash'.

def _hash_password(password: str, salt: str | None = None) -> str:
    """PBKDF2-HMAC-SHA256 хэш пароля с солью. Возвращает 'salt:hash'."""
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000)
    return f"{salt}:{h.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    """Проверка пароля против 'salt:hash' строки."""
    try:
        salt, expected_hex = stored.split(":", 1)
    except ValueError:
        return False
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000)
    return hmac.compare_digest(h.hex(), expected_hex)


# ── Engine ──────────────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "/app/data/shadow_logs.db")
DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ── Включаем WAL при подключении ────────────────────────────────────────────
@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


# ── Base ────────────────────────────────────────────────────────────────────
class Base(DeclarativeBase):
    pass


# ── Models ──────────────────────────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"

    user_id = Column(BigInteger, primary_key=True)
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    last_name = Column(String(255), nullable=True)
    last_seen = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    punishments = relationship("Punishment", back_populates="user", foreign_keys="Punishment.user_id")


class Moderator(Base):
    __tablename__ = "moderators"

    mod_id = Column(BigInteger, primary_key=True)
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)

    punishments = relationship("Punishment", back_populates="moderator", foreign_keys="Punishment.mod_id")


class Punishment(Base):
    __tablename__ = "punishments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    mod_id = Column(BigInteger, ForeignKey("moderators.mod_id"), nullable=False)
    chat_id = Column(BigInteger, nullable=False)
    action_type = Column(String(20), nullable=False)        # mute / warn / ban / unmute / unwarn / unban
    duration_seconds = Column(Integer, nullable=True)        # NULL для warn/ban/unmute; для warn = кол-во поинтов
    reason = Column(Text, nullable=True)
    message_text = Column(Text, nullable=True)               # текст удалённого сообщения нарушителя
    permissions_snapshot = Column(Text, nullable=True)        # JSON: пермишены пользователя ДО санкции
    report_message_id = Column(BigInteger, nullable=True)    # ID пересланного сообщения в канале отчётов (для медиа)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    # ── Soft-revoke: помечаем что санкция снята (для unwarn/unban/unmute) ──
    is_revoked = Column(Boolean, default=False, nullable=False)   # True = санкция снята, не учитывать в счётчиках
    revoked_at = Column(DateTime, nullable=True)                  # когда снята
    revoked_by_mod_id = Column(BigInteger, nullable=True)         # кто из модераторов снял

    user = relationship("User", back_populates="punishments", foreign_keys=[user_id])
    moderator = relationship("Moderator", back_populates="punishments", foreign_keys=[mod_id])


class ChatAdmin(Base):
    """Дополнительные админы, добавленные через /addadmin в личке."""
    __tablename__ = "chat_admins"

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(BigInteger, nullable=False)
    user_id = Column(BigInteger, nullable=False)
    added_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ChatSettings(Base):
    """Настройки чата: пороги варнов, хэштег, репорт-чат и т.д."""
    __tablename__ = "chat_settings"

    chat_id = Column(BigInteger, primary_key=True)
    hashtag = Column(String(64), nullable=True)              # хэштег чата (#Бэбэй, #Деградач)
    report_chat_id = Column(BigInteger, nullable=True)       # чат для отчётов (NULL = использовать env REPORT_CHAT_ID)
    warns_to_mute = Column(Integer, default=3)               # варнов до мьюта (0 = отключено)
    mute_duration_seconds = Column(Integer, default=3600)    # длительность мьюта по умолчанию (1ч)
    warns_to_ban = Column(Integer, default=5)                # варнов до бана (0 = отключено)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class WebUser(Base):
    """Учётные записи администраторов веб-панели.

    SU (super-user) — единственный, чьё имя = 'su', пароль хранится в env WEB_PASSWORD
    (в БЕЗ хэша — сверка идёт напрямую через == в web_app.py).
    Все остальные — созданные через /admin/users (v4.4: по TGID):
      пароль автогенерируется и сохраняется в password_hash (PBKDF2-HMAC-SHA256),
      профиль заполняется из Telegram (tg_user_id / tg_first_name / tg_last_name / tg_username).
    Логин (username) = @username из Telegram (без @).

    v4.4.6: role — 'su' | 'admin' | 'moderator'.
      • 'su'         — полный доступ (is_su=True, role='su' — синонимы)
      • 'admin'      — управление чатами/модераторами, без управления админами
      • 'moderator'  — только просмотр логов в веб-панели
    Поле is_su сохранено для обратной совместимости (всегда role='su' ⇔ is_su=True).
    """
    __tablename__ = "web_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=True)        # NULL только для 'su' (пароль из env)
    is_su = Column(Boolean, default=False, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    created_by = Column(String(64), nullable=True)             # username создателя
    last_login_at = Column(DateTime, nullable=True)
    # ── v4.4: привязка к Telegram ────────────────────────────────────────
    tg_user_id = Column(BigInteger, nullable=True, unique=True, index=True)
    tg_first_name = Column(String(255), nullable=True)
    tg_last_name = Column(String(255), nullable=True)
    tg_username = Column(String(255), nullable=True)            # @username из TG (без @, lowercase)
    # ── v4.4.6: role ────────────────────────────────────────────────────
    # 'su' / 'admin' / 'moderator'. Для существующих записей при миграции:
    #   is_su=True → role='su'; is_su=False → role='admin'.
    role = Column(String(16), nullable=False, default="admin")


# ── Init / Shutdown ────────────────────────────────────────────────────────
async def init_db() -> None:
    """Создаёт таблицы при первом запуске + миграции для новых колонок."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # ── Миграция: добавляем report_message_id если колонка отсутствует ──
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(punishments)"))
        columns = [row[1] for row in result.fetchall()]
        if "report_message_id" not in columns:
            await conn.execute(text(
                "ALTER TABLE punishments ADD COLUMN report_message_id BIGINT NULL"
            ))

    # ── Миграция: добавляем report_chat_id в chat_settings если колонка отсутствует ──
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(chat_settings)"))
        columns = [row[1] for row in result.fetchall()]
        if "report_chat_id" not in columns:
            await conn.execute(text(
                "ALTER TABLE chat_settings ADD COLUMN report_chat_id BIGINT NULL"
            ))

    # ── Миграция: добавляем soft-revoke колонки в punishments (v4.2) ──
    # is_revoked / revoked_at / revoked_by_mod_id — для команд !unwarn / !unban / !unmute
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(punishments)"))
        columns = [row[1] for row in result.fetchall()]
        if "is_revoked" not in columns:
            await conn.execute(text(
                "ALTER TABLE punishments ADD COLUMN is_revoked BOOLEAN NOT NULL DEFAULT 0"
            ))
        if "revoked_at" not in columns:
            await conn.execute(text(
                "ALTER TABLE punishments ADD COLUMN revoked_at DATETIME NULL"
            ))
        if "revoked_by_mod_id" not in columns:
            await conn.execute(text(
                "ALTER TABLE punishments ADD COLUMN revoked_by_mod_id BIGINT NULL"
            ))

    # ── Миграция: привязка веб-юзеров к Telegram (v4.4) ────────────────
    # tg_user_id (unique) / tg_first_name / tg_last_name / tg_username —
    # для создания админов через TGID с автозаполнением профиля из Telegram.
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(web_users)"))
        columns = [row[1] for row in result.fetchall()]
        if "tg_user_id" not in columns:
            await conn.execute(text(
                "ALTER TABLE web_users ADD COLUMN tg_user_id BIGINT NULL"
            ))
        if "tg_first_name" not in columns:
            await conn.execute(text(
                "ALTER TABLE web_users ADD COLUMN tg_first_name VARCHAR(255) NULL"
            ))
        if "tg_last_name" not in columns:
            await conn.execute(text(
                "ALTER TABLE web_users ADD COLUMN tg_last_name VARCHAR(255) NULL"
            ))
        if "tg_username" not in columns:
            await conn.execute(text(
                "ALTER TABLE web_users ADD COLUMN tg_username VARCHAR(255) NULL"
            ))
    # Уникальный индекс на tg_user_id (создаём после колонки; IF NOT EXISTS для идемпотентности).
    # SQLite поддерживает CREATE UNIQUE INDEX IF NOT EXISTS.
    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_web_users_tg_user_id "
            "ON web_users (tg_user_id) WHERE tg_user_id IS NOT NULL"
        ))

    # ── Миграция: добавляем role в web_users (v4.4.6) ──────────────────
    # role = 'su' | 'admin' | 'moderator'. Для существующих записей:
    #   is_su=True → role='su'; is_su=False → role='admin'.
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(web_users)"))
        columns = [row[1] for row in result.fetchall()]
        if "role" not in columns:
            # Добавляем колонку с дефолтом 'admin' (подойдёт для всех не-SU).
            await conn.execute(text(
                "ALTER TABLE web_users ADD COLUMN role VARCHAR(16) NOT NULL DEFAULT 'admin'"
            ))
            # Помечаем SU-аккаунты role='su'
            await conn.execute(text(
                "UPDATE web_users SET role='su' WHERE is_su=1"
            ))

    # ── Seed: гарантируем что SU-аккаунт существует в web_users ──────────
    # SU не имеет password_hash — пароль берётся из env WEB_PASSWORD при логине.
    # Это позволяет менять SU-пароль через env без перезаписи БД.
    async with async_session() as session:
        existing_su = (
            await session.execute(
                select(WebUser).where(WebUser.username == "su")
            )
        ).scalar_one_or_none()
        if existing_su is None:
            session.add(WebUser(
                username="su",
                password_hash=None,
                is_su=True,
                is_active=True,
                created_by="system",
                role="su",
            ))
            await session.commit()
        else:
            # На случай если SU существует, но role ещё не проставлен (старая БД)
            if existing_su.role != "su":
                existing_su.role = "su"
                await session.commit()


async def get_session() -> AsyncSession:
    """Фабрика сессий — использовать через async with."""
    async with async_session() as session:
        yield session
