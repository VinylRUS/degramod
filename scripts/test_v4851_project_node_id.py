#!/usr/bin/env python3
"""
test_v4851_project_node_id.py — тесты для hotfix v4.8.5.1.

Проверяет переписанный github_client.get_project_node_id() на:
  1. User-аккаунт с валидным Project → возвращает PVT_xxx.
  2. Organization с валидным Project → возвращает PVT_xxx.
  3. Login не существует ни как user, ни как org → понятная ошибка.
  4. User существует, но Project не найден → ошибка с подсказкой.
  5. Organization существует, но Project не найден → ошибка с подсказкой.
  6. PAT без scope project → GraphQL error пробрасывается.
  7. APP_VERSION в web_app.py = v4.8.5.1.
  8. Changelog v4.8.5.1 присутствует в base.html.
  9. Описание бага "Could not resolve to an Organization" упомянуто в changelog.

Мокаем github_client._graphql, чтобы не ходить в реальный GitHub API.
"""

import ast
import asyncio
import importlib.util
import sys
from pathlib import Path

# ── Пути ────────────────────────────────────────────────────────────────
V485_DIR = Path("/home/z/my-project/v485_work")
sys.path.insert(0, str(V485_DIR))

passed = 0
failed = 0
checks = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        checks.append(f"  ✓ {name}")
    else:
        failed += 1
        checks.append(f"  ✗ {name}  {detail}")


# ── Загружаем github_client ─────────────────────────────────────────────
spec = importlib.util.spec_from_file_location(
    "github_client", V485_DIR / "github_client.py",
)
gc = importlib.util.module_from_spec(spec)
sys.modules["github_client"] = gc  # required for @dataclass to resolve module
spec.loader.exec_module(gc)


# ── Хелпер: создаём модуль с моком _graphql ────────────────────────────
def make_gc_with_mock(mock_responses):
    """
    mock_responses: list of dicts. Каждый элемент — ответ на ОДИН вызов
    _graphql. Может содержать:
      - {"return": data}  → вернуть data
      - {"raise": GithubApiError("...")}  → кинуть ошибку
    """
    call_count = [0]

    async def mock_graphql(pat, query, variables=None):
        idx = call_count[0]
        call_count[0] += 1
        if idx >= len(mock_responses):
            raise AssertionError(
                f"Mock _graphql called {idx+1} times, but only "
                f"{len(mock_responses)} responses configured. "
                f"Last query variables: {variables}"
            )
        r = mock_responses[idx]
        if "raise" in r:
            raise r["raise"]
        return r["return"]

    # Перезаписываем _graphql в модуле
    gc._graphql = mock_graphql
    return call_count


# ── T1: User-аккаунт с валидным Project ─────────────────────────────────
async def t1_user_ok():
    """Login — это user, Project существует. Должен вернуть PVT_xxx."""
    make_gc_with_mock([
        # Шаг 1: user query — успешно
        {"return": {
            "user": {
                "projectV2": {
                    "id": "PVT_kwHOabcd1234",
                    "title": "Ded Vobzhak Ideas",
                }
            }
        }},
    ])
    result = await gc.get_project_node_id(
        pat="fake_pat", owner_login="VinylRUS", project_number=3,
    )
    check("T1: user ok returns PVT_xxx",
          result == "PVT_kwHOabcd1234",
          f"got {result!r}")


# ── T2: Organization с валидным Project ─────────────────────────────────
async def t2_org_ok():
    """Login — это organization, Project существует."""
    make_gc_with_mock([
        # Шаг 1: user query — падает "Could not resolve to a User"
        {"raise": gc.GithubApiError(
            "GraphQL errors: Could not resolve to a User with the login of 'some-org'.",
            status=200,
        )},
        # Шаг 2: org query — успешно
        {"return": {
            "organization": {
                "projectV2": {
                    "id": "PVT_kwHOxyz9876",
                    "title": "Org Board",
                }
            }
        }},
    ])
    result = await gc.get_project_node_id(
        pat="fake_pat", owner_login="some-org", project_number=1,
    )
    check("T2: org ok returns PVT_xxx",
          result == "PVT_kwHOxyz9876",
          f"got {result!r}")


# ── T3: Login не существует ни как user, ни как org ─────────────────────
async def t3_neither_exists():
    """Login вообще не существует — должна быть понятная ошибка."""
    make_gc_with_mock([
        # Шаг 1: user query — "Could not resolve to a User"
        {"raise": gc.GithubApiError(
            "GraphQL errors: Could not resolve to a User with the login of 'ghost123'.",
            status=200,
        )},
        # Шаг 2: org query — "Could not resolve to an Organization"
        {"raise": gc.GithubApiError(
            "GraphQL errors: Could not resolve to an Organization with the login of 'ghost123'.",
            status=200,
        )},
    ])
    try:
        await gc.get_project_node_id(
            pat="fake_pat", owner_login="ghost123", project_number=1,
        )
        check("T3: neither exists raises", False, "no exception raised")
    except gc.GithubApiError as e:
        msg = str(e)
        ok = "neither" in msg.lower() and "ghost123" in msg
        check("T3: neither exists raises with clear msg", ok, f"msg={msg!r}")


# ── T4: User существует, но Project не найден ───────────────────────────
async def t4_user_no_project():
    """User есть, но Project с таким номером не существует."""
    make_gc_with_mock([
        # Шаг 1: user есть, но projectV2 = null
        {"return": {"user": {"projectV2": None}}},
    ])
    try:
        await gc.get_project_node_id(
            pat="fake_pat", owner_login="VinylRUS", project_number=999,
        )
        check("T4: user no project raises", False, "no exception")
    except gc.GithubApiError as e:
        msg = str(e)
        ok = "not found for user" in msg.lower() and "999" in msg
        check("T4: user no project raises with hint",
              ok, f"msg={msg!r}")


# ── T5: Organization существует, но Project не найден ───────────────────
async def t5_org_no_project():
    """Organization есть, но Project не найден."""
    make_gc_with_mock([
        # Шаг 1: user query — не user
        {"raise": gc.GithubApiError(
            "GraphQL errors: Could not resolve to a User with the login of 'myorg'.",
            status=200,
        )},
        # Шаг 2: org есть, но projectV2 = null
        {"return": {"organization": {"projectV2": None}}},
    ])
    try:
        await gc.get_project_node_id(
            pat="fake_pat", owner_login="myorg", project_number=999,
        )
        check("T5: org no project raises", False, "no exception")
    except gc.GithubApiError as e:
        msg = str(e)
        ok = "not found for organization" in msg.lower() and "999" in msg
        check("T5: org no project raises with hint",
              ok, f"msg={msg!r}")


# ── T6: GraphQL error, не связанная с типом owner ──────────────────────
async def t6_unrelated_graphql_error():
    """PAT без scope project → GraphQL error пробрасывается как есть."""
    make_gc_with_mock([
        # Шаг 1: user query падает с ошибкой про права
        {"raise": gc.GithubApiError(
            "GraphQL errors: Your token has not been granted the scopes required by this query.",
            status=200,
        )},
    ])
    try:
        await gc.get_project_node_id(
            pat="fake_pat", owner_login="anyuser", project_number=1,
        )
        check("T6: unrelated error raises", False, "no exception")
    except gc.GithubApiError as e:
        msg = str(e)
        ok = "scopes" in msg.lower() or "token" in msg.lower()
        check("T6: unrelated error propagated as-is", ok, f"msg={msg!r}")


# ── T7: APP_VERSION = v4.8.5+ в web_app.py ──────────────────────────────
def t7_app_version():
    import re as _re
    src = (V485_DIR / "web_app.py").read_text(encoding="utf-8")
    # v4.8.6: принимаем v4.8.5+ (включая v4.8.6, v4.9.0 и т.д.)
    m = _re.search(r'APP_VERSION\s*=\s*"v(\d+)\.(\d+)\.(\d+[^"]*)"', src)
    if m:
        major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
        ok = (major, minor, patch) >= (4, 8, 5)
        ver_str = f"v{m.group(1)}.{m.group(2)}.{m.group(3)}"
    else:
        ok = False
        ver_str = None
    check(f"T7: APP_VERSION = v4.8.5+ (got {ver_str!r})", ok)


# ── T8: Changelog v4.8.5.1 в base.html ──────────────────────────────────
def t8_changelog():
    src = (V485_DIR / "templates" / "base.html").read_text(encoding="utf-8")
    ok1 = "v4.8.5.1" in src
    ok2 = "hotfix" in src.lower()
    ok3 = "Could not resolve to an Organization" in src
    check("T8: changelog v4.8.5.1 present", ok1)
    check("T8a: changelog mentions hotfix", ok2)
    check("T8b: changelog mentions original error", ok3)


# ── T9: Синтаксис всех 3 изменённых файлов ─────────────────────────────
def t9_syntax():
    for fname in ("github_client.py", "web_app.py"):
        try:
            ast.parse((V485_DIR / fname).read_text(encoding="utf-8"))
            check(f"T9: {fname} syntax OK", True)
        except SyntaxError as e:
            check(f"T9: {fname} syntax OK", False, str(e))


# ── T10: get_project_node_id — корректный сигнатурный API ──────────────
async def t10_api_signature():
    """API функции не изменился — принимает (pat, owner_login, project_number)."""
    import inspect
    sig = inspect.signature(gc.get_project_node_id)
    params = list(sig.parameters.keys())
    ok = params == ["pat", "owner_login", "project_number"]
    check("T10: API signature preserved", ok, f"params={params}")


# ── T11: User с projectV2=null (не None) → переходит к org? ─────────────
async def t11_user_null_branch():
    """Если user существует, но projectV2=None — НЕ переходим к org.

    Логин не может быть одновременно user и org — нет смысла пробовать.
    """
    call_count = make_gc_with_mock([
        {"return": {"user": {"projectV2": None}}},
        # Если бы попытались org — был бы второй вызов. Не должно быть.
    ])
    try:
        await gc.get_project_node_id(
            pat="fake_pat", owner_login="realuser", project_number=42,
        )
        check("T11: user null raises", False, "no exception")
    except gc.GithubApiError:
        check("T11: user null raises", True)
    # Должен быть ровно 1 вызов _graphql (только user).
    check("T11: only 1 _graphql call (no org fallback for user)",
          call_count[0] == 1, f"calls={call_count[0]}")


# ── T12: Реальный кейс пользователя — VinylRUS / org path ───────────────
async def t12_vinylrus_case():
    """Воспроизводит кейс из баг-репорта:
    VinylRUS — это organization (или user, не важно), но ошибка была
    'Could not resolve to an Organization'. Это значит, что в старом коде
    GraphQL упал именно на organization branch. Если VinylRUS — org, то
    наш новый код должен сначала попробовать user (упадёт), потом org
    (успешно), и вернуть PVT_xxx.
    """
    make_gc_with_mock([
        # Шаг 1: user query — VinylRUS это не user
        {"raise": gc.GithubApiError(
            "GraphQL errors: Could not resolve to a User with the login of 'VinylRUS'.",
            status=200,
        )},
        # Шаг 2: org query — VinylRUS это org, Project #3 существует
        {"return": {
            "organization": {
                "projectV2": {
                    "id": "PVT_kwHOVinylRUS3",
                    "title": "VinylRUS Ideas Board",
                }
            }
        }},
    ])
    result = await gc.get_project_node_id(
        pat="fake_pat", owner_login="VinylRUS", project_number=3,
    )
    check("T12: VinylRUS org case → PVT_xxx",
          result == "PVT_kwHOVinylRUS3", f"got {result!r}")


# ── T13: Реальный кейс — VinylRUS это user ──────────────────────────────
async def t13_vinylrus_user_case():
    """Альтернатива: VinylRUS — это user (не org). Тогда шаг 1 должен
    сразу вернуть PVT_xxx без шага 2.
    """
    call_count = make_gc_with_mock([
        {"return": {
            "user": {
                "projectV2": {
                    "id": "PVT_kwHOVinylUser3",
                    "title": "My Personal Ideas",
                }
            }
        }},
    ])
    result = await gc.get_project_node_id(
        pat="fake_pat", owner_login="VinylRUS", project_number=3,
    )
    check("T13: VinylRUS user case → PVT_xxx",
          result == "PVT_kwHOVinylUser3", f"got {result!r}")
    check("T13a: only 1 _graphql call (user is enough)",
          call_count[0] == 1, f"calls={call_count[0]}")


# ── Запуск ──────────────────────────────────────────────────────────────
async def main():
    print("=" * 70)
    print("test_v4851_project_node_id.py — hotfix v4.8.5.1 tests")
    print("=" * 70)

    await t1_user_ok()
    await t2_org_ok()
    await t3_neither_exists()
    await t4_user_no_project()
    await t5_org_no_project()
    await t6_unrelated_graphql_error()
    t7_app_version()
    t8_changelog()
    t9_syntax()
    await t10_api_signature()
    await t11_user_null_branch()
    await t12_vinylrus_case()
    await t13_vinylrus_user_case()

    print()
    for c in checks:
        print(c)
    print()
    print(f"Total: {passed} passed, {failed} failed, {passed+failed} checks")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
