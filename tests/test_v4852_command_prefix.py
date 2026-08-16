#!/usr/bin/env python3
"""
test_v4852_command_prefix.py — тесты для hotfix v4.8.5.2.

Проверяет, что декораторы cmd_idea_dm и cmd_idea_modchat используют
Command("idea", prefix="!/") — то есть ловят и !idea, и /idea.

В v4.8.5/4.8.5.1 там было Command("idea") без prefix=, что в aiogram 3.x
по умолчанию использует prefix="/" — !idea не ловилось, бот молчал.
"""
from _paths import _P  # noqa: E402  (корень вычисляется от __file__)

import ast
import re
import sys
from pathlib import Path

V485_DIR = Path(_P())
passed = 0
failed = 0
checks = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        checks.append(f"  ✓ {name}")
    else:
        failed += 1
        checks.append(f"  ✗ {name}  {detail}")


# ── Парсим bot_handlers.py через AST, ищем декораторы cmd_idea_* ────────
bh_src = (V485_DIR / "bot_handlers.py").read_text(encoding="utf-8")
tree = ast.parse(bh_src)


def get_decorator_calls(func_name: str) -> list[ast.Call]:
    """Возвращает список ast.Call для декораторов функции с именем func_name."""
    result = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == func_name:
            for dec in node.decorator_list:
                # Декоратор может быть @router.message(...) — это Call.
                if isinstance(dec, ast.Call):
                    result.append(dec)
    return result


def find_command_call_in_decorator(dec: ast.Call) -> ast.Call | None:
    """Ищет Command(...) внутри декоратора router.message(...)."""
    # dec.args — позиционные аргументы router.message(...).
    for arg in dec.args:
        if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) \
                and arg.func.id == "Command":
            return arg
    return None


# ── T1: cmd_idea_dm использует Command("idea", prefix="!/") ─────────────
def t1_idea_dm_prefix():
    decs = get_decorator_calls("cmd_idea_dm")
    if not decs:
        check("T1: cmd_idea_dm has decorator", False, "no decorator found")
        return
    cmd_call = None
    for dec in decs:
        cmd_call = find_command_call_in_decorator(dec)
        if cmd_call:
            break
    if not cmd_call:
        check("T1: cmd_idea_dm decorator has Command()", False,
              "no Command() in decorator")
        return

    # Проверяем первый позиционный аргумент — должен быть "idea".
    if not cmd_call.args:
        check("T1: Command() has 'idea' arg", False, "no args")
        return
    first_arg = cmd_call.args[0]
    if not isinstance(first_arg, ast.Constant) or first_arg.value != "idea":
        check("T1: Command() first arg is 'idea'", False,
              f"got {ast.dump(first_arg)}")
        return
    check("T1: Command('idea') first arg = 'idea'", True)

    # Ищем prefix= ключевым аргументом.
    prefix_kw = None
    for kw in cmd_call.keywords:
        if kw.arg == "prefix":
            prefix_kw = kw
            break
    if not prefix_kw:
        check("T1: Command('idea', prefix='!/') — prefix= present",
              False, "no prefix= keyword arg")
        return
    if not isinstance(prefix_kw.value, ast.Constant) \
            or prefix_kw.value.value != "!/":
        check("T1: Command('idea', prefix='!/') — prefix value is '!/'",
              False,
              f"got {ast.dump(prefix_kw.value)}")
        return
    check("T1: Command('idea', prefix='!/') — prefix='!/'", True)


# ── T2: cmd_idea_modchat использует Command("idea", prefix="!/") ────────
def t2_idea_modchat_prefix():
    decs = get_decorator_calls("cmd_idea_modchat")
    if not decs:
        check("T2: cmd_idea_modchat has decorator", False, "no decorator found")
        return
    cmd_call = None
    for dec in decs:
        cmd_call = find_command_call_in_decorator(dec)
        if cmd_call:
            break
    if not cmd_call:
        check("T2: cmd_idea_modchat decorator has Command()", False,
              "no Command() in decorator")
        return

    first_arg = cmd_call.args[0] if cmd_call.args else None
    if not isinstance(first_arg, ast.Constant) or first_arg.value != "idea":
        check("T2: Command('idea') first arg = 'idea'", False,
              f"got {ast.dump(first_arg) if first_arg else 'None'}")
        return
    check("T2: Command('idea') first arg = 'idea'", True)

    prefix_kw = None
    for kw in cmd_call.keywords:
        if kw.arg == "prefix":
            prefix_kw = kw
            break
    if not prefix_kw:
        check("T2: prefix='!/' present", False, "no prefix= keyword")
        return
    if not isinstance(prefix_kw.value, ast.Constant) \
            or prefix_kw.value.value != "!/":
        check("T2: prefix value is '!/'", False,
              f"got {ast.dump(prefix_kw.value)}")
        return
    check("T2: Command('idea', prefix='!/') — prefix='!/'", True)


# ── T3: APP_VERSION = v4.8.5+ в web_app.py ──────────────────────────────
def t3_app_version():
    import re as _re
    src = (V485_DIR / "web_app.py").read_text(encoding="utf-8")
    # v4.8.6: принимаем v4.8.5+ (включая v4.8.6, v4.9.0 и т.д.)
    m = _re.search(r'APP_VERSION\s*=\s*"v(\d+)\.(\d+)\.(\d+[^"]*)"', src)
    if m:
        major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
        ok = (major, minor, patch) >= (4, 8, 5)
        ver_str = f"v{m.group(1)}.{m.group(2)}.{m.group(3)}"
    else:
        ok = False
        ver_str = None
    check(f"T3: APP_VERSION = v4.8.5+ (got {ver_str!r})", ok)


# ── T4: changelog v4.8.5.2 в base.html ──────────────────────────────────
def t4_changelog():
    src = (V485_DIR / "templates" / "base.html").read_text(encoding="utf-8")
    ok1 = "v4.8.5.2" in src
    ok2 = "Command" in src and "prefix" in src
    ok3 = "stealth_catchall_private" in src or "stealth" in src.lower()
    check("T4: changelog v4.8.5.2 present", ok1)
    check("T4a: changelog mentions Command+prefix", ok2)
    check("T4b: changelog mentions stealth (cause)", ok3)


# ── T5: только 2 вхождения Command("idea", prefix="!/") ────────────────
def t5_only_two_handlers():
    """Должно быть ровно 2 вхождения — для DM и для modchat."""
    src = (V485_DIR / "bot_handlers.py").read_text(encoding="utf-8")
    # Считаем с regex с допуском пробелов.
    pattern = re.compile(r'Command\s*\(\s*"idea"\s*,\s*prefix\s*=\s*"!/"\s*\)')
    matches = pattern.findall(src)
    check("T5: exactly 2 Command('idea', prefix='!/') occurrences",
          len(matches) == 2, f"found {len(matches)}")


# ── T6: НЕ осталось Command("idea") без prefix= ────────────────────────
def t6_no_bare_command_idea():
    """Проверяем, что не осталось bare Command('idea') без prefix=."""
    src = (V485_DIR / "bot_handlers.py").read_text(encoding="utf-8")
    # Ищем Command("idea") или Command('idea') БЕЗ prefix=.
    # Подход: находим все Command(...idea...) и проверяем каждый на наличие
    # prefix= в тех же скобках.
    bare_pattern = re.compile(
        r'Command\s*\(\s*["\']idea["\']\s*(?!\s*,\s*prefix\s*=)[^)]*\)'
    )
    matches = bare_pattern.findall(src)
    check("T6: no bare Command('idea') without prefix=",
          len(matches) == 0, f"found {len(matches)}: {matches}")


# ── T7: проверяем, что в aiogram Command default prefix — это "/" ──────
def t7_aiogram_default_prefix_is_slash():
    """Smoke-тест: убеждаемся, что наше понимание aiogram верно.
    Если aiogram вдруг поменяет default prefix — этот тест провалится, и
    мы поймём, что фикс нужно пересмотреть.
    """
    try:
        from aiogram.filters import Command  # type: ignore
        import inspect
        sig = inspect.signature(Command.__init__)
        prefix_param = sig.parameters.get("prefix")
        if prefix_param is None:
            check("T7: aiogram Command has prefix param", False,
                  "no prefix param in signature")
            return
        default = prefix_param.default
        # Default должен быть "/" (или tuple containing "/").
        if default == "/":
            check("T7: aiogram Command default prefix='/'", True)
        elif isinstance(default, (tuple, list)) and "/" in default:
            check("T7: aiogram Command default prefix contains '/'", True)
        else:
            check("T7: aiogram Command default prefix='/'", False,
                  f"default={default!r}")
    except ImportError:
        check("T7: aiogram not installed — SKIP", True,
              "(skipped — aiogram not in env)")
    except Exception as e:
        check("T7: aiogram Command signature", False, str(e))


# ── T8: /help (full + moderator) по-прежнему содержит !idea ────────────
def t8_help_still_contains_idea():
    """Регрессия: !idea должна остаться в /help."""
    src = (V485_DIR / "bot_handlers.py").read_text(encoding="utf-8")
    # В _build_help_full_rich и _build_help_moderator_rich.
    ok1 = "!idea" in src
    check("T8: !idea mentioned in bot_handlers.py (help)", ok1)


# ── T9: синтаксис всех 3 изменённых файлов ─────────────────────────────
def t9_syntax():
    for fname in ("bot_handlers.py", "web_app.py"):
        try:
            ast.parse((V485_DIR / fname).read_text(encoding="utf-8"))
            check(f"T9: {fname} syntax OK", True)
        except SyntaxError as e:
            check(f"T9: {fname} syntax OK", False, str(e))


# ── T10: проверка, что !idea через regex тоже ловит ────────────────────
def t10_idea_command_pattern_in_help():
    """В help тексте (Rich Message builders) !idea должна быть задокументирована
    как 'idea' (через ! или / — оба валидны, но help показывает !)."""
    src = (V485_DIR / "bot_handlers.py").read_text(encoding="utf-8")
    # Ищем что-то вроде ("!idea <текст>", "описание") в tuples help-билдеров.
    if re.search(r'["\']!idea\b', src):
        check("T10: help references '!idea' string", True)
    else:
        check("T10: help references '!idea' string", False,
              "no '!idea' string found")


# ── Запуск ──────────────────────────────────────────────────────────────
def main():
    print("=" * 70)
    print("test_v4852_command_prefix.py — hotfix v4.8.5.2 tests")
    print("=" * 70)
    print()

    t1_idea_dm_prefix()
    t2_idea_modchat_prefix()
    t3_app_version()
    t4_changelog()
    t5_only_two_handlers()
    t6_no_bare_command_idea()
    t7_aiogram_default_prefix_is_slash()
    t8_help_still_contains_idea()
    t9_syntax()
    t10_idea_command_pattern_in_help()

    print()
    for c in checks:
        print(c)
    print()
    print(f"Total: {passed} passed, {failed} failed, {passed+failed} checks")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
