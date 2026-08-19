#!/usr/bin/env python3
"""v4.8.8 verify: найти POST-роуты где осталась старая зависимость без CSRF.

v4.10.0: после декомпозиции create_app() все 34 POST-роута переехали из
web_app.py в web/*.py (там @router.post, а не @app.post). web_app.py теперь
содержит только «надгробные» комментарии вида
`# Раньше тут были inline @app.post("/logout")` — их литералы путей и
декораторов раньше давали 12 ложных совпадений и скрипт молча печатал OK,
ничего не проверяя. Источник сканирования перенесён на web/*.py, добавлен
настоящий assert."""
from _paths import _P  # noqa: E402  (корень вычисляется от __file__)
import re
from pathlib import Path

WEB_DIR = Path(_P("web"))
SRC_FILES = sorted(WEB_DIR.glob("*.py"))

# Четыре роута легально не имеют require_csrf_*:
#   /login  — до входа нет сессии, а значит и username, из которого
#             выводится CSRF-токен (см. require_csrf_auth); защищён
#             отдельно rate-limit'ом по IP (web/auth.py).
#   /logout — вообще без require_auth (работает и без cookie), максимум
#             вреда от подделки — принудительный логаут, не изменение
#             данных; вектор уже закрыт POST + SameSite=lax (см. docstring
#             logout() в web/auth.py).
#   /api/unban, /api/reset-automute-count — по дизайну v4.8.8 CSRF-проверкой
#             не покрываются JSON /api/* роуты (changelog v4.8.8 в
#             templates/base.html: «каждый POST-роут, кроме /login и /api/*»).
EXCLUDE = {"/login", "/logout", "/api/unban", "/api/reset-automute-count"}

issues = []

for src in SRC_FILES:
    text = src.read_text(encoding="utf-8")
    lines = text.split("\n")

    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.search(r'@router\.post\("([^"]+)"', line)
        # Пропускаем совпадения внутри комментариев (# раньше декоратора
        # в строке) — «надгробные» комментарии в web_app.py именно так и
        # выглядели, тот же риск учитываем и здесь на будущее.
        if m and "#" in line[: m.start()]:
            m = None
        if m:
            path = m.group(1)
            # Ищем сигнатуру функции вплоть до закрывающей скобки `):`
            j = i
            sig = ""
            while j < min(i + 50, len(lines)):
                sig += lines[j] + "\n"
                if re.search(r'\):\s*$', lines[j].rstrip()) and j > i:
                    break
                j += 1
            if path not in EXCLUDE:
                if "Depends(require_auth)" in sig or \
                   "Depends(require_su)" in sig or \
                   "Depends(require_admin)" in sig:
                    issues.append((src.name, i + 1, path, "осталась старая зависимость"))
                elif "Depends(require_csrf_" not in sig:
                    issues.append((src.name, i + 1, path, "нет CSRF зависимости вообще"))
        i += 1

if not issues:
    print("OK: все POST-роуты (кроме excluded) имеют require_csrf_* зависимость")
else:
    print(f"PROBLEMS: {len(issues)}")
    for fname, ln, path, msg in issues:
        print(f"  {fname}:L{ln} {path}: {msg}")

assert not issues, (
    "найдены POST-роуты без require_csrf_*: "
    + ", ".join(f"{fname}:L{ln} {path} ({msg})" for fname, ln, path, msg in issues)
)
