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
    # ── v4.5.1: consumed_by_action — какой авто-действие «погасило» варн ──
    # 'auto_mute' | 'auto_ban' | NULL. Когда _check_warn_threshold триггерит
    # автомьют или автобан, все активные варны юзера получают эту метку —
    # _count_warns их больше не считает, повторный !warn начинает счёт с 0.
    # is_revoked остаётся False — варн «активен» в логе (видно в веб-панели),
    # но не влияет на пороги. Так исправляется баг с повторным триггером
    # автомьюта при каждом следующем !warn.
    consumed_by_action = Column(String(20), nullable=True)

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
    """Настройки чата: пороги варнов, хэштег, репорт-чат и т.д.

    v4.4.7: добавлены поля для авто-обнаружения чатов и управления доступом:
      • title         — название чата из Telegram (для отображения в веб-панели)
      • is_enabled    — если False, бот полностью игнорирует чат (никакие команды)
      • is_private    — закрытый чат (напр. платный контент-чат): админ-уровень
                        туда не имеет доступа, только SU и явно привязанные модераторы
      • is_report_chat — если True, этот чат используется как склад отчётов по умолчанию
                         (заменяет env REPORT_CHAT_ID; чат может быть одновременно
                         и обычным чатом для модерации, и репорт-чатом — неважно)
    """
    __tablename__ = "chat_settings"

    chat_id = Column(BigInteger, primary_key=True)
    hashtag = Column(String(64), nullable=True)              # хэштег чата (#Бэбэй, #Деградач)
    report_chat_id = Column(BigInteger, nullable=True)       # чат для отчётов (NULL = global default)
    warns_to_mute = Column(Integer, default=3)               # варнов до мьюта (0 = отключено)
    mute_duration_seconds = Column(Integer, default=3600)    # длительность мьюта по умолчанию (1ч)
    warns_to_ban = Column(Integer, default=5)                # варнов до бана (0 = отключено)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    # ── v4.4.7 ──────────────────────────────────────────────────────────
    title = Column(String(255), nullable=True)               # название чата (из TG, snapshot)
    is_enabled = Column(Boolean, default=True, nullable=False)  # False = бот игнорирует чат
    is_private = Column(Boolean, default=False, nullable=False)  # закрытый чат (админ не лезет)
    is_report_chat = Column(Boolean, default=False, nullable=False)  # склад отчётов
    # ── v4.5.2 ──────────────────────────────────────────────────────────
    # CAS integration (#2): проверка каждого нового участника по api.cas.chat.
    # По умолчанию OFF — включается per-chat через веб-панель или /cas on.
    cas_check_enabled = Column(Boolean, default=False, nullable=False)
    # Link filter (#8): блокировка ссылок кроме allowlist. Allowlist хранится
    # отдельно в link_allowlist (global + per-chat).
    link_filter_enabled = Column(Boolean, default=False, nullable=False)
    link_filter_action = Column(String(16), default="delete", nullable=False)  # delete|warn|mute|ban
    # Auto-delete command messages after execution (#8 user-requested):
    # если True — бот удаляет сообщение модератора с командой (текущее поведение).
    # Если False — команда остаётся видимой в чате (полезно для прозрачности).
    # NULL = использовать глобальную настройку (link_filter_action_default).
    auto_delete_commands = Column(Boolean, default=True, nullable=False)
    # Warn decay (#45): варны старше N дней не учитываются в счётчике.
    # 0 = отключено (варны копятся вечно). Типичное значение: 30 дней.
    warn_decay_days = Column(Integer, default=0, nullable=False)
    # Auto night mode (user-requested #29-33): автоматическое включение
    # ограничительных прав в заданное время. Background-таска в bot.py
    # проверяет расписание каждую минуту.
    night_mode_enabled = Column(Boolean, default=False, nullable=False)
    night_mode_start = Column(String(5), default="23:00", nullable=False)  # HH:MM (МСК)
    night_mode_end = Column(String(5), default="07:00", nullable=False)    # HH:MM (МСК)
    # JSON-снапшот прав, применяемых ночью (ChatPermissions). По умолчанию —
    # запрет всех медиа, оставить только текстовые сообщения.
    night_mode_permissions = Column(Text, nullable=True)
    # JSON-снапшот прав ДО ночного режима — восстанавливается при выходе из него.
    # Заполняется при первом входе в ночной режим; обновляется, если SU меняет
    # права вручную днём (таска перезаписывает snapshot каждый день при входе).
    night_mode_saved_permissions = Column(Text, nullable=True)
    # Флаг: сейчас активен ночной режим (для логирования и веб-панели).
    night_mode_currently_active = Column(Boolean, default=False, nullable=False)
    # ── v4.5.3: расширенная настройка ночного режима ───────────────────────
    # IANA timezone (Europe/Moscow, Asia/Yekaterinburg, ...). По умолчанию MSK.
    # Если зона некорректна — fallback на Europe/Moscow.
    night_mode_tz = Column(String(64), default="Europe/Moscow", nullable=False)
    # Отдельное расписание на субботу+воскресенье. NULL = использовать
    # будничное расписание (start/end).
    night_mode_weekend_start = Column(String(5), nullable=True)
    night_mode_weekend_end = Column(String(5), nullable=True)
    # Отправлять ли сообщение в чат при входе/выходе из ночного режима.
    night_mode_notify = Column(Boolean, default=False, nullable=False)
    # Кастомный текст уведомления при входе (NULL = дефолтный шаблон).
    night_mode_notify_enter_msg = Column(Text, nullable=True)
    # Кастомный текст уведомления при выходе (NULL = дефолтный шаблон).
    night_mode_notify_exit_msg = Column(Text, nullable=True)

    # ── v4.5.4: Санитарный день ─────────────────────────────────────────
    # Список дат/диапазонов, в которые чат переводится в полный lockdown
    # (ChatPermissions → all False). Модераторов это НЕ касается: их права
    # выданы через promote_chat_member (Telegram admin rights), которые
    # override'ят chat-level ChatPermissions. Обычные участники — muted.
    # В sanitary day ночной режим НЕ дёргает права чата: если sanitary day
    # начался пока night был активен — night корректно восстанавливает
    # снапшот (как будто night закончился), потом sanitary берёт управление.
    # Когда sanitary day заканчивается — восстанавливаем snapshot, и night
    # mode tick может снова войти в ночной режим если окно всё ещё активно.
    # Формат: JSON-массив пар [["YYYY-MM-DD","YYYY-MM-DD"], ...].
    # Однодневный санитарный день — пара с одинаковыми датами.
    # NULL или "[]" — санитарных дней нет.
    sanitary_days = Column(Text, nullable=True)
    # JSON-снапшот прав чата ДО входа в санитарный день — восстанавливается
    # при выходе из него. Аналог night_mode_saved_permissions.
    sanitary_days_saved_permissions = Column(Text, nullable=True)
    # Флаг: сейчас активен санитарный день (для логирования и веб-панели).
    sanitary_days_currently_active = Column(Boolean, default=False, nullable=False)


class WordFilter(Base):
    """v4.5.2: Word filter (#7) — список запрещённых слов/паттернов для чата.

    Хранится отдельно от ChatSettings, т.к. паттернов может быть много.
    chat_id=0 — глобальные паттерны (применяются ко всем чатам, где word_filter
    включён — но мы не делаем per-chat toggle для word filter в этой версии;
    паттерны работают per-chat, для глобального default используется chat_id=0).
    """
    __tablename__ = "word_filters"

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(BigInteger, nullable=False, index=True)  # 0 = global default
    pattern = Column(String(255), nullable=False)             # строка или regex
    is_regex = Column(Boolean, default=False, nullable=False)  # True — re.search, False — lowercase in
    action = Column(String(16), default="delete", nullable=False)  # delete|warn|mute|ban
    mute_duration = Column(Integer, nullable=True)            # сек, для action=mute (NULL = chat default)
    created_by = Column(BigInteger, nullable=True)            # mod_id создателя
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    is_active = Column(Boolean, default=True, nullable=False)


class LinkAllowlist(Base):
    """v4.5.2: Link filter (#8) — список разрешённых доменов.

    chat_id=0 — глобальный allowlist (применяется ко всем чатам).
    Конкретный чат может добавлять свои домены (chat_id=<id>).
    Сравнение по подстроке домена: 't.me' разрешит все поддомены.
    """
    __tablename__ = "link_allowlist"

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(BigInteger, nullable=False, index=True)  # 0 = global
    domain = Column(String(255), nullable=False)              # без схемы, без пути (напр. 't.me')
    created_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class BannedStickerPack(Base):
    """v4.5.2: Banned sticker packs (#15).

    Pack идентифицируется по pack_name (то, что в Telegram StickerSet.name).
    Ссылка вида https://t.me/addstickers/<pack_name> приводится к pack_name.

    Punishment — что делать при использовании стикера из пака:
      • delete — только удалить сообщение (по умолчанию)
      • warn   — выдать варн (+ удалить)
      • mute   — замьютить на mute_duration секунд (+ удалить)
      • ban    — забанить (+ удалить)
    """
    __tablename__ = "banned_sticker_packs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(BigInteger, nullable=False, index=True)  # 0 = global (all chats)
    pack_name = Column(String(255), nullable=False)            # Telegram StickerSet.name
    punishment = Column(String(16), default="delete", nullable=False)  # delete|warn|mute|ban
    mute_duration = Column(Integer, nullable=True)              # сек, для punishment=mute
    reason = Column(Text, nullable=True)                       # почему пак забанен (для аудита)
    added_by_mod_id = Column(BigInteger, nullable=True)        # кто добавил (mod_id)
    added_via = Column(String(16), default="manual", nullable=False)  # manual | auto_ban | web
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    is_active = Column(Boolean, default=True, nullable=False)


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
    # ── v4.5: аватарка из Telegram ───────────────────────────────────────
    # timestamp последнего успешного обновления аватарки (для инвалидации
    # кэша в шаблоне через ?v=<ts>). Сама аватарка хранится локально в
    # <data_dir>/avatars/<tg_user_id>.jpg, чтобы не дёргать TG API на каждом
    # рендере и не хранить base64 в БД.
    tg_photo_updated_at = Column(DateTime, nullable=True)


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

    # ── Миграция: consumed_by_action в punishments (v4.5.1) ──────────
    # Какое авто-действие «погасило» варн: 'auto_mute' | 'auto_ban' | NULL.
    # См. комментарий в модели Punishment.
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(punishments)"))
        columns = [row[1] for row in result.fetchall()]
        if "consumed_by_action" not in columns:
            await conn.execute(text(
                "ALTER TABLE punishments ADD COLUMN consumed_by_action VARCHAR(20) NULL"
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

    # ── Миграция: tg_photo_updated_at в web_users (v4.5) ───────────────
    # timestamp последнего обновления аватарки. Сам файл хранится локально.
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(web_users)"))
        columns = [row[1] for row in result.fetchall()]
        if "tg_photo_updated_at" not in columns:
            await conn.execute(text(
                "ALTER TABLE web_users ADD COLUMN tg_photo_updated_at DATETIME NULL"
            ))

    # ── Миграция: расширение chat_settings (v4.4.7) ─────────────────────
    # title / is_enabled / is_private / is_report_chat — для авто-обнаружения
    # чатов и управления доступом. Все новые поля имеют дефолты, миграция
    # идемпотентна (IF NOT EXISTS через PRAGMA check).
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(chat_settings)"))
        columns = [row[1] for row in result.fetchall()]
        if "title" not in columns:
            await conn.execute(text(
                "ALTER TABLE chat_settings ADD COLUMN title VARCHAR(255) NULL"
            ))
        if "is_enabled" not in columns:
            await conn.execute(text(
                "ALTER TABLE chat_settings ADD COLUMN is_enabled BOOLEAN NOT NULL DEFAULT 1"
            ))
        if "is_private" not in columns:
            await conn.execute(text(
                "ALTER TABLE chat_settings ADD COLUMN is_private BOOLEAN NOT NULL DEFAULT 0"
            ))
        if "is_report_chat" not in columns:
            await conn.execute(text(
                "ALTER TABLE chat_settings ADD COLUMN is_report_chat BOOLEAN NOT NULL DEFAULT 0"
            ))

    # ── Миграция: расширение chat_settings (v4.5.2) ────────────────────
    # Новые поля для CAS, link filter, night mode, warn decay, auto-delete cmds.
    # Все имеют дефолты, миграция идемпотентна.
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(chat_settings)"))
        columns = [row[1] for row in result.fetchall()]
        v452_chat_settings_cols = [
            ("cas_check_enabled",          "BOOLEAN NOT NULL DEFAULT 0"),
            ("link_filter_enabled",        "BOOLEAN NOT NULL DEFAULT 0"),
            ("link_filter_action",         "VARCHAR(16) NOT NULL DEFAULT 'delete'"),
            ("auto_delete_commands",       "BOOLEAN NOT NULL DEFAULT 1"),
            ("warn_decay_days",            "INTEGER NOT NULL DEFAULT 0"),
            ("night_mode_enabled",         "BOOLEAN NOT NULL DEFAULT 0"),
            ("night_mode_start",           "VARCHAR(5) NOT NULL DEFAULT '23:00'"),
            ("night_mode_end",             "VARCHAR(5) NOT NULL DEFAULT '07:00'"),
            ("night_mode_permissions",     "TEXT NULL"),
            ("night_mode_saved_permissions", "TEXT NULL"),
            ("night_mode_currently_active", "BOOLEAN NOT NULL DEFAULT 0"),
        ]
        for col_name, col_type in v452_chat_settings_cols:
            if col_name not in columns:
                await conn.execute(text(
                    f"ALTER TABLE chat_settings ADD COLUMN {col_name} {col_type}"
                ))

    # ── Миграция: расширение chat_settings (v4.5.3) ────────────────────
    # Новые поля для расширенной настройки ночного режима: per-chat tz,
    # отдельное расписание на выходные, уведомления входа/выхода.
    # Идемпотентно (PRAGMA check + ALTER TABLE).
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(chat_settings)"))
        columns = [row[1] for row in result.fetchall()]
        v453_chat_settings_cols = [
            ("night_mode_tz",                 "VARCHAR(64) NOT NULL DEFAULT 'Europe/Moscow'"),
            ("night_mode_weekend_start",      "VARCHAR(5) NULL"),
            ("night_mode_weekend_end",        "VARCHAR(5) NULL"),
            ("night_mode_notify",             "BOOLEAN NOT NULL DEFAULT 0"),
            ("night_mode_notify_enter_msg",   "TEXT NULL"),
            ("night_mode_notify_exit_msg",    "TEXT NULL"),
        ]
        for col_name, col_type in v453_chat_settings_cols:
            if col_name not in columns:
                await conn.execute(text(
                    f"ALTER TABLE chat_settings ADD COLUMN {col_name} {col_type}"
                ))

    # ── Миграция: расширение chat_settings (v4.5.4) ────────────────────
    # Новые поля для санитарных дней: список дат + snapshot прав + флаг.
    # Идемпотентно (PRAGMA check + ALTER TABLE).
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(chat_settings)"))
        columns = [row[1] for row in result.fetchall()]
        v454_chat_settings_cols = [
            ("sanitary_days",                    "TEXT NULL"),
            ("sanitary_days_saved_permissions",  "TEXT NULL"),
            ("sanitary_days_currently_active",   "BOOLEAN NOT NULL DEFAULT 0"),
        ]
        for col_name, col_type in v454_chat_settings_cols:
            if col_name not in columns:
                await conn.execute(text(
                    f"ALTER TABLE chat_settings ADD COLUMN {col_name} {col_type}"
                ))

    # ── v4.5.2: новые таблицы (word_filters, link_allowlist, banned_sticker_packs)
    # create_all() выше уже создаст их для новой БД; для существующей БД
    # добавляем CREATE TABLE IF NOT EXISTS на случай если create_all пропустил.
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS word_filters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id BIGINT NOT NULL,
                pattern VARCHAR(255) NOT NULL,
                is_regex BOOLEAN NOT NULL DEFAULT 0,
                action VARCHAR(16) NOT NULL DEFAULT 'delete',
                mute_duration INTEGER NULL,
                created_by BIGINT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN NOT NULL DEFAULT 1
            )
        """))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_word_filters_chat_id ON word_filters (chat_id)"
        ))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS link_allowlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id BIGINT NOT NULL,
                domain VARCHAR(255) NOT NULL,
                created_by BIGINT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_link_allowlist_chat_id ON link_allowlist (chat_id)"
        ))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS banned_sticker_packs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id BIGINT NOT NULL,
                pack_name VARCHAR(255) NOT NULL,
                punishment VARCHAR(16) NOT NULL DEFAULT 'delete',
                mute_duration INTEGER NULL,
                reason TEXT NULL,
                added_by_mod_id BIGINT NULL,
                added_via VARCHAR(16) NOT NULL DEFAULT 'manual',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN NOT NULL DEFAULT 1
            )
        """))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_banned_sticker_packs_chat_id ON banned_sticker_packs (chat_id)"
        ))

    # ── v4.5.2: seed глобального allowlist для link filter ─────────────
    # При первом запуске (или если link_allowlist пуст) добавляем базовый
    # набор доверенных доменов: t.me, telegram.me, github.com.
    async with async_session() as session:
        existing = (await session.execute(
            select(LinkAllowlist).where(LinkAllowlist.chat_id == 0).limit(1)
        )).scalar_one_or_none()
        if existing is None:
            for domain in ("t.me", "telegram.me", "github.com", "youtu.be", "youtube.com"):
                session.add(LinkAllowlist(chat_id=0, domain=domain))
            await session.commit()
            logger_info = "v4.5.2 init_db: seeded global link allowlist (t.me, telegram.me, github.com, youtu.be, youtube.com)"
            # Логируем через print, т.к. logging может быть не настроен на момент init_db.
            print(logger_info)

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
