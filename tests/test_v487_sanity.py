"""v4.8.7 — sanity check для всех 8 пунктов скоупа.

Проверяет (без запуска сервера — через импорт + unittest assertions):
  1. db.py: connect_args={"timeout": 30} + PRAGMA busy_timeout=30000
  2. web_app.py: SESSION_SECRET gating (create_app без env падает, с флагом нет)
  3. web_app.py: _COOKIE_SECURE через env (default True, override в "0")
  4. web_app.py: SU login использует hmac.compare_digest (через статический анализ)
  5. web_app.py: _verify_token проверяет срок годности (expired → None)
  6. web_app.py: _SESSION_TTL_SECONDS через env (default 7 дней)
  7. bot_handlers.py: _background_tasks set + _spawn_background_task helper
  8. bot_handlers.py: tg_safe_call helper + import TelegramRetryAfter
  9. bot_handlers.py: все 5 asyncio.create_task заменены на _spawn_background_task
 10. bot_handlers.py: 13 call sites используют tg_safe_call
 11. web_app.py: blocking SQLite в /admin/* роутах обёрнут в asyncio.to_thread
 12. web_app.py: APP_VERSION = "v4.8.7"
 13. templates/base.html: changelog для v4.8.7 присутствует

Запуск: python scripts/test_v487_sanity.py
"""
from _paths import _P  # noqa: E402  (корень вычисляется от __file__)
import os
import sys
import re
import ast

os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ["ADMIN_IDS"] = "123"
os.environ["WEB_ALLOW_NO_SECRET"] = "1"
os.environ["WEB_COOKIE_SECURE"] = "0"

sys.path.insert(0, _P())
sys.path.insert(0, _P())  # symlink fallback

print("=== v4.8.7 sanity check ===\n")

# 1. db.py: busy_timeout
print("[1] db.py: connect_args + PRAGMA busy_timeout...")
with open(_P("db.py")) as f:
    db_src = f.read()
assert 'connect_args={"timeout": 30}' in db_src or 'connect_args={"timeout": 30}' in db_src.replace(" ", ""), \
    "db.py: missing connect_args={'timeout': 30}"
# More flexible check
assert re.search(r'connect_args\s*=\s*\{\s*["\']timeout["\']\s*:\s*30\s*\}', db_src), \
    "db.py: missing connect_args with timeout=30"
assert "PRAGMA busy_timeout=30000" in db_src, "db.py: missing PRAGMA busy_timeout=30000"
print("    ✓ connect_args={timeout:30} + PRAGMA busy_timeout=30000")

# 2. SESSION_SECRET gating
print("\n[2] web_app.py: SESSION_SECRET gating в create_app...")
import web_app
# When WEB_ALLOW_NO_SECRET=1 — create_app should NOT raise
app = web_app.create_app(bot=None)
assert app is not None
print("    ✓ create_app() works with WEB_ALLOW_NO_SECRET=1")
# Verify that without WEB_ALLOW_NO_SECRET it would raise — check source
with open(_P("web_app.py")) as f:
    wa_src = f.read()
assert "_SESSION_SECRET_EXPLICIT" in wa_src, "missing _SESSION_SECRET_EXPLICIT flag"
assert 'WEB_ALLOW_NO_SECRET' in wa_src, "missing WEB_ALLOW_NO_SECRET bypass"
assert "RuntimeError" in wa_src and "SESSION_SECRET env var is required" in wa_src, \
    "missing RuntimeError when SESSION_SECRET not set"
print("    ✓ RuntimeError raised when SESSION_SECRET missing + WEB_ALLOW_NO_SECRET!=1")

# 3. _COOKIE_SECURE
print("\n[3] web_app.py: _COOKIE_SECURE через env (default True)...")
# Reload web_app with default env (no WEB_COOKIE_SECURE)
del os.environ["WEB_COOKIE_SECURE"]
import importlib
importlib.reload(web_app)
assert web_app._COOKIE_SECURE is True, f"expected True by default, got {web_app._COOKIE_SECURE}"
print("    ✓ default _COOKIE_SECURE=True (HTTPS-ready)")
os.environ["WEB_COOKIE_SECURE"] = "0"
importlib.reload(web_app)
assert web_app._COOKIE_SECURE is False, f"expected False when WEB_COOKIE_SECURE=0, got {web_app._COOKIE_SECURE}"
print("    ✓ WEB_COOKIE_SECURE=0 → _COOKIE_SECURE=False (dev mode)")
# Reload once more for downstream tests
importlib.reload(web_app)

# 4. SU password uses hmac.compare_digest
print("\n[4] web/auth.py: SU password compare_digest...")
# v4.9.0 (Task 10): роут /login переехал из web_app.py в web/auth.py.
with open(_P("web/auth.py")) as f:
    auth_src = f.read()
assert re.search(r"hmac\.compare_digest\s*\(\s*password\s*,\s*web_app\.WEB_PASSWORD\s*\)", auth_src), \
    "SU login should use hmac.compare_digest(password, web_app.WEB_PASSWORD)"
src_lines = auth_src.split("\n")
violations = []
for i, line in enumerate(src_lines, 1):
    stripped = line.split("#", 1)[0].strip()
    if "password != web_app.WEB_PASSWORD" in stripped:
        violations.append((i, line))
assert not violations, f"found `password != WEB_PASSWORD` at lines: {violations}"
print("    ✓ SU login uses hmac.compare_digest (no != comparison)")

# 5. _verify_token checks expiry
print("\n[5] web_app.py: _verify_token checks expiry...")
import time, json
# Fresh token
t = web_app._make_token("alice", is_su=False, role="admin")
assert web_app._verify_token(t) is not None
print("    ✓ fresh token valid")
# Expired token (TTL + 60s in past)
payload = {"u":"bob","s":0,"r":"admin","t":int(time.time())-(web_app._SESSION_TTL_SECONDS+60),"n":"x"}
raw = json.dumps(payload, separators=(",",":"))
sig = web_app._sign(raw)
t_old = f"{raw}:{sig}"
assert web_app._verify_token(t_old) is None, "expired token should be rejected"
print("    ✓ expired token (age > TTL) rejected")
# Future timestamp token
payload = {"u":"carol","s":0,"r":"admin","t":int(time.time())+100,"n":"x"}
raw = json.dumps(payload, separators=(",",":"))
sig = web_app._sign(raw)
t_future = f"{raw}:{sig}"
assert web_app._verify_token(t_future) is None, "future-dated token should be rejected"
print("    ✓ future-dated token rejected (>5s tolerance)")

# 6. _SESSION_TTL_SECONDS env
print("\n[6] web_app.py: _SESSION_TTL_SECONDS через env (default 604800)...")
assert web_app._SESSION_TTL_SECONDS == 604800, f"expected 604800 (7 days), got {web_app._SESSION_TTL_SECONDS}"
print(f"    ✓ default _SESSION_TTL_SECONDS={web_app._SESSION_TTL_SECONDS} (7 дней)")

# 7. _background_tasks + _spawn_background_task
print("\n[7] bot_handlers.py: _background_tasks + _spawn_background_task...")
import bot_handlers
assert hasattr(bot_handlers, "_background_tasks"), "missing _background_tasks set"
assert isinstance(bot_handlers._background_tasks, set), "_background_tasks must be a set"
assert hasattr(bot_handlers, "_spawn_background_task"), "missing _spawn_background_task helper"
assert callable(bot_handlers._spawn_background_task), "_spawn_background_task must be callable"
print("    ✓ _background_tasks: set + _spawn_background_task() helper")

# Functional test
import asyncio
async def _test_spawn():
    results = []
    async def ok_task():
        await asyncio.sleep(0.001)
        results.append("ok")
    t = bot_handlers._spawn_background_task(ok_task(), label="test")
    await t
    assert t not in bot_handlers._background_tasks, "task should be discarded after done"
    assert results == ["ok"]
asyncio.run(_test_spawn())
print("    ✓ _spawn_background_task: strong ref during, discard after done")

# 8. tg_safe_call + import TelegramRetryAfter
print("\n[8] bot_handlers.py: tg_safe_call + TelegramRetryAfter import...")
assert hasattr(bot_handlers, "tg_safe_call"), "missing tg_safe_call"
assert callable(bot_handlers.tg_safe_call), "tg_safe_call must be callable"
from aiogram.exceptions import TelegramRetryAfter
# Verify bot_handlers imports it
with open(_P("bot_handlers.py")) as f:
    bh_src = f.read()
assert "TelegramRetryAfter" in bh_src, "bot_handlers.py should import TelegramRetryAfter"
assert "tg_safe_call" in bh_src
print("    ✓ tg_safe_call() defined + TelegramRetryAfter imported")

# Functional test
async def _test_tg_safe():
    # success
    async def ok():
        return "ok"
    r = await bot_handlers.tg_safe_call(ok, label="test_ok")
    assert r == "ok"
    # retry then success
    bot_handlers._RETRY_CAP = 0  # instant retry
    state = {"n": 0}
    async def flaky():
        state["n"] += 1
        if state["n"] == 1:
            raise TelegramRetryAfter(method=None, message="flood", retry_after=0)
        return "recovered"
    r = await bot_handlers.tg_safe_call(flaky, label="test_retry")
    assert r == "recovered"
    assert state["n"] == 2
asyncio.run(_test_tg_safe())
print("    ✓ tg_safe_call: success + retry-on-429 verified")

# 9. All 5 asyncio.create_task replaced
#
# Проверка искала все пять вызовов в bot_handlers.py и с v4.8.9 стояла красной:
# при декомпозиции handle_group_command пятый вызов уехал в mod_commands.py
# (_del_msg). Helper используется по-прежнему, изменился только файл — поэтому
# считаем по обоим модулям, а не по одному.
print("\n[9] bot_handlers.py + mod_commands.py: все 5 asyncio.create_task заменены "
      "на _spawn_background_task...")
with open(_P("mod_commands.py"), encoding="utf-8") as f:
    mc_src = f.read()

_spawn_src = bh_src + "\n" + mc_src
# Count _spawn_background_task CALLS (excluding definition)
calls = re.findall(r"_spawn_background_task\s*\(", _spawn_src)
# Subtract 1 for the definition line (def _spawn_background_task...)
def_count = len(re.findall(r"def\s+_spawn_background_task\s*\(", _spawn_src))
actual_calls = len(calls) - def_count
assert actual_calls >= 5, f"expected ≥5 _spawn_background_task call sites, got {actual_calls}"
print(f"    ✓ {actual_calls} _spawn_background_task call sites (expected ≥5)")

# 9b. Голых asyncio.create_task не осталось ни в одном из двух модулей —
# именно это правило охраняет инвариант (задачу может собрать GC на середине).
# Разбор через ast, а не регексом: в bot_handlers.py "asyncio.create_task()"
# трижды встречается в комментариях, объясняющих, почему так делать нельзя.
import ast as _ast


def _bare_create_task(src: str) -> list[int]:
    """Строки с asyncio.create_task вне тела _spawn_background_task."""
    tree = _ast.parse(src)
    allowed: set[int] = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef) and node.name == "_spawn_background_task":
            allowed.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    found = []
    for node in _ast.walk(tree):
        if (
            isinstance(node, _ast.Call)
            and isinstance(node.func, _ast.Attribute)
            and node.func.attr == "create_task"
            and isinstance(node.func.value, _ast.Name)
            and node.func.value.id == "asyncio"
            and node.lineno not in allowed
        ):
            found.append(node.lineno)
    return found


for _name, _src in (("bot_handlers.py", bh_src), ("mod_commands.py", mc_src)):
    _bare = _bare_create_task(_src)
    assert not _bare, f"{_name}: голый asyncio.create_task в строках {_bare}"
print("    ✓ голых asyncio.create_task нет")

# 10. 13 tg_safe_call call sites
print("\n[10] bot_handlers.py: ≥13 tg_safe_call call sites...")
# Часть call sites уехала в mod_commands.py вместе с ветками команд
# (v4.8.9/v4.8.10) — считаем по обоим модулям, как и в проверке [9].
tg_calls = re.findall(r"tg_safe_call\s*\(\s*\n?\s*lambda", bh_src + "\n" + mc_src)
assert len(tg_calls) >= 13, f"expected ≥13 tg_safe_call(lambda:...) call sites, got {len(tg_calls)}"
print(f"    ✓ {len(tg_calls)} tg_safe_call(lambda: ...) call sites (expected ≥13)")

# 11. Blocking SQLite wrapped in asyncio.to_thread
print("\n[11] web_app.py: blocking SQLite wrapped in asyncio.to_thread...")
# Count asyncio.to_thread calls.
# v4.9.0 (Task 4): вызовы разъехались по web/ вместе с роутами
# (/admin/cleanup → web/admin_cleanup.py, дальше — по мере декомпозиции).
_to_thread_sources = [_P("web_app.py"), _P("web/admin_cleanup.py"), _P("web/admin_settings.py")]
to_thread_count = sum(
    len(re.findall(r"asyncio\.to_thread\s*\(", open(_src).read()))
    for _src in _to_thread_sources if os.path.exists(_src)
)
assert to_thread_count >= 7, f"expected ≥7 asyncio.to_thread calls (7 blocking sites), got {to_thread_count}"
print(f"    ✓ {to_thread_count} asyncio.to_thread() calls (expected ≥7)")
# Verify _wal_checkpoint_async and _backup_db_async exist
assert hasattr(web_app, "_wal_checkpoint_async"), "missing _wal_checkpoint_async"
assert hasattr(web_app, "_backup_db_async"), "missing _backup_db_async"
print("    ✓ _wal_checkpoint_async() + _backup_db_async() helpers defined")

# 12. APP_VERSION
# Проверка была прибита к "v4.8.7" — версии, в которой писался этот файл.
# Сверять релиз с константой внутри теста нечем, поэтому проверяем формат и
# что версия не ниже той, в которой появились охраняемые здесь инварианты.
print("\n[12] web_app.py: APP_VERSION...")
_m = re.match(r"^v(\d+)\.(\d+)\.(\d+)", web_app.APP_VERSION)
assert _m, f"unexpected APP_VERSION format: {web_app.APP_VERSION!r}"
assert tuple(int(g) for g in _m.groups()) >= (4, 8, 7), \
    f"expected >= v4.8.7, got {web_app.APP_VERSION}"
print(f"    ✓ APP_VERSION = {web_app.APP_VERSION}")

# 13. Changelog in base.html
print("\n[13] templates/base.html: changelog для v4.8.7 присутствует...")
with open(_P("templates/base.html")) as f:
    base_src = f.read()
assert "v4.8.7" in base_src, "base.html missing v4.8.7 changelog entry"
# Дата релиза v4.8.7 в changelog менялась; сам факт записи проверяется строкой
# выше, а конкретное число к инвариантам не относится.
assert "tg_safe_call" in base_src, "changelog should mention tg_safe_call"
assert "_spawn_background_task" in base_src or "_background_tasks" in base_src, \
    "changelog should mention background_tasks fix"
assert "busy_timeout" in base_src, "changelog should mention busy_timeout"
assert "compare_digest" in base_src, "changelog should mention compare_digest"
assert "SESSION_SECRET" in base_src, "changelog should mention SESSION_SECRET requirement"
assert "_COOKIE_SECURE" in base_src or "WEB_COOKIE_SECURE" in base_src, \
    "changelog should mention cookie secure flag"
print("    ✓ changelog v4.8.7 содержит все ключевые элементы")

print()
print("=" * 60)
print(f"ALL v4.8.7 SANITY CHECKS PASSED")
print("=" * 60)
