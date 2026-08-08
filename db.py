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

    v4.8.0: ВНИМАНИЕ — WordFilter объявлен deprecated. Используйте KeywordWatch
    (ниже). Код word-filter будет удалён в v4.8.1.

    v4.8.1: КОД WordFilter удалён из bot_handlers.py. Модель оставлена в
    SQLAlchemy только для сохранения таблицы word_filters в БД (исторические
    данные). Новые записи через /addword больше не создаются (команда
    отвечает заглушкой). Если нужно очистить таблицу — через /admin/cleanup
    или прямой SQL. Не используйте эту модель в новом коде — она оставлена
    только для обратной совместимости с существующими данными.
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

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(BigInteger, nullable=False, default=0, index=True)  # 0 = global (всегда)
    phrase = Column(String(255), nullable=False)              # отслеживаемая фраза
    ban_in_night_mode = Column(Boolean, default=False, nullable=False)  # авто-бан ночью
    rules_section = Column(String(64), nullable=True)         # ID секции сайта правил (для #11)
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
    # ── v4.7.0: авто-обнаружение TG-админов ────────────────────────────
    # is_pending=True — WebUser создан sync-кнопкой, ждёт /start от юзера
    # (is_active=False, без пароля). На /start → генерим пароль, is_active=True,
    # is_pending=False, шлём DM с credentials.
    is_pending = Column(Boolean, default=False, nullable=False)
    # auto_discovered=True — маркер что учётка создана автоматически через sync,
    # а не вручную SU. Полезно для статистики и для отличия исторических ручных
    # учёток (v4.6.x и ранее) от новых авто.
    auto_discovered = Column(Boolean, default=False, nullable=False)


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
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_keyword_watch_chat_id "
            "ON keyword_watch (chat_id)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_keyword_watch_is_active "
            "ON keyword_watch (is_active)"
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
