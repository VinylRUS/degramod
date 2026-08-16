#!/usr/bin/env python3
"""v4.8.8 verify: найти POST-роуты где осталась старая зависимость без CSRF."""
import re
from pathlib import Path

SRC = Path("/home/z/my-project/v488_work/web_app.py")
text = SRC.read_text(encoding="utf-8")
lines = text.split("\n")

# Найти все @app.post("...") декораторы и проверить их зависимости
EXCLUDE = {"/login", "/logout", "/api/unban", "/api/reset-automute-count"}
issues = []

i = 0
while i < len(lines):
    m = re.match(r'\s*@app\.post\("([^"]+)"', lines[i])
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
                issues.append((i + 1, path, "осталась старая зависимость"))
            elif "Depends(require_csrf_" not in sig:
                issues.append((i + 1, path, "нет CSRF зависимости вообще"))
    i += 1

if not issues:
    print("OK: все POST-роуты (кроме excluded) имеют require_csrf_* зависимость")
else:
    print(f"PROBLEMS: {len(issues)}")
    for ln, path, msg in issues:
        print(f"  L{ln} {path}: {msg}")
