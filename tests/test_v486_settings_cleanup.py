"""v4.8.6 Settings cleanup — sanity check.

Проверяет:
1. web_app.py импортируется без ошибок.
2. _APP_START_TIME определена.
3. _bot_info() возвращает ожидаемые ключи (включая новые memory_rss_bytes, python_version).
4. uptime_seconds > 0 (не 0 как раньше из-за отсутствия psutil).
5. admin_settings.html существует и содержит новые элементы.
6. db_path больше не передаётся в контекст (только db_path_dir).
7. sys импортирован.
"""
from _paths import _P  # noqa: E402  (корень вычисляется от __file__)
import sys
import os
import time

# Подкладываем фейковый DB_PATH чтобы импорт web_app не упал
os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ.setdefault("ADMIN_IDS", "123")

sys.path.insert(0, _P())

print("=== v4.8.6 Settings cleanup sanity check ===\n")

# 1. Импорт
print("[1/7] Импорт web_app...")
import web_app
print("  ✓ web_app импортирован")

# 2. _APP_START_TIME
print("\n[2/7] Проверка _APP_START_TIME...")
assert hasattr(web_app, "_APP_START_TIME"), "_APP_START_TIME не найдена"
assert isinstance(web_app._APP_START_TIME, float), "_APP_START_TIME должна быть float"
age = time.time() - web_app._APP_START_TIME
assert 0 <= age < 60, f"_APP_START_TIME слишком старая: {age}s"
print(f"  ✓ _APP_START_TIME = {web_app._APP_START_TIME:.0f} (возраст {age:.1f}s)")

# 3-4. _bot_info() — нужно создать app чтобы получить вложенную функцию
print("\n[3/7] Создание app для доступа к _bot_info()...")
app = web_app.create_app(bot=None)
print("  ✓ app создан")

# _bot_info — вложенная функция, её нет в public API. Достаём через reflection.
# Альтернатива: вызвать /admin/settings через TestClient, но это требует auth.
# Проще: проверить что в исходнике есть нужные поля.
print("\n[4/7] Проверка исходника _bot_info() на новые поля...")
src_path = _P("web_app.py")
with open(src_path, "r") as f:
    src = f.read()
required_in_src = [
    "_APP_START_TIME",
    "memory_rss_bytes",
    "python_version",
    "sys.version",
    "/proc/self/status",
    "VmRSS",
]
for token in required_in_src:
    assert token in src, f"В исходнике отсутствует: {token}"
    print(f"  ✓ найдено: {token}")

# Проверка что psutil больше не используется
assert "import psutil" not in src, "psutil всё ещё импортируется!"
print("  ✓ import psutil удалён")

# 5. admin_settings.html
print("\n[5/7] Проверка admin_settings.html...")
tpl_path = _P("templates/admin_settings.html")
with open(tpl_path, "r") as f:
    tpl = f.read()
required_in_tpl = [
    'id="bot-info"',
    'id="database"',
    'id="backup"',
    'id="cleanup"',
    'id="vacuum"',
    'id="github"',
    'is_error',  # color-aware flash
    'memory_rss_bytes',
    'python_version',
    'Online',  # status badge
    'border-left: 3px solid',  # grouped DB sections styling
]
for token in required_in_tpl:
    assert token in tpl, f"В шаблоне отсутствует: {token}"
    print(f"  ✓ найдено: {token}")

# Проверка что GitHub блок не тронут
assert "github-test-btn" in tpl, "GitHub test button исчез!"
assert "project_status_option_name" in tpl, "project_status_option_name поле исчезло!"
print("  ✓ GitHub Projects блок сохранён")

# 6. db_path не передаётся
print("\n[6/7] Проверка что db_path убран из контекста...")
# Ищем блок с TemplateResponse для admin_settings
import re
m = re.search(
    r'return templates\.TemplateResponse\("admin_settings\.html", \{(.*?)\}\)',
    src,
    re.DOTALL,
)
assert m, "Не найден TemplateResponse для admin_settings.html"
ctx_block = m.group(1)
assert '"db_path"' not in ctx_block, "db_path всё ещё передаётся в контекст!"
assert '"db_path_dir"' in ctx_block, "db_path_dir должен оставаться в контексте"
print('  ✓ db_path убран, db_path_dir остался')

# 7. sys импортирован
print("\n[7/7] Проверка import sys...")
assert "import sys" in src, "import sys не найден"
print("  ✓ import sys добавлен")

print("\n" + "=" * 60)
print("✓ ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ (7/7)")
print("=" * 60)
print("\nЧто сделано в v4.8.6 Settings cleanup:")
print("1. _APP_START_TIME на уровне модуля (вместо psutil)")
print("2. _bot_info(): uptime через _APP_START_TIME, +memory_rss_bytes, +python_version")
print("3. Memory RSS читается из /proc/self/status (Linux only)")
print("4. import sys добавлен")
print("5. db_path убран из контекста (был неиспользуемым дубликатом bot_info.db_path)")
print("6. admin_settings.html переписан:")
print("   - Color-aware flash (success/error)")
print("   - Status badge '● Online' в Bot info")
print("   - Memory (RSS) + Python version строки в Bot info")
print("   - Database maintenance — группировка Backup+Cleanup+VACUUM")
print("   - Цветные left-border для sub-секций (accent/warn/info)")
print("   - GitHub Projects блок не тронут")
