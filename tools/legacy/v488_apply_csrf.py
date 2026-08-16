#!/usr/bin/env python3
"""v4.8.8: заменить require_auth/require_admin/require_su на require_csrf_*
в POST-роутах. GET-роуты не трогаем.

Исключения (CSRF не нужен):
  /login      — нет сессии до логина
  /logout     — скрытая форма, CSRF желателен но не критичен в v4.8.8
  /api/*      — вызываются через fetch, CSRF через X-CSRF-Token header работает
                (require_csrf_auth тоже поддерживает header)
"""
import re
import sys
from pathlib import Path

SRC = Path("/home/z/my-project/v488_work/web_app.py")
EXCLUDE = {"/login", "/logout", "/api/unban", "/api/reset-automute-count"}

text = SRC.read_text(encoding="utf-8")
lines = text.split("\n")

# Найти все @app.post("...") декораторы и их индексы
post_routes = []  # list of (line_idx, route_path)
for i, ln in enumerate(lines):
    m = re.match(r'\s*@app\.post\("([^"]+)"', ln)
    if m:
        post_routes.append((i, m.group(1)))

print(f"Найдено POST-роутов: {len(post_routes)}")
for _, path in post_routes:
    marker = " [EXCLUDED]" if path in EXCLUDE else ""
    print(f"  {path}{marker}")

# Для каждого POST-роута (не excluded) — найти строку с Depends(require_*)
# в пределах 30 строк после декоратора и заменить
replacements = 0
for line_idx, path in post_routes:
    if path in EXCLUDE:
        continue
    # Ищем Depends(require_auth) / Depends(require_su) / Depends(require_admin)
    # в сигнатуре функции — обычно в пределах 20 строк после декоратора.
    # Заменяем только первое вхождение (сигнатура функции).
    replaced = False
    for j in range(line_idx, min(line_idx + 30, len(lines))):
        ln = lines[j]
        # Замена только в строке с Depends — не трогаем комментарии или другие упоминания
        if "Depends(require_auth)" in ln:
            lines[j] = ln.replace("Depends(require_auth)", "Depends(require_csrf_auth)")
            replaced = True
            replacements += 1
            print(f"  L{j+1} {path}: require_auth → require_csrf_auth")
            break
        if "Depends(require_su)" in ln:
            lines[j] = ln.replace("Depends(require_su)", "Depends(require_csrf_su)")
            replaced = True
            replacements += 1
            print(f"  L{j+1} {path}: require_su → require_csrf_su")
            break
        if "Depends(require_admin)" in ln:
            lines[j] = ln.replace("Depends(require_admin)", "Depends(require_csrf_admin)")
            replaced = True
            replacements += 1
            print(f"  L{j+1} {path}: require_admin → require_csrf_admin")
            break
    if not replaced:
        print(f"  ! {path}: не нашёл Depends(require_*) в 30 строках после декоратора", file=sys.stderr)

print(f"\nВсего замен: {replacements}")

# Записать обратно
SRC.write_text("\n".join(lines), encoding="utf-8")
print(f"Файл обновлён: {SRC}")
