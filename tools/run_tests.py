#!/usr/bin/env python3
"""Прогон сюиты: каждый файл — в своём процессе.

Зачем не просто `pytest tests`:

Сюита писалась как набор самостоятельных скриптов (`python test_x.py`), и
каждый файл на уровне модуля поднимает своё окружение — свой DB_PATH, свой
WEB_PASSWORD, свой импорт `bot`. В одном процессе это конфликтует:

  - aiogram: повторный `dp.include_router(...)` → RuntimeError: Router is
    already attached;
  - web_app читает WEB_PASSWORD при импорте, поэтому пароль замораживается
    тем файлом, который импортировал модуль первым;
  - db.py так же фиксирует DB_PATH.

Разделение по процессам сохраняет исходную семантику файлов и убирает
конфликты. Когда сюиту доведут до общих фикстур, этот раннер станет не нужен
и его место займёт обычный `pytest tests`.

Запуск:
    uv run python tools/run_tests.py            # всё
    uv run python tools/run_tests.py -k slowmode  # подмножество по подстроке
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
TIMEOUT_SECONDS = 120

# Файлы, временно выведенные из прогона. Сейчас список пуст — все 63 файла
# зелёные. Механизм оставлен на случай, если файл придётся отложить: пока он
# в списке, его падение не роняет сборку, но как только начинает проходить,
# раннер требует убрать строку, чтобы список не превращался в свалку.
BASELINE = ROOT / "tests" / "known_failing.txt"


def _known_failing() -> set[str]:
    if not BASELINE.exists():
        return set()
    out = set()
    for line in BASELINE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-k", dest="filter", default="", help="подстрока в имени файла")
    ap.add_argument("-v", dest="verbose", action="store_true", help="показывать вывод упавших")
    args = ap.parse_args()

    files = sorted(p for p in TESTS.glob("test_*.py") if args.filter in p.name)
    if not files:
        print(f"Не найдено файлов по фильтру {args.filter!r}", file=sys.stderr)
        return 2

    baseline = _known_failing()
    passed: list[str] = []
    failed: list[tuple[str, str]] = []
    expected_fail: list[str] = []
    fixed: list[str] = []
    started = time.monotonic()

    for i, path in enumerate(files, 1):
        rel = path.relative_to(ROOT)
        print(f"[{i:>2}/{len(files)}] {path.name:<52}", end="", flush=True)
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", str(rel), "-q", "--no-header", "-p", "no:cacheprovider"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
            )
            out = proc.stdout + proc.stderr
            # rc=5 — pytest не нашёл ни одной test-функции. Для скриптовых
            # файлов сюиты это успех: все проверки у них на уровне модуля,
            # то есть модуль импортировался и ни один assert не сработал.
            # Ошибка импорта дала бы rc=1/2, а не 5.
            ok = proc.returncode in (0, 5)
            script_style = proc.returncode == 5
        except subprocess.TimeoutExpired:
            out, ok, script_style = f"таймаут {TIMEOUT_SECONDS}s", False, False

        known = path.name in baseline
        if ok:
            passed.append(path.name)
            if known:
                fixed.append(path.name)
                print("PASS — уберите из known_failing.txt")
            else:
                print("PASS (модульные проверки)" if script_style else "PASS")
        elif known:
            expected_fail.append(path.name)
            print("XFAIL (в known_failing.txt)")
        else:
            failed.append((path.name, out))
            print("FAIL")

    elapsed = time.monotonic() - started
    print()
    print("=" * 68)
    print(
        f"Файлов: {len(files)}   PASS: {len(passed)}   "
        f"XFAIL: {len(expected_fail)}   FAIL: {len(failed)}   {elapsed:.1f}s"
    )
    print("=" * 68)

    if failed:
        print("\nУпали (нет в known_failing.txt):")
        for name, out in failed:
            last = [ln for ln in out.strip().split("\n") if ln.strip()]
            reason = last[-1] if last else "?"
            print(f"  - {name}\n      {reason[:120]}")
            if args.verbose:
                print("\n".join("      " + ln for ln in last[-25:]))
                print()

    if fixed:
        print("\nПроходят, хотя записаны в known_failing.txt — удалите строки:")
        for name in fixed:
            print(f"  - {name}")

    # Красным прогон делают только неожиданные падения и починившиеся файлы,
    # оставшиеся в baseline: и то и другое требует правки списка.
    return 1 if (failed or fixed) else 0


if __name__ == "__main__":
    sys.exit(main())
