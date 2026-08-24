"""Реестр команд бота — единственный источник истины (v5.1.0).

До v5.1.0 список команд жил в трёх местах: _ALL_MOD_COMMANDS в
bot_handlers.py и два блока /help (строки 7168 и 7319). Копии успели
разойтись. Теперь команда описывается здесь один раз, а диспетчер, меню
Telegram и тексты /help выводятся из этого описания.

Модуль намеренно не зависит ни от aiogram, ни от БД: это делает его
тестируемым напрямую, без поднятия бота.

Нормализация префикса. Исторически команды жили на «!», сейчас основной
префикс — «/» (общая конвенция Telegram). «!» остаётся рабочим алиасом, но
нигде не документируется и не показывается в меню. Вместо того чтобы
править якорь в пятнадцати паттернах, префикс и суффикс «@botusername»
срезаются один раз в strip_prefix, а паттерны матчат уже нормализованную
строку: «ban @vasya Скам» вместо «!ban @vasya Скам».
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class Access(StrEnum):
    """Уровень доступа, необходимый для команды."""

    USER = "user"    # доступно всем участникам чата
    MOD = "mod"      # проверяется через _is_admin()
    ADMIN = "admin"  # дополнительно требует роль su/admin


@dataclass(frozen=True)
class CommandSpec:
    """Описание одной команды.

    :param name: имя без префикса («ban»)
    :param pattern: regex по нормализованной строке (без «^!»)
    :param args_hint: аргументы для текста справки («<длит> <причина>»)
    :param description: одна строка для /help и меню Telegram
    :param access: минимальный уровень доступа
    :param in_menu: публиковать ли в меню команд Telegram
    """

    name: str
    pattern: re.Pattern
    args_hint: str
    description: str
    access: Access
    in_menu: bool = False


# ── Паттерны ────────────────────────────────────────────────────────────
# Скопированы из bot_handlers.py без изменений, кроме якоря: «^!mute» → «^mute».
#
# v5.1.0 (Task 2): паттерны и их история правок физически перенесены сюда —
# раньше жили в bot_handlers.py:515-578. Комментарии ниже воспроизводят
# оригинальные (v4.8.1, v4.8.3, v4.8.3.1, v4.8.4, v4.8.6, v4.7.20).
#
# v4.8.1: реформа команд ban/warn/mute.
#   • Громкие (ban/warn/mute) — причина ОБЯЗАТЕЛЬНА. После: публичное
#     сообщение в чат + отчёт в репорт-чат (без ephemeral модератору).
#   • Тихие (sban/swarn/smute) — причина НЕОБЯЗАТЕЛЬНА. После: ephemeral
#     модератору (и ephemeral нарушителю для swarn) + отчёт в репорт-чат.
#
# v4.8.3: расширение способов указания цели наказания.
#   • Раньше все команды работали ТОЛЬКО по reply на сообщение нарушителя.
#   • Теперь можно указать цель первым аргументом: @username или TGID.
#   • Reply остаётся приоритетным: если есть reply — цель из аргумента игнорируется.
#   • Скриншот: модератор может приложить фото к команде (caption содержит команду).
#
# Группа target: (?P<target>@\w+|\d+) — @username (начинается с @) или TGID (только цифры).
# Если target не указан — команда требует reply (для ban/warn/mute) или
# работает по reply если он есть (для sban/swarn/smute — иначе target=None,
# что приведёт к ошибке резолва в _resolve_punishment_target).
_P_MUTE = re.compile(
    r"^mute\s+(?:(?P<target>@\w+|\d+)\s+)?(?P<dur>\d+[a-zа-я]+)\s+(?P<reason>.+)$",
    re.IGNORECASE | re.DOTALL,
)  # dur + reason обязательны; target опционален (если нет — нужен reply).
_P_WARN = re.compile(
    r"^warn\s+(?:(?P<target>@\w+|\d+)\s+)?(?P<reason>(?!@\w+$|\d+$).+)$",
    re.IGNORECASE | re.DOTALL,
)  # reason обязательна; target опционален.
# v4.8.6: добавлен negative lookahead (?!@\w+$|\d+$) в reason — иначе
# «warn @username» (без причины) матчило reason="@username" и бан влетал
# на reply-target с некорректной причиной. Теперь такой ввод не матчится
# → handler вернёт "укажите причину".
_P_BAN = re.compile(
    r"^ban\s+(?:(?P<target>@\w+|\d+)\s+)?(?P<reason>(?!@\w+$|\d+$).+)$",
    re.IGNORECASE | re.DOTALL,
)  # reason обязательна; target опционален. v4.8.6: тот же фикс, что у _P_WARN.
# v4.8.1: тихие команды (stealth). s = silent/stealth.
# v4.8.3.1 HOTFIX: regex переосмыслен — _P_SWARN/_P_SBAN в v4.8.3 не матчили
#   «swarn Причина» (bare reason без @username/TGID), потому что внутренняя
#   группа (?P<reason>.+) требовала свой собственный \s+, но он уже был
#   съеден внешней \s+. Исправлено: target и reason — два независимых
#   optional-блока на верхнем уровне, каждый со своим \s+. Это позволяет
#   матчить все варианты: «swarn», «swarn Причина», «swarn @user»,
#   «swarn @user Причина», «swarn 12345», «swarn 12345 Причина».
# v4.8.3.1 HOTFIX: _P_SMUTE в v4.8.3 делал всё тело опциональным, поэтому
#   «smute» без длительности матчило (dur=None), а handler звал
#   _parse_duration(None) → AttributeError. Regex оставлен как есть (dur
#   внутри опциональной группы), но handler явно проверяет dur is None и
#   отправляет ephemeral с подсказкой формата.
_P_SMUTE = re.compile(
    r"^smute(?:\s+(?:(?P<target>@\w+|\d+)\s+)?(?P<dur>\d+[a-zа-я]+)(?:\s+(?P<reason>.+))?)?$",
    re.IGNORECASE | re.DOTALL,
)  # dur обяз. ЕСЛИ есть аргументы; reason опц.; target опц.
_P_SWARN = re.compile(
    r"^swarn(?:\s+(?P<target>@\w+|\d+))?(?:\s+(?P<reason>.+))?$",
    re.IGNORECASE | re.DOTALL,
)  # reason опциональна; target опционален; любой из них может быть один.
_P_SBAN = re.compile(
    r"^sban(?:\s+(?P<target>@\w+|\d+))?(?:\s+(?P<reason>.+))?$",
    re.IGNORECASE | re.DOTALL,
)  # reason опциональна; target опционален; любой из них может быть один.
# v5.2.0: команды снятия тоже получили опциональную группу target.
#   • Раньше они матчили только голое имя («^unban\s*$») и потому работали
#     исключительно по reply. Для /unban это была не мелочь, а тупик:
#     забаненного нет в чате, реплаить не на что — снять бан из Telegram
#     было нельзя вообще, только через веб-панель.
#   • Форма та же, что у наказательных команд (@username или TGID), чтобы
#     не заводить второй синтаксис: /unban @vasya, /unban 123456789.
#   • Reply остаётся приоритетнее аргумента — см. _resolve_punishment_target.
_P_UNMUTE = re.compile(r"^unmute(?:\s+(?P<target>@\w+|\d+))?\s*$", re.IGNORECASE)
_P_UNBAN = re.compile(r"^unban(?:\s+(?P<target>@\w+|\d+))?\s*$", re.IGNORECASE)
# У unwarn есть собственный числовой аргумент — количество варнов, и «3»
# одинаково подходит под TGID и под счётчик. Паттерн обе группы просто
# собирает, а разводит их unwarn_args() по наличию reply (см. её docstring).
_P_UNWARN = re.compile(
    r"^unwarn(?:\s+(?P<target>@\w+|\d+))?(?:\s+(?P<count>\d+))?\s*$",
    re.IGNORECASE,
)
_P_WARNS = re.compile(r"^warns(?:\s+(?P<target>@\w+|\d+))?\s*$", re.IGNORECASE)
_P_RESETWARNS = re.compile(
    r"^resetwarns(?:\s+(?P<target>@\w+|\d+))?\s*$",
    re.IGNORECASE,
)
# v4.8.4: resetmc — сброс счётчика автомьютов (прогрессивные муты).
# Цель: reply на сообщение, ИЛИ resetmc @username, ИЛИ resetmc <tgid>.
# Доступ: только SU/Admin (как resetwarns).
_P_RESETMC = re.compile(
    r"^resetmc(?:\s+(?P<target>@\w+|\d+))?\s*$",
    re.IGNORECASE,
)
# v4.7.20: alarm on [duration] / alarm off
# Длительность: опциональная, форматы "1ч" / "1h" / "30м" / "30m" / "2д" / "2d".
# Если не указана — alarm активен до ручного alarm off.
# Примеры: "alarm on", "alarm on 1ч", "alarm on 2h", "alarm off"
_P_ALARM = re.compile(
    r"^alarm\s+(?P<state>on|off|вкл|выкл)"
    r"(?:\s+(?P<amount>\d+)\s*(?P<unit>ч|h|м|m|д|d))?"
    r"\s*$",
    re.IGNORECASE,
)
_P_MYWARNS = re.compile(r"^mywarns\s*$", re.IGNORECASE)
_P_RULES = re.compile(r"^rules\s*$", re.IGNORECASE)


# ── Реестр групповых команд ─────────────────────────────────────────────
GROUP_COMMANDS: tuple[CommandSpec, ...] = (
    # Публичные — единственные, что попадают в меню Telegram.
    CommandSpec("mywarns", _P_MYWARNS, "", "показать свои варны", Access.USER, in_menu=True),
    CommandSpec("rules", _P_RULES, "", "ссылка на правила чата", Access.USER, in_menu=True),
    # Громкие мод-команды.
    CommandSpec("mute", _P_MUTE, "<длит> <причина>",
                "мьют. Длительность: 1d2h, 30м, 2h", Access.MOD),
    CommandSpec("warn", _P_WARN, "<причина>",
                "варн (1 поинт). Сообщение нарушителя удаляется", Access.MOD),
    CommandSpec("ban", _P_BAN, "<причина>",
                "бан. Если reply на стикер — пак автодобавляется", Access.MOD),
    # Тихие (stealth).
    CommandSpec("smute", _P_SMUTE, "<длит> [причина]",
                "мьют без публичного сообщения", Access.MOD),
    CommandSpec("swarn", _P_SWARN, "[причина]",
                "варн. Нарушитель видит причину", Access.MOD),
    CommandSpec("sban", _P_SBAN, "[причина]",
                "бан без публичного сообщения", Access.MOD),
    # Снятие ограничений.
    CommandSpec("unmute", _P_UNMUTE, "[@user|tgid]",
                "снять мьют. Цель: reply, @user или TGID", Access.MOD),
    CommandSpec("unban", _P_UNBAN, "[@user|tgid]",
                "снять бан. Цель: reply, @user или TGID", Access.MOD),
    CommandSpec("unwarn", _P_UNWARN, "[@user|tgid] [N]",
                "снять N последних варнов (по умолчанию 1)", Access.MOD),
    CommandSpec("warns", _P_WARNS, "[@user|tgid]",
                "показать активные варны цели", Access.MOD),
    # Только su/admin — доп. проверка роли живёт в обработчике.
    CommandSpec("resetwarns", _P_RESETWARNS, "[@user|tgid]",
                "обнулить варны", Access.ADMIN),
    CommandSpec("resetmc", _P_RESETMC, "[@user|tgid]",
                "обнулить счётчик автомьютов", Access.ADMIN),
    # Режим тревоги.
    CommandSpec("alarm", _P_ALARM, "on [длит] | off",
                "режим тревоги (усиленные ограничения)", Access.MOD),
)

# Команды, применяющие наказание. На них в диспетчере навешаны проверки
# self-harm и friendly-fire, и для них цель резолвится через
# _resolve_punishment_target.
PUNITIVE: frozenset[str] = frozenset({"ban", "warn", "mute", "sban", "swarn", "smute"})

# v5.2.0: команды снятия. Их цель резолвится мягко
# (_resolve_punishment_target(require_membership=False)): забаненного или
# вышедшего юзера в чате уже нет, и строгая проверка через get_chat_member
# отвергала бы ровно тот случай, ради которого команда и вызывается.
LENIENT_TARGET: frozenset[str] = frozenset(
    {"unmute", "unban", "unwarn", "warns", "resetwarns"}
)


def unwarn_args(match: re.Match, has_reply: bool) -> tuple[str | None, int]:
    """Разводит два числовых смысла в «/unwarn N» — цель и количество.

    Паттерн отдаёт две опциональные группы, но «/unwarn 3» матчится в
    target="3", count=None: одинокое число одинаково похоже и на TGID, и
    на счётчик варнов. Разводит их наличие reply:

      reply + «/unwarn»           → (None, 1)
      reply + «/unwarn 3»         → (None, 3)          число = счётчик
      «/unwarn @vasya»            → ("@vasya", 1)
      «/unwarn @vasya 3»          → ("@vasya", 3)
      «/unwarn 123456789»         → ("123456789", 1)   число = TGID
      «/unwarn 123456789 3»       → ("123456789", 3)

    «/unwarn 3» без reply трактуется как TGID 3: резолв провалится и
    модератор получит внятную ошибку. Обратный выбор (считать это тремя
    варнами) снимал бы варны неизвестно с кого молча.

    Возвращает (target_str, count); count всегда ≥ 1.
    """
    raw_target = match.groupdict().get("target")
    raw_count = match.groupdict().get("count")

    if raw_count is not None:
        target, count = raw_target, int(raw_count)
    elif has_reply and raw_target is not None and raw_target.isdigit():
        target, count = None, int(raw_target)
    else:
        target, count = raw_target, 1

    return target, max(count, 1)

# Публикуются в меню личных чатов. Административные DM-команды
# (/bansticker, /setkeywords, /linkallow и ещё около двадцати) сюда
# намеренно не входят: AllPrivateChats видят все, кто написал боту, и
# публикация вывесила бы наружу всю админскую поверхность.
DM_MENU_COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "привязать учётную запись"),
    ("help", "справка по командам"),
    ("mywarns", "показать свои варны"),
    ("rules", "ссылка на правила"),
)


# ── Имя бота ────────────────────────────────────────────────────────────
# Нужно, чтобы отличить «/ban@degradach_bot» от «/ban@other_bot». Ставится
# один раз при старте из lifespan; фильтры синхронные и сами await'ить
# bot.me() не могут.
_bot_username: str | None = None


def set_bot_username(username: str | None) -> None:
    """Запоминает username бота (без «@»). Вызывается из lifespan."""
    global _bot_username
    _bot_username = username


def get_bot_username() -> str | None:
    """Username бота, либо None если ещё не установлен."""
    return _bot_username


_HEAD = re.compile(
    r"^([!/])([A-Za-z0-9_]+)(?:@([A-Za-z0-9_]+))?(.*)$",
    re.DOTALL,
)


def strip_prefix(text: str | None, bot_username: str | None) -> str | None:
    """Срезает префикс команды и «@botusername».

    Возвращает нормализованную строку («ban @vasya Скам») либо None, если
    текст не является командой этого бота.

    Команда, явно адресованная другому боту («/ban@other_bot»), отбрасывается.
    Если собственный username ещё не известен, явная адресация тоже
    отбрасывается — перехватывать чужие команды хуже, чем пропустить свою;
    команда без «@» при этом продолжает работать.
    """
    if not text:
        return None
    m = _HEAD.match(text.lstrip())
    if m is None:
        return None
    _prefix, name, at_username, rest = m.groups()
    if at_username is not None:
        if not bot_username or at_username.lower() != bot_username.lower():
            return None
    return f"{name}{rest}"


def resolve(
    text: str | None, bot_username: str | None,
) -> tuple[CommandSpec, re.Match] | None:
    """Находит команду в тексте.

    Возвращает пару (спека, match) — match нужен вызывающему коду ради
    именованных групп target/dur/reason. None, если это не наша команда.
    """
    normalized = strip_prefix(text, bot_username)
    if normalized is None:
        return None
    for spec in GROUP_COMMANDS:
        m = spec.pattern.match(normalized)
        if m is not None:
            return spec, m
    return None


def spec_by_name(name: str) -> CommandSpec | None:
    """Спека по имени команды, либо None."""
    for spec in GROUP_COMMANDS:
        if spec.name == name:
            return spec
    return None
