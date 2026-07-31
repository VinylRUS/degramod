"""
test_v473_nested_forms_bug.py — Reproduce nested forms bug in admin_chats.html.

BUG: Главная форма /admin/chats/{id}/update содержит ВНУТРИ себя несколько
других <form> для toggle-кнопок (Enabled, CAS, Link-filter, Night mode toggle,
Sanitary days toggle, sync-admins). HTML запрещает вложённые <form> — браузер
автозакрывает внешнюю форму перед первой вложенной. В итоге:
- /update форма реально содержит только поля ДО первого toggle (никаких).
- Все поля (hashtag, warns_to_mute, night_mode_start, sanitary_days_text, etc.)
  оказываются ВНЕ формы.
- При нажатии «Сохранить» сабмитится пустая форма — ничего не сохраняется.

FIX: Вынести все inline toggle <form> за пределы главной /update <form>.
Использовать button + JS (fetch POST) или располагать toggle-формы рядом,
но не внутри update-формы.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, "/home/z/my-project/v4.5")
os.chdir("/home/z/my-project/v4.5")


class TestNestedFormsBug(unittest.TestCase):
    """Static check: исходник admin_chats.html НЕ должен содержать вложённые <form>."""

    def setUp(self):
        with open("templates/admin_chats.html", "r", encoding="utf-8") as f:
            self.src = f.read()

    def test_update_form_has_no_nested_forms_inside(self):
        """Главная /update форма НЕ должна содержать <form> внутри себя.

        Парсим html простой stack-машиной (html.parser не подходит — он
        НЕ закрывает form автоматически как браузер; мы симулируем поведение
        браузера: <form> внутри <form> немедленно закрывает родителя).

        ВНИМАНИЕ: regex исключает <form> внутри Jinja-комментариев {# ... #}.
        Они не попадают в HTML output, но regex их ловит в source.
        """
        # Уберём Jinja-комментарии {# ... #} перед анализом
        src_no_comments = re.sub(r'\{#.*?#\}', '', self.src, flags=re.DOTALL)

        # Найдём все <form> открывающие теги с их позицией и action
        form_opens = []
        for m in re.finditer(r'<form\b[^>]*>', src_no_comments):
            tag = m.group(0)
            action_m = re.search(r'action="([^"]+)"', tag)
            action = action_m.group(1) if action_m else "(none)"
            form_opens.append((m.start(), action))

        # Найдём все </form>
        form_closes = [m.start() for m in re.finditer(r'</form>', src_no_comments)]
        self.assertEqual(len(form_opens), len(form_closes),
                         f"Unbalanced: {len(form_opens)} <form> vs {len(form_closes)} </form>")

        # Симулируем браузерный парсинг: при встрече <form> внутри уже открытого <form>
        # браузер закрывает родитель.
        stack = []  # список (start_pos, action)
        nested_violations = []
        events = []
        for pos, action in form_opens:
            events.append(("open", pos, action))
        for pos in form_closes:
            events.append(("close", pos, None))
        events.sort(key=lambda e: e[1])

        for kind, pos, action in events:
            if kind == "open":
                if stack:
                    # Вложенная форма! Браузер закрывает родителя.
                    parent_pos, parent_action = stack.pop()
                    nested_violations.append((parent_pos, parent_action, pos, action))
                stack.append((pos, action))
            else:  # close
                if stack:
                    stack.pop()

        # Должно быть 0 вложённых форм
        self.assertEqual(len(nested_violations), 0,
                         f"Nested <form> detected! Browser will auto-close outer form.\n"
                         f"Violations (parent_pos, parent_action, nested_pos, nested_action):\n" +
                         "\n".join(f"  outer <form action={pa!r}> @ {pp} contains "
                                   f"<form action={na!r}> @ {np_}"
                                   for pp, pa, np_, na in nested_violations))

    def test_hashtag_field_is_inside_update_form(self):
        """Поле name="hashtag" должно быть внутри /update формы (не после неё)."""
        # Уберём Jinja-комментарии — они не попадают в HTML output
        src = re.sub(r'\{#.*?#\}', '', self.src, flags=re.DOTALL)

        # Найти /update форму и её конец (с учётом браузерного авто-закрытия)
        update_open_m = re.search(r'<form\b[^>]*action="/admin/chats/\{\{[^}]+\}\}/update"[^>]*>', src)
        self.assertIsNotNone(update_open_m, "Update form not found in template")

        # Браузерный конец: ближайший <form> ИЛИ </form> после update_open
        update_start = update_open_m.end()
        next_form = re.search(r'<form\b', src[update_start:])
        next_close = re.search(r'</form>', src[update_start:])

        if next_form and (not next_close or next_form.start() < next_close.start()):
            # Браузер закроет /update на позиции next_form
            browser_end = update_start + next_form.start()
        else:
            browser_end = update_start + (next_close.start() if next_close else 0)

        # Найдём поле hashtag
        hashtag_m = re.search(r'name="hashtag"', src)
        self.assertIsNotNone(hashtag_m, "hashtag field not found")

        hashtag_pos = hashtag_m.start()
        self.assertGreater(hashtag_pos, update_open_m.start(),
                           "hashtag field must be after /update form open")
        self.assertLess(hashtag_pos, browser_end,
                        f"hashtag field @ {hashtag_pos} is OUTSIDE /update form "
                        f"(browser-closed @ {browser_end}) — won't be submitted!")


if __name__ == "__main__":
    unittest.main(verbosity=2)
