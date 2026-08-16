#!/usr/bin/env python3
"""v4.8.3.1 — диагностика regex-ов _CMD_SMUTE / _CMD_SWARN / _CMD_SBAN.

Цель: понять, какие варианты вызова !smute/!swarn/!sban матчатся,
а какие — нет, и какие группы (target/dur/reason) при этом получаются.
"""
import re
import sys

# Точные копии regex-ов из v4.8.3 bot_handlers.py
_CMD_SMUTE = re.compile(
    r"^!smute(?:\s+(?:(?P<target>@\w+|\d+)\s+)?(?P<dur>\d+[a-zа-я]+)(?:\s+(?P<reason>.+))?)?$",
    re.IGNORECASE,
)
_CMD_SWARN = re.compile(
    r"^!swarn(?:\s+(?:(?P<target>@\w+|\d+))?(?:\s+(?P<reason>.+))?)?$",
    re.IGNORECASE,
)
_CMD_SBAN = re.compile(
    r"^!sban(?:\s+(?:(?P<target>@\w+|\d+))?(?:\s+(?P<reason>.+))?)?$",
    re.IGNORECASE,
)


def _show(label: str, pat: re.Pattern, text: str) -> None:
    m = pat.match(text)
    if m is None:
        print(f"  [{label}] NO MATCH: {text!r}")
        return
    gd = m.groupdict()
    print(f"  [{label}] OK: {text!r}  → target={gd.get('target')!r} "
          f"dur={gd.get('dur')!r} reason={gd.get('reason')!r}")


def main() -> int:
    print("=== _CMD_SMUTE ===")
    for t in [
        "!smute",
        "!smute 1d",
        "!smute 1d Причина",
        "!smute @user 1d",
        "!smute @user 1d Причина",
        "!smute 12345 1d",
        "!smute 1д",
        "!smute 1д Причина",
        "!smute 30m",
    ]:
        _show("SMUTE", _CMD_SMUTE, t)

    print("\n=== _CMD_SWARN ===")
    for t in [
        "!swarn",
        "!swarn Причина",
        "!swarn @user",
        "!swarn @user Причина",
        "!swarn 12345",
        "!swarn 12345 Причина",
    ]:
        _show("SWARN", _CMD_SWARN, t)

    print("\n=== _CMD_SBAN ===")
    for t in [
        "!sban",
        "!sban Причина",
        "!sban @user",
        "!sban @user Причина",
        "!sban 12345",
        "!sban 12345 Причина",
    ]:
        _show("SBAN", _CMD_SBAN, t)

    # Симулируем вызов _parse_duration(None)
    print("\n=== _parse_duration(None) simulation ===")
    try:
        text = None
        _ = text.strip()
        print("  UNEXPECTED: no AttributeError raised")
    except AttributeError as e:
        print(f"  AttributeError raised as expected: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
