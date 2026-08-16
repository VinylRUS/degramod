#!/usr/bin/env python3
"""v4.8.8: добавить {{ csrf_field() }} во все POST-формы в шаблонах.

Для каждой <form method="post" ...> добавляем скрытое поле csrf_token
сразу после открывающего тега <form ...>.

GET-формы не трогаем.
"""
import re
import sys
from pathlib import Path

TEMPLATES_DIR = Path("/home/z/my-project/v488_work/templates")

# Regex: <form ... method="post" ...> или <form ... method='post' ...>
# Затем любое количество атрибутов и закрывающий >
# Срабатывает на одну строку (все формы в проекте в одну строку).
FORM_OPEN_RE = re.compile(
    r'(<form\b[^>]*\bmethod=["\']post["\'][^>]*>)',
    re.IGNORECASE,
)

# Не трогаем формы с уже добавленным csrf_field (idempotent)
HAS_CSRF_RE = re.compile(r'csrf_field\s*\(\s*\)', re.IGNORECASE)

total_patched = 0
files_modified = []

for tpl_path in sorted(TEMPLATES_DIR.glob("*.html")):
    src = tpl_path.read_text(encoding="utf-8")
    matches = list(FORM_OPEN_RE.finditer(src))
    if not matches:
        continue
    
    # Найдём только те, где ещё нет csrf_field в этой форме
    # Простой подход: для каждого match смотрим следующие ~200 символов
    out = []
    last_end = 0
    patched_in_file = 0
    for m in matches:
        form_open = m.group(1)
        # Проверяем есть ли csrf_field() уже в пределах этой формы (до </form>)
        form_end_idx = src.find("</form>", m.end())
        if form_end_idx == -1:
            form_end_idx = min(m.end() + 500, len(src))
        form_body = src[m.end():form_end_idx]
        if HAS_CSRF_RE.search(form_body):
            # Уже есть — пропускаем
            continue
        # Добавляем сразу после <form ...>
        # Если form_open заканчивается на >, добавляем после >
        out.append(src[last_end:m.end()])
        # Учитываем перенос строки если form_open заканчивается им
        insertion = f"\n        {{% raw %}}{{% endraw %}}{{ csrf_field() }}"
        # Упрощаем: просто вставляем csrf_field() на новой строке
        insertion = "\n            {{ csrf_field() }}"
        out.append(insertion)
        last_end = m.end()
        patched_in_file += 1
    
    if patched_in_file == 0:
        continue
    
    out.append(src[last_end:])
    new_src = "".join(out)
    
    # Проверка синтаксиса Jinja: простой smoke-check, что количество { и } совпадает
    open_braces = new_src.count("{{")
    close_braces = new_src.count("}}")
    if open_braces != close_braces:
        print(f"  ! WARNING: {tpl_path.name}: {open_braces} {{ vs {close_braces} }}", file=sys.stderr)
    
    tpl_path.write_text(new_src, encoding="utf-8")
    print(f"  {tpl_path.name}: +{patched_in_file} форм пропатчено")
    files_modified.append(tpl_path.name)
    total_patched += patched_in_file

print(f"\nВсего: {total_patched} форм пропатчено в {len(files_modified)} файлах")
print(f"Файлы: {', '.join(files_modified)}")
