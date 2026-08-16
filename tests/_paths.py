"""Пути к файлам проекта, вычисляемые от расположения самих тестов.

Сюита пришла из внешней песочницы, где корень проекта был прошит абсолютным
путём (`/home/z/my-project/v4.5`, `.../v485_work`, `.../v487_work`,
`.../v488_work` — четыре разных каталога в разных файлах). Из-за этого 39 из 65
файлов не запускались нигде, кроме той машины.

Теперь корень берётся от `__file__`, поэтому сюита работает из любого клона,
в контейнере и в CI. Ничего прошитого здесь быть не должно.
"""
from __future__ import annotations

from pathlib import Path

# tests/_paths.py → tests/ → корень репозитория
ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"


def _P(rel: str = "") -> str:
    """Абсолютный путь к файлу проекта. Без аргумента — корень репозитория.

    Заменяет прошитые литералы вида "/home/z/my-project/v4.5/web_app.py":
        Path(_P("web_app.py"))
        sys.path.insert(0, _P())
    """
    return str(ROOT / rel) if rel else str(ROOT)
