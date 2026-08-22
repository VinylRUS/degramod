"""
db.py — Асинхронный SQLAlchemy: модели, сессии.
База: /app/data/shadow_logs.db  |  WAL режим для конкурентного доступа.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import sys
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,  # v4.8.9: для явных Index() declarations в __table_args__
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
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

# v4.8.7: connect_args.timeout — таймаут на ждущий коннект (по умолчанию
# SQLite 5 сек, что мало при конкурентных writes в WAL-режиме). 30 сек
# покрывает даже тяжёлые VACUUM и крупные транзакции на проде.
# Дополнительно PRAGMA busy_timeout=30000 ставится в _set_sqlite_pragma
# ниже — для соединений, которые aiosqlite открывает уже внутри пула.
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"timeout": 30},
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ── Включаем WAL при подключении ────────────────────────────────────────────
@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    # v4.8.7: busy_timeout=30000 мс — сколько SQLite ждёт блокировку перед
    # выбросом SQLITE_BUSY. По умолчанию 5000 — мало при VACUUM или длинных
    # writes в WAL. 30 сек синхронизировано с connect_args.timeout выше.
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


# v4.8.9.1: logger для Alembic auto-stamp и других db-level операций.
# Использует тот же shadow_logger, что и bot.py / web_app.py — для консистентности.
logger = logging.getLogger("shadow_logger")


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
    # ── v4.7.16: Slow mode (chat-level, separate from ChatPermissions) ────
    # Telegram позволяет ставить slow_mode_delay (0-36400 сек) — минимальный
    # интервал между сообщениями одного юзера. Не входит в ChatPermissions,
    # это отдельное свойство чата (chat.slow_mode_delay).
    # night_mode_slow_mode_delay — slow_mode (сек) во время ночного режима.
    #   0 = не менять (поведение по умолчанию, backward compat).
    #   Типичное значение: 30 или 60 — ночью интервал больше.
    # day_slow_mode_delay — slow_mode (сек) в дневном режиме. Приоритет над
    #   snapshot'ом при восстановлении (preset-driven, как v4.7.12 для прав).
    #   0 = не менять (тогда восстанавливается snapshot если он есть).
    #   Типичное значение: 10.
    # night_mode_saved_slow_mode_delay — snapshot chat.slow_mode_delay на
    #   момент входа в ночной режим. Используется как fallback для
    #   восстановления при выходе, если day_slow_mode_delay=0.
    night_mode_slow_mode_delay = Column(Integer, default=0, nullable=False)
    day_slow_mode_delay = Column(Integer, default=0, nullable=False)
    night_mode_saved_slow_mode_delay = Column(Integer, nullable=True)

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
    # v4.6.0: формат изменён на monthly — JSON-объект вида
    #   {"2026-08": [["2026-08-02","2026-08-03"]],
    #    "2026-09": []}
    # При выходе из последнего санитарного дня месяца — ключ этого месяца
    # удаляется, ставится отметка last_sanitary_month="2026-08" чтобы
    # suppress dashboard warnings ("в этом месяце уже был санитарный день").
    # Старый формат (плоский массив пар) поддерживается — конвертируется
    # при первом чтении (parse_sanitary_days_json).
    sanitary_days = Column(Text, nullable=True)
    # JSON-снапшот прав чата ДО входа в санитарный день — восстанавливается
    # при выходе из него. Аналог night_mode_saved_permissions.
    sanitary_days_saved_permissions = Column(Text, nullable=True)
    # Флаг: сейчас активен санитарный день (для логирования и веб-панели).
    sanitary_days_currently_active = Column(Boolean, default=False, nullable=False)
    # ── v4.7.2: явный toggle для санитарных дней ───────────────────────
    # Раньше sanitary day включался автоматически если sanitary_days не пустой.
    # Теперь — явный toggle: если sanitary_days_enabled=False, _sanitary_day_tick
    # пропускает чат, даже если даты заданы. Настройки (даты, perms) сохраняются,
    # но не активны. Аналогично night_mode_enabled для ночного режима.
    sanitary_days_enabled = Column(Boolean, default=False, nullable=False)

    # ── v4.6.0: Granular permissions ───────────────────────────────────
    # day_permissions — права, применяемые в нормальном дневном состоянии.
    # Если NULL — бот берёт текущие права чата через snapshot (старое поведение,
    # обратная совместимость). Если задан (JSON) — используется явно.
    # Заполняется при выборе preset'а в веб-панели (PermissionPreset.scope='day').
    day_permissions = Column(Text, nullable=True)
    # sanitary_days_permissions — права на время санитарного дня. По умолчанию
    # NULL = all False (полный локдаун). Можно переопределить через preset
    # scope='sanitary' (например, оставить только текст).
    sanitary_days_permissions = Column(Text, nullable=True)
    # last_sanitary_month — месяц (YYYY-MM) в котором последний раз проводился
    # санитарный день. Защищает от ложных warnings "нет дат на след. месяц"
    # если санитарный день уже прошёл в текущем месяце. NULL = никогда.
    last_sanitary_month = Column(String(7), nullable=True)

    # ── v4.7.20: !alarm команда ──────────────────────────────────────────
    # Модераторская команда !alarm on/off — экстренное ограничение чата:
    # отключение медиа + slow_mode 30 сек. Аналог "панической кнопки" когда
    # в чате начинается флуд стикерами/гифками/медиа и нужно быстро всё
    # заглушить не прибегая к санитарному дню или ночному режиму.
    #
    # alarm_currently_active — True если !alarm сейчас активен. Используется
    #   _night_mode_tick для решения: снимать alarm перед входом в night.
    # alarm_saved_permissions — JSON-снапшот ChatPermissions чата ДО alarm.
    #   Восстанавливается при !alarm off. NULL если alarm не активен.
    # alarm_saved_slow_mode_delay — snapshot chat.slow_mode_delay ДО alarm.
    #   Восстанавливается при !alarm off (если day_slow_mode_delay=0).
    # alarm_active_until — datetime когда alarm должен автоматически сняться.
    #   NULL = до ручного !alarm off. _night_mode_tick проверяет каждую минуту.
    # alarm_started_by — user_id модератора, который включил alarm (для логов).
    alarm_currently_active = Column(Boolean, default=False, nullable=False)
    alarm_saved_permissions = Column(Text, nullable=True)
    alarm_saved_slow_mode_delay = Column(Integer, nullable=True)
    alarm_active_until = Column(DateTime, nullable=True)
    alarm_started_by = Column(BigInteger, nullable=True)

    # ── v4.7.24: Via-bot filter (rate-limit all «via @Bot» messages) ────
    # Когда юзер пишет в чат сообщение через стороннего бота (message.via_bot
    # is not None — например @HowYourBot, @vote, @like), бот применяет
    # rate-limit: разрешает не более 1 сообщения боту в N секунд на юзера.
    # Если юзер пытается чаще — сообщение удаляется, юзер мутичится на
    # M минут. Полный stealth — юзер не получает никакого уведомления.
    #
    # Дизайн (v4.7.24 — изменён по запросу пользователя):
    #   • Все боты в «чёрном списке» по умолчанию — фильтр работает на ВСЕХ
    #     via_bot сообщениях, явный список ботов не нужен.
    #   • Rate-limit grace: 1 сообщение в via_bot_filter_rate_limit_seconds
    #     (по умолчанию 300 = 5 минут) на (chat_id, user_id, bot_user_id).
    #     Внутри grace-окна — позволяем; за пределами — блокируем.
    #   • Действие при превышении: delete + mute на via_bot_mute_minutes
    #     (по умолчанию 10 минут). Без warn — сразу mute.
    #   • Управление: только через web-панель /admin/chats (toggle VIA-BOT
    #     в action buttons, настройки в разделе «Наказания»).
    #   • Stealth: молча удаляем. Юзер не понимает, кто удалил и почему.
    #   • Default: OFF (via_bot_filter_enabled=False). Per-chat.
    via_bot_filter_enabled = Column(Boolean, default=False, nullable=False)
    via_bot_rate_limit_seconds = Column(Integer, default=300, nullable=False)  # grace window (def 5 min)
    via_bot_mute_minutes = Column(Integer, default=10, nullable=False)         # mute duration (def 10 min)

    # ── v4.8.0: Modchat (модераторский чат) + keyword-watch ─────────────
    # Modchat — отдельный чат для оперативных оповещений модераторам:
    # события alarm on/off/auto-off/продление + keyword-watch (упоминания
    # заданных фраз). Взаимоисключается с is_report_chat (проверяется в коде).
    mod_chat_id = Column(BigInteger, nullable=True)
    is_mod_chat = Column(Boolean, default=False, nullable=False)

    # v5.1.0: ссылка на правила для команды /rules. Пусто → RULES_URL_DEFAULT.
    # Бот мультичатовый, и у чатов со временем заводятся свои правила —
    # колонка дешевле, чем миграция задним числом.
    rules_url = Column(String, nullable=True)


class PermissionPreset(Base):
    """v4.6.0: Глобальные пресеты прав для day / night / sanitary режимов.

    Хранит именованный набор ChatPermissions (13 bool полей) с указанием scope.
    Один пресет можно привязать к нескольким чатам — но в каждом чате сохраняется
    копия JSON (в ChatSettings.day_permissions / night_mode_permissions /
    sanitary_days_permissions), чтобы изменение/удаление пресета не ломало
    уже настроенные чаты.

    Системный пресет «Full lockdown» (scope='sanitary', id=1, все 13 полей False)
    создаётся автоматически при init_db и не может быть удалён.

    scope:
      • 'day'      — пресет для дневного режима (default chat state)
      • 'night'    — пресет для ночного режима
      • 'sanitary' — пресет для санитарных дней

    permissions — JSON вида {"can_send_messages": true, "can_send_audios": false, ...}
    со всеми 13 полями ChatPermissions.
    """
    __tablename__ = "permission_presets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(64), nullable=False, unique=True, index=True)
    scope = Column(String(16), nullable=False, index=True)  # 'day' | 'night' | 'sanitary'
    permissions = Column(Text, nullable=False)              # JSON of 13 ChatPermissions fields
    # v4.7.16: slow_mode_delay (chat-level, separate from ChatPermissions).
    # None = не менять slow_mode при применении пресета (backward compat).
    # 0 = выключить slow_mode (применить set_chat_slow_mode_delay(0)).
    # >0 = установить slow_mode в N секунд (Telegram limit: 0..36400).
    # Копируется в ChatSettings.day_slow_mode_delay / night_mode_slow_mode_delay
    # при выборе пресета — для independence (изменение/удаление пресета не ломает
    # уже настроенные чаты, как и для permissions).
    slow_mode_delay = Column(Integer, nullable=True)
    is_system = Column(Boolean, default=False, nullable=False)  # True = неудаляемый системный пресет
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class WordFilter(Base):
    """v4.5.2: Word filter (#7) — список запрещённых слов/паттернов для чата.

    Хранится отдельно от ChatSettings, т.к. паттернов может быть много.
    chat_id=0 — глобальные паттерны (применяются ко всем чатам, где word_filter
    включён — но мы не делаем per-chat toggle для word filter в этой версии;
    паттерны работают per-chat, для глобального default используется chat_id=0).

    v4.8.0: WordFilter был объявлен deprecated и частично заменён на KeywordWatch
    (см. ниже — для night-mode автобана). Однако WordFilter остаётся активным
    механизмом для базового word filter, управляемым через web UI /admin/presets
    (раздел «Запрещённые слова»). Bot-команды /addword /delword /listwords
    удалены в v4.8.6 (раньше отвечали заглушкой) — управление только через web.

    v4.8.6: модель и таблица остаются активными. Удаление не планируется.
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


class KeywordWatch(Base):
    """v4.8.0: Keyword-watch — замена word_filter.

    Принципиальные отличия от WordFilter:
      • Глобальный список (один на все чаты — правила везде одни).
        chat_id=0 всегда, поле оставлено для будущих расширений.
      • Каждая фраза имеет флаг ban_in_night_mode:
        - День: только notify в modchat (не банит, не удаляет).
        - Ночь (active night mode): если ban_in_night_mode=True — применяет
          автобан. Иначе — только notify.
      • Match logic:
        - Если фраза содержит пробел → substring match (case-insensitive).
          Фраза "срал в торт детишкам" найдёт только эту последовательность.
        - Если фраза — одно слово → word-boundary match (не сработает на
          "замодераторили" если в списке "модератор").
      • Exempt: модераторы/админы/SU пропускаются через _is_admin().
      • Антиспам modchat'а: rate-limit 60 сек/фраза + multiplexing
        (3 совпадения в одном сообщении → 1 notify).

    Поле rules_section (v4.8.0+): ID секции на сайте правил, куда публиковать
    фразу через GitHub sync (см. ROADMAP пункт #11). null = не публиковать.
    Пока в v4.8.0 не используется (GitHub sync планируется отдельно).
    """
    __tablename__ = "keyword_watch"
    # v4.8.9: явные Index() declarations — раньше индексы создавались через
    # raw SQL в init_db(), что путало Alembic autogenerate. Теперь они
    # описаны в модели → Alembic видит их как expected.
    __table_args__ = (
        Index("ix_keyword_watch_chat_id", "chat_id"),
        Index("ix_keyword_watch_is_active", "is_active"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # v4.8.9: index=True убран — индекс ix_keyword_watch_chat_id явно описан
    # в __table_args__ через Index(). Двойное объявление даёт конфликт.
    chat_id = Column(BigInteger, nullable=False, default=0)  # 0 = global (всегда)
    phrase = Column(String(255), nullable=False)              # отслеживаемая фраза
    ban_in_night_mode = Column(Boolean, default=False, nullable=False)  # авто-бан ночью
    rules_section = Column(String(64), nullable=True)         # ID секции сайта правил (для #11)
    created_by = Column(BigInteger, nullable=True)            # mod_id создателя
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    is_active = Column(Boolean, default=True, nullable=False)


class BotWhitelist(Base):
    """v5.1.0: боты, на которых не действует via-bot кулдаун и автомьют.

    Калька с LinkAllowlist: chat_id=0 означает «во всех чатах», конкретный
    chat_id — только в этом чате.

    bot_id заполняется оппортунистически, когда бот впервые встречается в
    message.via_bot: username сменить можно, числовой id — нет. Матч идёт
    по username ИЛИ по известному bot_id.
    """

    __tablename__ = "bot_whitelist"

    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, nullable=False, default=0)  # 0 = global
    bot_username = Column(String, nullable=False)            # lower, без «@»
    bot_id = Column(BigInteger, nullable=True)
    note = Column(String, nullable=True)
    added_by_mod_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("chat_id", "bot_username", name="uq_bot_whitelist_chat_bot"),
    )


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


class AutomuteCounter(Base):
    """v4.8.4: Счётчик автомьютов для прогрессивных мутов.

    Хранит количество раз, которое бот автоматически замьютил юзера
    в конкретном чате. Используется для прогрессивной формулы:

        mute_duration = base_duration + (count * 60 секунд)

    где ``count`` — значение счётчика ДО текущего мьюта (0 при первом
    автомьюте, 1 при втором, и т.д.). После применения мута счётчик
    инкрементируется.

    Ключевые свойства:
      • **Не сбрасывается** при ``!resetwarns`` — варны и муты считаются
        независимо. Очистка варнов не обнуляет историю автомьютов.
      • **Не сбрасывается** при ``!unmute`` — снятие мьюта не обнуляет
        счётчик.
      • **Растёт бесконечно** — нет кэпа, нет автобана после N мутов.
      • **Per-chat**: муты в чате A не влияют на длительность в чате B.
      • **Только автомьюты**: ручные мьюты (``!mute``, ``!smute``) НЕ
        инкрементируют счётчик и не используют прогрессивную формулу.
      • Сбрасывается только через ``!resetmc`` (SU/Admin) или веб-панель.

    PK: ``(chat_id, user_id)`` — одна запись на юзера на чат.
    """
    __tablename__ = "automute_counters"

    chat_id = Column(BigInteger, primary_key=True)
    user_id = Column(BigInteger, primary_key=True)
    count = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


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
    # ── v4.7.0: авто-обнаружение TG-админов ────────────────────────────
    # is_pending=True — WebUser создан sync-кнопкой, ждёт /start от юзера
    # (is_active=False, без пароля). На /start → генерим пароль, is_active=True,
    # is_pending=False, шлём DM с credentials.
    is_pending = Column(Boolean, default=False, nullable=False)
    # auto_discovered=True — маркер что учётка создана автоматически через sync,
    # а не вручную SU. Полезно для статистики и для отличия исторических ручных
    # учёток (v4.6.x и ранее) от новых авто.
    auto_discovered = Column(Boolean, default=False, nullable=False)


# ── v4.8.5: Шифрование PAT для GitHub интеграции ──────────────────────────
# Используем Fernet (symmetric AES-128-CBC + HMAC-SHA256) из пакета cryptography.
# Ключ шифрования берётся из env GITHUB_IDEA_ENC_KEY (32 urlsafe-base64 байта).
# Если env не задан — генерируется одноразово при первом запуске и сохраняется
# в файл <data_dir>/.github_enc_key (авто-создаётся, права 0600).
# При потере ключа — PAT невозможно расшифровать, придется перезавести.

_ENC_KEY_ENV = "GITHUB_IDEA_ENC_KEY"
_ENC_KEY_FILE = os.path.join(
    os.path.dirname(os.getenv("DB_PATH", "/app/data/shadow_logs.db")),
    ".github_enc_key",
)


def _load_or_create_enc_key() -> bytes:
    """Загружает Fernet-ключ из env или файла. Если нет — генерирует и
    сохраняет в файл (с правами 0600).

    Returns:
        32 urlsafe-base64 байта (как требует Fernet).
    Raises:
        RuntimeError: если не удалось создать файл ключа.
    """
    # 1. Env приоритетнее.
    env_key = os.getenv(_ENC_KEY_ENV)
    if env_key:
        return env_key.strip().encode("utf-8")

    # 2. Файл.
    try:
        with open(_ENC_KEY_FILE, "r", encoding="utf-8") as f:
            return f.read().strip().encode("utf-8")
    except FileNotFoundError:
        pass
    except OSError:
        # Файл есть, но не читается — падаем. Это лучше чем молча перегенерить
        # ключ и потерять доступ к уже зашифрованным PAT'ам.
        raise RuntimeError(
            f"Cannot read GitHub PAT encryption key at {_ENC_KEY_FILE}: "
            "permission denied. Fix file permissions or set "
            f"{_ENC_KEY_ENV} env variable."
        )

    # 3. Генерируем новый ключ.
    try:
        from cryptography.fernet import Fernet
    except ImportError as e:
        raise RuntimeError(
            "Package 'cryptography' is required for GitHub PAT encryption. "
            "Install it: pip install cryptography"
        ) from e

    new_key = Fernet.generate_key()
    try:
        # Записываем с правами 0600 — только владелец может читать.
        fd = os.open(_ENC_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, new_key)
        finally:
            os.close(fd)
    except OSError as e:
        raise RuntimeError(
            f"Cannot create GitHub PAT encryption key file at {_ENC_KEY_FILE}: {e}. "
            f"Set {_ENC_KEY_ENV} env variable as alternative."
        ) from e

    return new_key


def _encrypt_pat(pat: str) -> str:
    """Шифрует GitHub PAT через Fernet. Возвращает urlsafe-base64 строку.

    Args:
        pat: GitHub Personal Access Token (plaintext).

    Returns:
        Fernet-encrypted token (str), сохраняемый в БД.

    Raises:
        RuntimeError: если cryptography не установлен или ключ недоступен.
    """
    try:
        from cryptography.fernet import Fernet
    except ImportError as e:
        raise RuntimeError(
            "Package 'cryptography' is required for GitHub PAT encryption"
        ) from e
    key = _load_or_create_enc_key()
    return Fernet(key).encrypt(pat.encode("utf-8")).decode("utf-8")


def _decrypt_pat(encrypted: str) -> str:
    """Расшифровывает GitHub PAT.

    Args:
        encrypted: Fernet-encrypted token (str) из БД.

    Returns:
        Plaintext PAT.

    Raises:
        RuntimeError: если cryptography не установлен.
        cryptography.fernet.InvalidToken: если ключ не подходит или данные
            повреждены.
    """
    try:
        from cryptography.fernet import Fernet
    except ImportError as e:
        raise RuntimeError(
            "Package 'cryptography' is required for GitHub PAT encryption"
        ) from e
    key = _load_or_create_enc_key()
    return Fernet(key).decrypt(encrypted.encode("utf-8")).decode("utf-8")


# ── v4.8.5: Лог идей (`!idea` → GitHub Issues) ────────────────────────────
class IdeaLog(Base):
    """v4.8.5: Лог идей, отправленных модераторами/админами через `!idea`.

    Каждая запись — один успешный (или неуспешный) вызов `!idea` в ЛС боту
    или в модераторском чате (modchat). Метаданные НЕ попадают в GitHub
    Issue (Issue остаётся чистым, только заголовок = текст идеи) —
    метаданные живут здесь, в БД бота.

    Назначение:
      • История поданных идей (когда, кто, откуда).
      • Источник имени отправившего для алерта SU в DM.
      • Аудит при падениях GitHub API (если Issue создать не удалось —
        в ``github_issue_url`` остаётся NULL, но текст идеи не потерян).

    PK: auto-increment id. По ``(tg_user_id, created_at)`` можно
    фильтровать историю конкретного юзера.
    """
    __tablename__ = "idea_log"
    # v4.8.9: явные Index() declarations (раньше raw SQL в init_db()).
    __table_args__ = (
        Index("ix_idea_log_tg_user_id", "tg_user_id"),
        Index("ix_idea_log_created_at", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # v4.8.9: index=True убран — индекс ix_idea_log_tg_user_id явно описан
    # в __table_args__ через Index(). Двойное объявление даёт конфликт.
    tg_user_id = Column(BigInteger, nullable=False)
    tg_username = Column(String(255), nullable=True)         # @username (без @, lowercase) или None
    tg_display_name = Column(String(255), nullable=True)     # first_name + last_name для алерта SU
    source = Column(String(16), nullable=False)              # "dm" | "modchat"
    source_chat_id = Column(BigInteger, nullable=True)       # ID чата (для modchat — id modchat, для dm — user_id)
    idea_text = Column(String(200), nullable=False)          # текст идеи (до 200 символов)
    github_issue_url = Column(String(512), nullable=True)    # NULL если создать не удалось
    github_issue_number = Column(Integer, nullable=True)     # NULL если создать не удалось
    github_project_item_id = Column(String(64), nullable=True)  # GraphQL node ID в Project v2
    error_message = Column(Text, nullable=True)              # NULL при успехе, текст ошибки при провале
    bot_version = Column(String(32), nullable=False)         # например, "v4.8.5"
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)


# ── v4.8.5: Настройки GitHub Projects интеграции ──────────────────────────
class GithubSettings(Base):
    """v4.8.5: Настройки подключения к GitHub для `!idea` → Issues.

    Хранит PAT (зашифрованный через Fernet), реквизиты репозитория и
    GitHub Project v2. Заполняется через веб-панель (Settings → GitHub
    Projects). Одна строка в таблице (singleton, id=1) — настройки
    глобальные на весь бот.

    Поля:
      • pat_encrypted — Fernet-шифр PAT. Расшифровка через _decrypt_pat().
      • repo_owner, repo_name — куда создавать Issues (например,
        ``degradach``/``ded-vobzhak-ideas``).
      • project_node_id — GraphQL node ID Project v2 (например,
        ``PVT_xxx``). Получается через GraphQL-запрос или из URL Project.
      • project_number — number Project в организации/юзере (для отображения
        в веб-панели и для тестов). Необязательно.
      • project_owner_login — login владельца Project (для GraphQL query).
      • project_status_option_name — имя single-select option в поле Status
        Project v2, в которое автоматически попадают новые Issue (default
        'Предложено'). v4.8.5.3.
      • is_active — флаг включения интеграции. Если False — `!idea` не
        пытается отправить в GitHub, отправителю возвращается «не активна».
      • updated_at, updated_by — audit.
    """
    __tablename__ = "github_settings"

    id = Column(Integer, primary_key=True)                  # всегда 1 (singleton)
    pat_encrypted = Column(Text, nullable=True)             # Fernet-шифр PAT
    repo_owner = Column(String(128), nullable=True)
    repo_name = Column(String(128), nullable=True)
    project_node_id = Column(String(64), nullable=True)     # GraphQL node ID (PVT_...)
    project_number = Column(Integer, nullable=True)
    project_owner_login = Column(String(128), nullable=True)
    # v4.8.5.3: имя Status-опции для авто-присвоения ('Предложено' по умолчанию).
    # lookup происходит при каждой !idea — find_status_field + set_item_status.
    project_status_option_name = Column(String(128), nullable=True)
    is_active = Column(Boolean, default=False, nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
    updated_by = Column(String(64), nullable=True)          # username из веб-панели


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

    # ── Миграция: is_pending / auto_discovered в web_users (v4.7.0) ────
    # is_pending=True — авто-созданная учётка, ждёт /start (is_active=False, без пароля).
    # auto_discovered=True — маркер что создана через sync, а не вручную SU.
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(web_users)"))
        columns = [row[1] for row in result.fetchall()]
        if "is_pending" not in columns:
            await conn.execute(text(
                "ALTER TABLE web_users ADD COLUMN is_pending BOOLEAN NOT NULL DEFAULT 0"
            ))
        if "auto_discovered" not in columns:
            await conn.execute(text(
                "ALTER TABLE web_users ADD COLUMN auto_discovered BOOLEAN NOT NULL DEFAULT 0"
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

    # ── Миграция: расширение chat_settings (v4.6.0) ────────────────────
    # Новые поля: day_permissions, sanitary_days_permissions, last_sanitary_month.
    # Идемпотентно (PRAGMA check + ALTER TABLE).
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(chat_settings)"))
        columns = [row[1] for row in result.fetchall()]
        v460_chat_settings_cols = [
            ("day_permissions",            "TEXT NULL"),
            ("sanitary_days_permissions",  "TEXT NULL"),
            ("last_sanitary_month",        "VARCHAR(7) NULL"),
        ]
        for col_name, col_type in v460_chat_settings_cols:
            if col_name not in columns:
                await conn.execute(text(
                    f"ALTER TABLE chat_settings ADD COLUMN {col_name} {col_type}"
                ))

    # ── Миграция: sanitary_days_enabled + one-time сброс toggles (v4.7.2 / v4.7.23)
    # v4.7.2: явные toggle для night mode и sanitary days.
    #   - Добавляем колонку sanitary_days_enabled (Boolean, default 0).
    #   - ONE-TIME сброс night_mode_enabled=0 для всех чатов (пользователь решил
    #     что при обновлении все функции должны быть выключены — нужно явно
    #     включать через /admin/chats после обновления).
    #
    # v4.7.23 HOTFIX: предыдущая реализация (v4.7.2) использовала
    # "SELECT COUNT(*) WHERE night_mode_enabled=1 OR sanitary_days_currently_active=1"
    # как маркер "первого запуска после апгрейда до v4.7.2". Но этот маркер
    # некорректен: как только любой чат получает night_mode_enabled=1 (пользователь
    # нормально включил режим через web-панель или !nightmode on), КАЖДЫЙ рестарт
    # триггерил UPDATE ... SET night_mode_enabled=0 → toggle пользователя сбрасывался.
    # Пользователь сообщил: "при каждой перезагрузке скидывается переключатель
    # автопереключения ночного/дневного режима". Фикс: reset перемещён внутрь
    # блока `if "sanitary_days_enabled" not in columns:` — настоящий маркер
    # "первой миграции" это "колонка ещё не добавлена", а не "есть чат с
    # включённым night_mode".
    #
    # night_mode_currently_active / sanitary_days_currently_active — это
    # RUNTIME state, не user toggle. Если бот был оффлайн во время
    # scheduled exit window, эти флаги могут остаться stale=1. Сбрасываем
    # их при каждом запуске — _night_mode_tick / _sanitary_days_tick
    # переопределят их в течение 60 сек согласно расписанию.
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(chat_settings)"))
        columns = [row[1] for row in result.fetchall()]
        if "sanitary_days_enabled" not in columns:
            # ── Первый запуск после апгрейда до v4.7.2 ──
            # Добавляем колонку И делаем one-time reset всех toggle/state.
            await conn.execute(text(
                "ALTER TABLE chat_settings ADD COLUMN sanitary_days_enabled "
                "BOOLEAN NOT NULL DEFAULT 0"
            ))
            # v4.7.2 one-time reset: пользователь явно запросил, чтобы при
            # обновлении все функции были выключены — нужно явно включать
            # через /admin/chats после обновления.
            await conn.execute(text(
                "UPDATE chat_settings SET night_mode_enabled=0, "
                "night_mode_currently_active=0, sanitary_days_currently_active=0 "
                "WHERE chat_id != 0"
            ))
        else:
            # ── Обычный рестарт (миграция v4.7.2 уже применена ранее) ──
            # НЕ трогаем night_mode_enabled / sanitary_days_enabled — это
            # user toggles, они должны сохраняться между рестартами.
            # Только сбрасываем runtime state (currently_active) на случай
            # stale-флага после краша во время активного режима.
            await conn.execute(text(
                "UPDATE chat_settings SET "
                "night_mode_currently_active=0, sanitary_days_currently_active=0 "
                "WHERE chat_id != 0"
            ))

    # ── Миграция: v4.7.16 slow_mode columns (chat-level, separate from
    # ChatPermissions). Идемпотентно (PRAGMA check + ALTER TABLE).
    # НЕ сбрасываем существующие значения slow_mode у чатов — это новая
    # фича, по умолчанию выключена (0 = не менять).
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(chat_settings)"))
        columns = [row[1] for row in result.fetchall()]
        v4716_chat_settings_cols = [
            ("night_mode_slow_mode_delay",     "INTEGER NOT NULL DEFAULT 0"),
            ("day_slow_mode_delay",            "INTEGER NOT NULL DEFAULT 0"),
            ("night_mode_saved_slow_mode_delay", "INTEGER NULL"),
        ]
        for col_name, col_type in v4716_chat_settings_cols:
            if col_name not in columns:
                await conn.execute(text(
                    f"ALTER TABLE chat_settings ADD COLUMN {col_name} {col_type}"
                ))

    # ── Миграция: новая таблица permission_presets (v4.6.0) ───────────
    # create_all() выше создаст её для новой БД; для существующей — IF NOT EXISTS.
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS permission_presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(64) NOT NULL UNIQUE,
                scope VARCHAR(16) NOT NULL,
                permissions TEXT NOT NULL,
                is_system BOOLEAN NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_permission_presets_scope ON permission_presets (scope)"
        ))

    # ── Миграция: v4.7.16 slow_mode_delay в permission_presets ────────
    # Идемпотентно (PRAGMA check + ALTER TABLE). None = не менять slow_mode,
    # 0 = выкл, >0 = установить N сек. См. PermissionPreset.slow_mode_delay.
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(permission_presets)"))
        columns = [row[1] for row in result.fetchall()]
        if "slow_mode_delay" not in columns:
            await conn.execute(text(
                "ALTER TABLE permission_presets ADD COLUMN slow_mode_delay INTEGER NULL"
            ))

    # ── Миграция: v4.7.20 !alarm columns ──────────────────────────────
    # Идемпотентно (PRAGMA check + ALTER TABLE). alarm_currently_active
    # по умолчанию 0 (False) — alarm выключен. Остальные колонки nullable.
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(chat_settings)"))
        columns = [row[1] for row in result.fetchall()]
        v4720_alarm_cols = [
            ("alarm_currently_active",      "BOOLEAN NOT NULL DEFAULT 0"),
            ("alarm_saved_permissions",     "TEXT NULL"),
            ("alarm_saved_slow_mode_delay", "INTEGER NULL"),
            ("alarm_active_until",          "DATETIME NULL"),
            ("alarm_started_by",            "BIGINT NULL"),
        ]
        for col_name, col_type in v4720_alarm_cols:
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

    # ── v4.7.24: Via-bot filter (изменённый дизайн) ─────────────────────
    # Добавляем 3 новые колонки в chat_settings для rate-limit-фильтра
    # «via @Bot» сообщений. Идемпотентная миграция — проверяем существование
    # каждой колонки через PRAGMA table_info перед ALTER TABLE.
    # Если колонка уже есть — пропускаем (можно запускать при каждом старте).
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(chat_settings)"))
        existing_cols = {row[1] for row in result.fetchall()}
        if "via_bot_filter_enabled" not in existing_cols:
            await conn.execute(text(
                "ALTER TABLE chat_settings ADD COLUMN via_bot_filter_enabled "
                "BOOLEAN NOT NULL DEFAULT 0"
            ))
        if "via_bot_rate_limit_seconds" not in existing_cols:
            await conn.execute(text(
                "ALTER TABLE chat_settings ADD COLUMN via_bot_rate_limit_seconds "
                "INTEGER NOT NULL DEFAULT 300"
            ))
        if "via_bot_mute_minutes" not in existing_cols:
            await conn.execute(text(
                "ALTER TABLE chat_settings ADD COLUMN via_bot_mute_minutes "
                "INTEGER NOT NULL DEFAULT 10"
            ))

    # ── v4.8.0: Modchat (модераторский чат) + взаимоисключение с report_chat ──
    # Modchat — отдельный чат для оперативных оповещений модераторам:
    # события alarm on/off/auto-off/продление + keyword-watch (упоминания
    # заданных фраз). Без медиа-превью, краткий текстовый формат.
    # Взаимоисключение: один и тот же чат не может быть одновременно
    # репорт-чатом и modchat'ом. Это проверяется в коде (web_app.py +
    # !setmodchat команда), не на уровне БД.
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(chat_settings)"))
        existing_cols = {row[1] for row in result.fetchall()}
        v480_modchat_cols = [
            ("mod_chat_id",  "BIGINT NULL"),   # ID чата, назначенного как modchat
            ("is_mod_chat",  "BOOLEAN NOT NULL DEFAULT 0"),  # флаг «этот чат — modchat»
        ]
        for col_name, col_type in v480_modchat_cols:
            if col_name not in existing_cols:
                await conn.execute(text(
                    f"ALTER TABLE chat_settings ADD COLUMN {col_name} {col_type}"
                ))

    # ── v5.1.0: rules_url для команды /rules ─────────────────────────────
    # До ЛЮБОГО ORM-запроса к chat_settings — иначе ORM подставляет в SELECT
    # все колонки модели и падает на старой БД (так уже ломался старт бота
    # в v4.8.5.4).
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(chat_settings)"))
        existing_cols = {row[1] for row in result.fetchall()}
        if "rules_url" not in existing_cols:
            await conn.exec_driver_sql(
                "ALTER TABLE chat_settings ADD COLUMN rules_url VARCHAR"
            )

    # ── v5.1.0: Новая таблица bot_whitelist (обход via-bot фильтра) ─────
    # create_all() создаст её для новой БД; для существующей — CREATE IF NOT EXISTS.
    # До ЛЮБОГО ORM-запроса к bot_whitelist — иначе ORM подставляет в SELECT
    # все колонки модели и падает на старой БД (см. v4.8.5.4).
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS bot_whitelist ("
            "id INTEGER PRIMARY KEY, "
            "chat_id BIGINT NOT NULL DEFAULT 0, "
            "bot_username VARCHAR NOT NULL, "
            "bot_id BIGINT, "
            "note VARCHAR, "
            "added_by_mod_id BIGINT, "
            "created_at DATETIME, "
            "UNIQUE (chat_id, bot_username))"
        )

    # ── v4.8.0: Новая таблица keyword_watch ────────────────────────────
    # Замена word_filters. См. модель KeywordWatch выше для деталей.
    # create_all() создаст её для новой БД; для существующей — CREATE IF NOT EXISTS.
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS keyword_watch (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id BIGINT NOT NULL DEFAULT 0,
                phrase VARCHAR(255) NOT NULL,
                ban_in_night_mode BOOLEAN NOT NULL DEFAULT 0,
                rules_section VARCHAR(64) NULL,
                created_by BIGINT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN NOT NULL DEFAULT 1
            )
        """))
        # v4.8.9: индексы ix_keyword_watch_* создаются через Index() в
        # __table_args__ модели KeywordWatch (Base.metadata.create_all).
        # Раньше тут был raw SQL CREATE INDEX IF NOT EXISTS, но он конфликтовал
        # с явными Index() declarations. Для существующих БД индексы уже стоят
        # (созданы предыдущими версиями init_db). Для новых БД — создастся
        # автоматически через create_all.

    # ── v4.8.4: Новая таблица automute_counters (прогрессивные автомьюты) ──
    # Хранит per-chat per-user счётчик автомьютов. Используется для формулы:
    #   mute_duration = base_duration + (count * 60 сек)
    # create_all() создаст её для новой БД; для существующей — CREATE IF NOT EXISTS.
    # PK (chat_id, user_id) — одна запись на юзера на чат.
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS automute_counters (
                chat_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, user_id)
            )
        """))

    # ── v4.8.5: Новая таблица idea_log (лог идей → GitHub Issues) ──────────
    # create_all() создаст её для новой БД; для существующей — CREATE IF NOT EXISTS.
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS idea_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_user_id BIGINT NOT NULL,
                tg_username VARCHAR(255) NULL,
                tg_display_name VARCHAR(255) NULL,
                source VARCHAR(16) NOT NULL,
                source_chat_id BIGINT NULL,
                idea_text VARCHAR(200) NOT NULL,
                github_issue_url VARCHAR(512) NULL,
                github_issue_number INTEGER NULL,
                github_project_item_id VARCHAR(64) NULL,
                error_message TEXT NULL,
                bot_version VARCHAR(32) NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        # v4.8.9: индексы ix_idea_log_* создаются через Index() в
        # __table_args__ модели IdeaLog (Base.metadata.create_all).

    # ── v4.8.5: Новая таблица github_settings (singleton для PAT и репо) ───
    # create_all() создаст её для новой БД; для существующей — CREATE IF NOT EXISTS.
    # ВАЖНО: здесь указываем ВСЕ колонки, что в модели GithubSettings — иначе при
    # ручном создании БД (без Base.metadata.create_all) у нас будет таблица без
    # новой колонки project_status_option_name, и последующий ORM-SELECT упадёт.
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS github_settings (
                id INTEGER PRIMARY KEY,
                pat_encrypted TEXT NULL,
                repo_owner VARCHAR(128) NULL,
                repo_name VARCHAR(128) NULL,
                project_node_id VARCHAR(64) NULL,
                project_number INTEGER NULL,
                project_owner_login VARCHAR(128) NULL,
                project_status_option_name VARCHAR(128) NULL,
                is_active BOOLEAN NOT NULL DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_by VARCHAR(64) NULL
            )
        """))

    # ── v4.8.5.3: миграция — добавляем project_status_option_name в github_settings ──
    # Идемпотентно (PRAGMA check + ALTER TABLE). Default 'Предложено' —
    # стандартная колонка, которую мы договаривались использовать.
    # ВАЖНО: этот блок идёт ДО ORM-SELECT'а singleton-строки ниже — иначе на
    # существующей БД без этой колонки SELECT падает с
    # "no such column: github_settings.project_status_option_name".
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(github_settings)"))
        columns = [row[1] for row in result.fetchall()]
        if "project_status_option_name" not in columns:
            await conn.execute(text(
                "ALTER TABLE github_settings ADD COLUMN "
                "project_status_option_name VARCHAR(128) NULL"
            ))
            # Заполняем default для существующей singleton-строки.
            await conn.execute(text(
                "UPDATE github_settings SET project_status_option_name = 'Предложено' "
                "WHERE project_status_option_name IS NULL"
            ))

    # ── v4.8.5: Seed singleton-строки github_settings (id=1) ───────────────
    # Гарантируем что строка с id=1 существует — на ней будут UPDATE'ы из
    # веб-панели. Без этого INSERT через веб-форму упадёт на PK-конфликте
    # при повторном сохранении.
    async with async_session() as session:
        existing_gs = (await session.execute(
            select(GithubSettings).where(GithubSettings.id == 1)
        )).scalar_one_or_none()
        if existing_gs is None:
            session.add(GithubSettings(
                id=1, is_active=False,
                project_status_option_name="Предложено",  # v4.8.5.3 default
            ))
            await session.commit()

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

    # ── v4.6.0: Seed системных permission presets ──────────────────────
    # Создаём 3 неудаляемых системных пресета — по одному на каждый scope.
    # Если они уже есть (idempotent) — пропускаем.
    import json as _json
    _ALL_TRUE = {k: True for k in (
        "can_send_messages", "can_send_audios", "can_send_documents",
        "can_send_photos", "can_send_videos", "can_send_video_notes",
        "can_send_voice_notes", "can_send_polls", "can_send_other_messages",
        "can_add_web_page_previews", "can_change_info", "can_invite_users",
        "can_pin_messages",
    )}
    _ALL_FALSE = {k: False for k in _ALL_TRUE}
    _TEXT_ONLY = {**_ALL_FALSE, "can_send_messages": True}
    _DAY_DEFAULT = {
        # Per user spec for "Day default":
        # Allowed: text, audios, photos, videos, stickers/GIFs (other_messages)
        # Blocked: documents, vnotes, voices, polls, link_previews,
        #          change_info, invite_users, pin_messages
        "can_send_messages": True,
        "can_send_audios": True,
        "can_send_photos": True,
        "can_send_videos": True,
        "can_send_other_messages": True,  # stickers, GIFs, dice
        "can_send_documents": False,
        "can_send_video_notes": False,
        "can_send_voice_notes": False,
        "can_send_polls": False,
        "can_add_web_page_previews": False,
        "can_change_info": False,
        "can_invite_users": False,
        "can_pin_messages": False,
    }
    _SYSTEM_PRESETS = [
        ("Full lockdown", "sanitary", _ALL_FALSE),
        ("Text only",     "night",    _TEXT_ONLY),
        ("Day default",   "day",      _DAY_DEFAULT),
    ]
    async with async_session() as session:
        for name, scope, perms in _SYSTEM_PRESETS:
            existing_p = (await session.execute(
                select(PermissionPreset).where(PermissionPreset.name == name)
            )).scalar_one_or_none()
            if existing_p is None:
                session.add(PermissionPreset(
                    name=name, scope=scope,
                    permissions=_json.dumps(perms),
                    is_system=True,
                ))
            else:
                # Гарантируем что is_system=True (на случай если пресет был создан
                # вручную до того как стал системным — unlikely но безопасно).
                if not existing_p.is_system:
                    existing_p.is_system = True
        await session.commit()


async def get_session() -> AsyncSession:
    """Фабрика сессий — использовать через async with."""
    async with async_session() as session:
        yield session


# ── v4.8.9: Alembic миграции ────────────────────────────────────────────────
# Заменяет 664-строчный идемпотентный init_db() на явные миграции через Alembic.
# Escape hatch: env DB_USE_LEGACY_MIGRATIONS=1 — fallback на init_db() если
# Alembic что-то сломает (см. 03_TASK_v4.8.9.md §4, грабли №3).

async def run_migrations_async() -> None:
    """Запускает `alembic upgrade head` через in-process API (без subprocess).

    v4.8.9: вызывается из bot.py lifespan startup ВМЕСТО init_db().
    v4.8.9.1: добавлен auto-stamp для существующих БД без alembic_version.
    v4.8.9.2: усилена диагностика — print() в stderr (точно видно в Bothost
              даже если logger фильтруется).
    """
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    alembic_ini = Path(__file__).resolve().parent / "alembic.ini"
    if not alembic_ini.exists():
        raise RuntimeError(
            f"alembic.ini not found at {alembic_ini}. "
            "v4.8.9 requires alembic.ini + migrations/ in the project root."
        )

    config = Config(str(alembic_ini))

    db_path_env = os.getenv("DB_PATH", "/app/data/shadow_logs.db")
    print(f"[v4.8.9.2] run_migrations_async: starting, DB_PATH={db_path_env}", flush=True)

    # v4.8.9.1: auto-stamp для существующих БД без alembic_version.
    _auto_stamp_if_needed(config, alembic_ini)

    print("[v4.8.9.2] run_migrations_async: calling alembic upgrade head", flush=True)
    command.upgrade(config, "head")
    print("[v4.8.9.2] run_migrations_async: alembic upgrade head done", flush=True)


def _known_revisions(alembic_ini) -> set[str]:
    """Идентификаторы ревизий, лежащих в migrations/versions.

    Нужны, чтобы отличить «в базе стоит наша ревизия» от «в базе стоит
    ревизия, которой в этом коде нет» — второе бывает при откате версии
    назад и без обработки роняет старт с CommandError.

    При любой ошибке чтения возвращает пустое множество: вызывающий
    трактует это как «проверить не удалось» и ревизию не трогает.
    """
    from pathlib import Path as _Path
    try:
        versions_dir = _Path(alembic_ini).resolve().parent / "migrations" / "versions"
        found = set()
        for f in versions_dir.glob("*.py"):
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.startswith("revision:") or line.startswith("revision ="):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        found.add(parts[1].strip().strip("'\""))
                    break
        return found
    except OSError:
        return set()


def _auto_stamp_if_needed(config, alembic_ini) -> None:
    """v4.8.9.1: если БД существует с таблицами, но без alembic_version —
    автоматически stamp head.

    v4.8.9.2: усилена диагностика через print() в stderr — Bothost logs
    показывают stdout/stderr без фильтрации по уровню logging.
    """
    import sqlite3
    import sys

    from alembic import command

    db_path = os.getenv("DB_PATH", "/app/data/shadow_logs.db")
    print(f"[v4.8.9.2] auto-stamp: checking DB at {db_path}", flush=True)

    if not os.path.exists(db_path):
        print("[v4.8.9.2] auto-stamp: DB file does not exist — empty DB, will create schema from scratch", flush=True)
        return

    file_size = os.path.getsize(db_path)
    print(f"[v4.8.9.2] auto-stamp: DB file exists, size={file_size} bytes", flush=True)

    try:
        conn = sqlite3.connect(db_path)
        try:
            # Все таблицы
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            all_tables = [row[0] for row in cursor.fetchall()]
            print(f"[v4.8.9.2] auto-stamp: tables found: {all_tables}", flush=True)

            if not all_tables:
                print("[v4.8.9.2] auto-stamp: no tables — empty DB, will create schema", flush=True)
                return

            has_alembic = "alembic_version" in all_tables
            if has_alembic:
                # v4.11.0 (Task 12): проверяем НАЛИЧИЕ ЗАПИСИ, а не только
                # таблицы. Раньше здесь стоял безусловный return, и это
                # дважды роняло прод.
                #
                # Alembic создаёт alembic_version ДО того, как запишет в неё
                # ревизию. Если первый upgrade упал (а он падал — на уже
                # существующих таблицах legacy-БД), таблица остаётся пустой.
                # Старый код видел её, считал базу размеченной и выходил;
                # Alembic дальше читал ревизию, получал пусто, решал, что не
                # применено ничего, и запускал baseline с нуля — CREATE TABLE
                # на существующей таблице. Бот не стартовал.
                #
                # Диагноз подтверждён 20.08.2026 на копии боевой БД: таблица
                # есть, строк ноль.
                try:
                    version_row = conn.execute(
                        "SELECT version_num FROM alembic_version"
                    ).fetchone()
                except sqlite3.Error as e:
                    print(f"[v4.11.0] auto-stamp: cannot read alembic_version: {e} — stamping head", flush=True)
                    version_row = None

                if version_row and version_row[0]:
                    current = version_row[0]
                    known = _known_revisions(alembic_ini)
                    if not known or current in known:
                        print(f"[v4.11.0] auto-stamp: alembic_version={current} — ok, nothing to do", flush=True)
                        return
                    # Ревизия есть, но репозиторий её не знает: код откатили
                    # на версию без этой миграции. upgrade кинул бы
                    # CommandError и не дал боту стартовать.
                    print(f"[v4.11.0] auto-stamp: unknown revision {current!r} — re-stamping head", flush=True)
                    conn.execute("DELETE FROM alembic_version")
                    conn.commit()
                else:
                    print("[v4.11.0] auto-stamp: alembic_version table is EMPTY — stamping head", flush=True)
                    print("[v4.11.0] auto-stamp: это след упавшего upgrade (таблица создаётся до записи ревизии)", flush=True)

                command.stamp(config, "head")
                print("[v4.11.0] auto-stamp: stamp head done", flush=True)
                return

            # БД существует, таблицы есть, alembic_version нет → auto-stamp.
            non_alembic_tables = [t for t in all_tables if t != "alembic_version"]
            print(f"[v4.8.9.2] auto-stamp: {len(non_alembic_tables)} tables exist but no alembic_version — stamping head", flush=True)
            print("[v4.8.9.2] auto-stamp: this is a legacy DB (created by init_db() before v4.8.9)", flush=True)

            command.stamp(config, "head")
            print("[v4.8.9.2] auto-stamp: stamp head done — DB marked as up-to-date", flush=True)

            # Verify stamp succeeded
            cursor = conn.execute("SELECT version_num FROM alembic_version")
            version_row = cursor.fetchone()
            print(f"[v4.8.9.2] auto-stamp: verification — alembic_version={version_row[0] if version_row else 'NULL'}", flush=True)
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(f"[v4.8.9.2] auto-stamp: ERROR inspecting DB: {type(e).__name__}: {e}", flush=True)
        print("[v4.8.9.2] auto-stamp: falling through to alembic upgrade head (may fail)", flush=True)
    except Exception as e:
        print(f"[v4.8.9.2] auto-stamp: UNEXPECTED ERROR: {type(e).__name__}: {e}", flush=True)
        import traceback
        traceback.print_exc(file=sys.stderr)


async def init_db_with_fallback() -> None:
    """v4.8.9: запускает миграции через Alembic, с fallback на init_db().

    Если env DB_USE_LEGACY_MIGRATIONS=1 — вызывает init_db() (старый путь).
    Иначе — вызывает run_migrations_async() (Alembic, с auto-stamp).

    Это точка входа из bot.py lifespan startup.
    """
    if os.getenv("DB_USE_LEGACY_MIGRATIONS") == "1":
        # Рубильник: принудительно старый путь, Alembic не вызывается вовсе.
        await init_db()
        return

    # v4.11.0 (Task 12): настоящий fallback. Раньше функция называлась
    # «with_fallback», но никакого запасного пути в ней не было: любое
    # исключение из миграций поднималось наверх и бот не стартовал. Именно
    # так прод дважды и лёг.
    #
    # Миграции — не та операция, ради которой стоит держать бота
    # выключенным. init_db() идемпотентна (PRAGMA table_info → ALTER TABLE
    # ADD COLUMN) и отрабатывает на каждом старте, так что запасной путь
    # безопасен по построению.
    try:
        await run_migrations_async()
    except Exception as e:
        logger.error(
            "MIGRATIONS FAILED (%s: %s) — откат на init_db(). "
            "Бот поднимется, но схему нужно разобрать вручную: "
            "проверьте alembic_version и migrations/versions.",
            type(e).__name__, e,
        )
        # print — дублируем в stderr: в Bothost логи logging могут
        # фильтроваться по уровню, а stderr показывается всегда.
        print(
            f"[v4.11.0] MIGRATIONS FAILED: {type(e).__name__}: {e} — falling back to init_db()",
            file=sys.stderr, flush=True,
        )
        await init_db()
