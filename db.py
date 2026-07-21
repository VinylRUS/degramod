"""
db.py — Асинхронный SQLAlchemy: модели, сессии.
База: /app/data/shadow_logs.db  |  WAL режим для конкурентного доступа.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from sqlalchemy import (
    Column, Integer, BigInteger, String, DateTime, ForeignKey, Text, Float, event,
)
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, relationship

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
    action_type = Column(String(20), nullable=False)        # mute / warn / ban / unmute
    duration_seconds = Column(Integer, nullable=True)        # NULL для warn/ban/unmute; для warn = кол-во поинтов
    reason = Column(Text, nullable=True)
    message_text = Column(Text, nullable=True)               # текст удалённого сообщения нарушителя
    permissions_snapshot = Column(Text, nullable=True)        # JSON: пермишены пользователя ДО санкции
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

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
    """Настройки чата: пороги варнов, хэштег и т.д."""
    __tablename__ = "chat_settings"

    chat_id = Column(BigInteger, primary_key=True)
    hashtag = Column(String(64), nullable=True)              # хэштег чата (#Бэбэй, #Деградач)
    warns_to_mute = Column(Integer, default=3)               # варнов до мьюта (0 = отключено)
    mute_duration_seconds = Column(Integer, default=3600)    # длительность мьюта по умолчанию (1ч)
    warns_to_ban = Column(Integer, default=5)                # варнов до бана (0 = отключено)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# ── Init / Shutdown ────────────────────────────────────────────────────────
async def init_db() -> None:
    """Создаёт таблицы при первом запуске."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    """Фабрика сессий — использовать через async with."""
    async with async_session() as session:
        yield session
