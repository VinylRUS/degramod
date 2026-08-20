#!/usr/bin/env python3
"""
test_v4853_status_field.py — тесты для v4.8.5.3 (auto-set Status='Предложено').

Проверяет:
  1. github_client.find_status_field() — корректный парсинг GraphQL response.
  2. github_client.set_item_status() — корректный mutation payload.
  3. github_client.set_item_status_by_name() — happy path (field+option found).
  4. set_item_status_by_name — Status field not found → False + no exception.
  5. set_item_status_by_name — option name not found → False + no exception.
  6. set_item_status_by_name — GraphQL error → False + no exception.
  7. set_item_status_by_name — default status_name = 'Предложено'.
  8. bot_handlers.py — вызов set_item_status_by_name после add_issue_to_project.
  9. db.py — колонка project_status_option_name в GithubSettings.
  10. db.py — миграция для project_status_option_name.
  11. web_app.py — APP_VERSION = v4.8.5.3.
  12. web/admin_settings.py — GET/POST /admin/settings/github принимают project_status_option_name.
  13. admin_settings.html — поле Status option name присутствует.
  14. base.html — changelog v4.8.5.3.
  15. Синтаксис всех 5 изменённых файлов.
"""
from _paths import _P  # noqa: E402  (корень вычисляется от __file__)

import ast
import asyncio
import importlib.util
import re
import sys
from pathlib import Path

V485_DIR = Path(_P())
sys.path.insert(0, str(V485_DIR))

passed = 0
failed = 0
checks = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """Проверка с настоящим падением.

    v4.10.1 (Task 18): раньше провал только увеличивал счётчик, а выход с
    ошибкой стоял в main() под `if __name__`. Под pytest main() не
    вызывается, поэтому файл был зелёным независимо от результата проверок.
    Теперь провал бросает AssertionError — падает и в pytest, и при прямом
    запуске.
    """
    global passed, failed
    if ok:
        passed += 1
        checks.append(f"  ✓ {name}")
    else:
        failed += 1
        checks.append(f"  ✗ {name}  {detail}")
        raise AssertionError(f"{name} — {detail}" if detail else name)


# ── Загружаем github_client ─────────────────────────────────────────────
spec = importlib.util.spec_from_file_location(
    "github_client", V485_DIR / "github_client.py",
)
gc = importlib.util.module_from_spec(spec)
sys.modules["github_client"] = gc
spec.loader.exec_module(gc)


def make_gc_with_mock(mock_responses):
    """mock_responses: list of {"return": data} | {"raise": GithubApiError}."""
    call_count = [0]

    async def mock_graphql(pat, query, variables=None):
        idx = call_count[0]
        call_count[0] += 1
        if idx >= len(mock_responses):
            raise AssertionError(
                f"Mock _graphql called {idx+1} times, but only "
                f"{len(mock_responses)} configured. vars={variables}"
            )
        r = mock_responses[idx]
        if "raise" in r:
            raise r["raise"]
        return r["return"]

    gc._graphql = mock_graphql
    return call_count


# ── T1: find_status_field — happy path ──────────────────────────────────
async def test_01_find_status_field_ok():
    make_gc_with_mock([
        {"return": {
            "node": {
                "fields": {
                    "nodes": [
                        {"id": "PVTSSF_aaa", "name": "Status",
                         "options": [
                             {"id": "opt1", "name": "Предложено"},
                             {"id": "opt2", "name": "В работе"},
                             {"id": "opt3", "name": "Готово"},
                         ]},
                        {"id": "PVTSSF_bbb", "name": "Priority",
                         "options": [{"id": "p1", "name": "High"}]},
                    ]
                }
            }
        }},
    ])
    result = await gc.find_status_field("pat", "PVT_kwXXX")
    ok = result is not None and result["field_id"] == "PVTSSF_aaa"
    ok = ok and len(result["options"]) == 3
    ok = ok and result["options"][0] == {"id": "opt1", "name": "Предложено"}
    check("T1: find_status_field happy path", ok, f"result={result!r}")


# ── T2: find_status_field — Status not found (renamed/deleted) ──────────
async def test_02_find_status_field_not_found():
    make_gc_with_mock([
        {"return": {
            "node": {
                "fields": {
                    "nodes": [
                        {"id": "PVTSSF_bbb", "name": "Priority",
                         "options": [{"id": "p1", "name": "High"}]},
                    ]
                }
            }
        }},
    ])
    result = await gc.find_status_field("pat", "PVT_kwXXX")
    check("T2: find_status_field returns None when no Status", result is None)


# ── T3: find_status_field — node is None (invalid project_node_id) ──────
async def test_03_find_status_field_node_none():
    make_gc_with_mock([
        {"return": {"node": None}},
    ])
    result = await gc.find_status_field("pat", "PVT_invalid")
    check("T3: find_status_field returns None when node is None",
          result is None)


# ── T4: set_item_status_by_name — happy path → True ─────────────────────
async def test_04_set_by_name_ok():
    """find_status_field находит Status с option 'Предложено' → set_item_status → True."""
    make_gc_with_mock([
        # Шаг 1: find_status_field
        {"return": {
            "node": {
                "fields": {
                    "nodes": [
                        {"id": "PVTSSF_aaa", "name": "Status",
                         "options": [
                             {"id": "opt1", "name": "Предложено"},
                             {"id": "opt2", "name": "В работе"},
                         ]},
                    ]
                }
            }
        }},
        # Шаг 2: set_item_status mutation
        {"return": {
            "updateProjectV2ItemFieldValue": {
                "projectV2Item": {"id": "PVTI_xxx"}
            }
        }},
    ])
    result = await gc.set_item_status_by_name(
        pat="pat", project_node_id="PVT_kwXXX",
        item_id="PVTI_xxx", status_name="Предложено",
    )
    check("T4: set_item_status_by_name happy path → True",
          result is True, f"result={result!r}")


# ── T5: set_item_status_by_name — Status field missing → False ──────────
async def test_05_status_field_missing():
    make_gc_with_mock([
        {"return": {
            "node": {"fields": {"nodes": [
                {"id": "PVTSSF_bbb", "name": "Priority",
                 "options": [{"id": "p1", "name": "High"}]},
            ]}}
        }},
    ])
    result = await gc.set_item_status_by_name(
        pat="pat", project_node_id="PVT_kwXXX",
        item_id="PVTI_xxx", status_name="Предложено",
    )
    check("T5: Status field missing → False", result is False)


# ── T6: set_item_status_by_name — option not found → False ──────────────
async def test_06_option_not_found():
    make_gc_with_mock([
        {"return": {
            "node": {"fields": {"nodes": [
                {"id": "PVTSSF_aaa", "name": "Status",
                 "options": [{"id": "opt1", "name": "Todo"},
                             {"id": "opt2", "name": "Done"}]},
            ]}}
        }},
    ])
    result = await gc.set_item_status_by_name(
        pat="pat", project_node_id="PVT_kwXXX",
        item_id="PVTI_xxx", status_name="Предложено",
    )
    check("T6: option 'Предложено' not found → False", result is False)


# ── T7: set_item_status_by_name — GraphQL error → False ─────────────────
async def test_07_graphql_error_returns_false():
    """find_status_field падает с GraphQL error — должна вернуть False, не кидать."""
    make_gc_with_mock([
        {"raise": gc.GithubApiError(
            "GraphQL errors: Something went wrong", status=200,
        )},
    ])
    try:
        result = await gc.set_item_status_by_name(
            pat="pat", project_node_id="PVT_kwXXX",
            item_id="PVTI_xxx", status_name="Предложено",
        )
        check("T7: GraphQL error → False (no exception)", result is False)
    except Exception as e:
        check("T7: GraphQL error → False (no exception)", False,
              f"raised: {type(e).__name__}: {e}")


# ── T8: set_item_status_by_name — default status_name = 'Предложено' ────
async def test_08_default_status_name():
    """Если status_name не задан — default 'Предложено'."""
    import inspect
    sig = inspect.signature(gc.set_item_status_by_name)
    default = sig.parameters["status_name"].default
    check("T8: default status_name = 'Предложено'",
          default == "Предложено", f"default={default!r}")


# ── T9: bot_handlers.py — set_item_status_by_name вызывается после add ──
def test_09_bot_handlers_calls_set_status():
    src = (V485_DIR / "bot_handlers.py").read_text(encoding="utf-8")
    ok1 = "set_item_status_by_name" in src
    ok2 = "from github_client import" in src and "set_item_status_by_name" in src
    # После add_issue_to_project должен быть вызов set_item_status_by_name.
    add_pos = src.find("add_issue_to_project")
    set_pos = src.find("set_item_status_by_name")
    ok3 = add_pos > 0 and set_pos > 0 and set_pos > add_pos
    # Проверяем что это в _process_idea_submission.
    proc_pos = src.find("async def _process_idea_submission")
    ok4 = proc_pos > 0 and set_pos > proc_pos
    check("T9: bot_handlers imports set_item_status_by_name", ok1)
    check("T9a: bot_handlers imports it from github_client", ok2)
    check("T9b: call is AFTER add_issue_to_project", ok3)
    check("T9c: call is inside _process_idea_submission", ok4)


# ── T10: db.py — поле project_status_option_name в GithubSettings ───────
def test_10_db_field_present():
    src = (V485_DIR / "db.py").read_text(encoding="utf-8")
    ok = "project_status_option_name = Column(String(128)" in src
    check("T10: db.py has project_status_option_name column", ok)


# ── T11: db.py — миграция для project_status_option_name ────────────────
def test_11_db_migration_present():
    src = (V485_DIR / "db.py").read_text(encoding="utf-8")
    ok1 = 'ALTER TABLE github_settings ADD COLUMN' in src
    ok2 = 'project_status_option_name VARCHAR(128) NULL' in src
    ok3 = "UPDATE github_settings SET project_status_option_name = 'Предложено'" in src
    check("T11: db.py migration — ALTER TABLE", ok1)
    check("T11a: db.py migration — column type", ok2)
    check("T11b: db.py migration — UPDATE default", ok3)


# ── T12: web_app.py — APP_VERSION = v4.8.5+ (>= v4.8.5.3) ────────────────
def test_12_app_version():
    src = (V485_DIR / "web_app.py").read_text(encoding="utf-8")
    # v4.8.6: принимаем v4.8.5+ (включая v4.8.5.x hotfixes, v4.8.6, v4.9.0 и т.д.)
    m = re.search(r'APP_VERSION\s*=\s*"v(\d+)\.(\d+)\.(\d+)', src)
    if m:
        major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
        ok = (major, minor, patch) >= (4, 8, 5)
    else:
        ok = False
    check(f"T12: APP_VERSION = v4.8.5+ (ok={ok})", ok)


# ── T13: web/admin_settings.py — project_status_option_name в GET/POST ──
# v4.9.0 (Task 8): /admin/settings/github* переехал из web_app.py в
# web/admin_settings.py вместе с этим полем.
def test_13_web_app_endpoints():
    src = (V485_DIR / "web" / "admin_settings.py").read_text(encoding="utf-8")
    # GET endpoint должен возвращать это поле.
    ok1 = '"project_status_option_name"' in src
    # POST endpoint должен принимать Form.
    ok2 = "project_status_option_name: str = Form" in src
    # POST endpoint должен сохранять в БД.
    ok3 = "gs.project_status_option_name =" in src
    check("T13: GET returns project_status_option_name", ok1)
    check("T13a: POST accepts Form field", ok2)
    check("T13b: POST saves to gs.project_status_option_name", ok3)


# ── T14: admin_settings.html — поле Status option name ──────────────────
def test_14_admin_settings_field():
    src = (V485_DIR / "templates" / "admin_settings.html").read_text(encoding="utf-8")
    ok1 = 'name="project_status_option_name"' in src
    ok2 = "Status option name" in src
    ok3 = "Предложено" in src  # default value или placeholder
    check("T14: admin_settings.html has input name", ok1)
    check("T14a: admin_settings.html has 'Status option name' label", ok2)
    check("T14b: admin_settings.html mentions 'Предложено' default", ok3)


# ── T15: base.html — changelog v4.8.5.3 ─────────────────────────────────
def test_15_changelog():
    src = (V485_DIR / "templates" / "base.html").read_text(encoding="utf-8")
    ok1 = "v4.8.5.3" in src
    ok2 = "Предложено" in src
    ok3 = "updateProjectV2ItemFieldValue" in src
    ok4 = "find_status_field" in src and "set_item_status" in src
    check("T15: changelog v4.8.5.3 present", ok1)
    check("T15a: mentions 'Предложено'", ok2)
    check("T15b: mentions GraphQL mutation name", ok3)
    check("T15c: mentions new functions", ok4)


# ── T16: синтаксис всех изменённых файлов ───────────────────────────────
def test_16_syntax():
    files = [
        "github_client.py", "web_app.py", "bot_handlers.py", "db.py",
    ]
    for f in files:
        try:
            ast.parse((V485_DIR / f).read_text(encoding="utf-8"))
            check(f"T16: {f} syntax OK", True)
        except SyntaxError as e:
            check(f"T16: {f} syntax OK", False, str(e))


# ── T17: find_status_field — case-sensitive name matching ──────────────
async def test_17_case_sensitive():
    """Имя поля Status case-sensitive. 'status' (lowercase) не должна
    матчится."""
    make_gc_with_mock([
        {"return": {
            "node": {"fields": {"nodes": [
                {"id": "PVTSSF_aaa", "name": "status",  # lowercase!
                 "options": [{"id": "opt1", "name": "Предложено"}]},
            ]}}
        }},
    ])
    result = await gc.find_status_field("pat", "PVT_kwXXX")
    check("T17: 'Status' is case-sensitive (lowercase 'status' not matched)",
          result is None, f"got {result!r}")


# ── T18: set_item_status — mutation payload ─────────────────────────────
async def test_18_set_item_status_payload():
    """Проверяем, что set_item_status отправляет correct GraphQL variables."""
    captured = []

    async def capture_graphql(pat, query, variables=None):
        captured.append({"query": query, "variables": variables})
        return {"updateProjectV2ItemFieldValue": {
            "projectV2Item": {"id": "PVTI_xxx"}
        }}

    gc._graphql = capture_graphql
    await gc.set_item_status(
        pat="pat", project_node_id="PVT_kwXXX",
        item_id="PVTI_xxx", field_id="PVTSSF_aaa", option_id="opt1",
    )
    if not captured:
        check("T18: set_item_status called _graphql", False, "no call captured")
        return
    vars = captured[0]["variables"]
    ok1 = vars.get("projectId") == "PVT_kwXXX"
    ok2 = vars.get("itemId") == "PVTI_xxx"
    ok3 = vars.get("fieldId") == "PVTSSF_aaa"
    ok4 = vars.get("optionId") == "opt1"
    # Проверяем что mutation содержит singleSelectOptionId.
    query = captured[0]["query"]
    ok5 = "singleSelectOptionId" in query and "updateProjectV2ItemFieldValue" in query
    check("T18: variables.projectId correct", ok1, f"vars={vars}")
    check("T18a: variables.itemId correct", ok2)
    check("T18b: variables.fieldId correct", ok3)
    check("T18c: variables.optionId correct", ok4)
    check("T18d: mutation uses singleSelectOptionId", ok5)


# ── T19: set_item_status_by_name — best-effort exception suppression ───
async def test_19_set_by_name_suppresses_exceptions():
    """Даже если set_item_status кидает неожиданное исключение,
    set_item_status_by_name должен его поймать и вернуть False."""
    # Мокаем find_status_field чтобы вернуть валидный field_info,
    # но _graphql для set_item_status кидает.
    call_count = [0]

    async def mock_graphql(pat, query, variables=None):
        call_count[0] += 1
        if call_count[0] == 1:
            # find_status_field — успех.
            return {
                "node": {"fields": {"nodes": [
                    {"id": "PVTSSF_aaa", "name": "Status",
                     "options": [{"id": "opt1", "name": "Предложено"}]},
                ]}}
            }
        elif call_count[0] == 2:
            # set_item_status — падает.
            raise gc.GithubApiError("Forbidden: no project write permission",
                                     status=200)
        raise AssertionError("unexpected 3rd call")

    gc._graphql = mock_graphql
    try:
        result = await gc.set_item_status_by_name(
            pat="pat", project_node_id="PVT_kwXXX",
            item_id="PVTI_xxx", status_name="Предложено",
        )
        check("T19: set_item_status error → False (no exception)",
              result is False, f"result={result!r}")
    except Exception as e:
        check("T19: set_item_status error → False (no exception)",
              False, f"raised: {type(e).__name__}: {e}")


# ── T20: db.py — default в seed для нового singleton'а ──────────────────
def test_20_db_seed_default():
    """При создании singleton github_settings (новая БД) project_status_option_name
    должен быть 'Предложено'."""
    src = (V485_DIR / "db.py").read_text(encoding="utf-8")
    # Ищем GithubSettings(id=1, ...) с project_status_option_name="Предложено".
    ok = re.search(
        r'GithubSettings\s*\(\s*id\s*=\s*1.*?'
        r'project_status_option_name\s*=\s*["\']Предложено["\']',
        src, re.DOTALL,
    ) is not None
    check("T20: db.py seed sets project_status_option_name='Предложено'",
          ok, "default not found in seed")


# ── Запуск ──────────────────────────────────────────────────────────────
async def main():
    print("=" * 70)
    print("test_v4853_status_field.py — v4.8.5.3 tests")
    print("=" * 70)
    print()

    await test_01_find_status_field_ok()
    await test_02_find_status_field_not_found()
    await test_03_find_status_field_node_none()
    await test_04_set_by_name_ok()
    await test_05_status_field_missing()
    await test_06_option_not_found()
    await test_07_graphql_error_returns_false()
    await test_08_default_status_name()
    test_09_bot_handlers_calls_set_status()
    test_10_db_field_present()
    test_11_db_migration_present()
    test_12_app_version()
    test_13_web_app_endpoints()
    test_14_admin_settings_field()
    test_15_changelog()
    test_16_syntax()
    await test_17_case_sensitive()
    await test_18_set_item_status_payload()
    await test_19_set_by_name_suppresses_exceptions()
    test_20_db_seed_default()

    print()
    for c in checks:
        print(c)
    print()
    print(f"Total: {passed} passed, {failed} failed, {passed+failed} checks")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
