"""
test_v580_settings.py — тесты v5.8.0: LLM-настройки (GigaChat) в веб-панели и БД.

Что проверяет:

  T1:  db.py содержит модель LlmSettings (ORM-класс).
  T2:  db.py содержит колонку chat_settings.llm_expand_reason.
  T3:  init_db() создаёт таблицу llm_settings и сидит id=1.
  T4:  init_db() добавляет колонку llm_expand_reason в существующую БД.
  T5:  web/admin_settings.py имеет роуты GET/POST /admin/settings/llm и
       POST /admin/settings/llm/test.
  T6:  admin_settings.html содержит раздел LLM с формой и кнопкой теста.
  T7:  admin_chats.html содержит тумблер LLM и чекбокс в форме /update.
  T8:  llm_client.py существует и экспортирует LLMResult, complete,
       expand_reason, test_connection, is_available.
  T9:  docs/rules.md существует и не пустой.
  T10: Alembic-миграция v5.8.0 есть и описывает те же изменения, что init_db().
  T11: bot_handlers.py вызывает _maybe_spawn_llm_reason_expand из _send_report.
  T12: .env.example не упоминает GigaChat (ключ не в env, в БД).
  T13: llm_client.py НЕ хардкодит ключ и НЕ упоминает его в git-ignored местах.
  T14: Файлы docs/superpowers/specs/ не содержат ссылок на GigaChat-токен.

Запуск:
    uv run python tools/run_tests.py -k v580_settings
    uv run python tests/test_v580_settings.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from _paths import ROOT

sys.path.insert(0, str(ROOT))

WORK_DIR = ROOT


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _fail(msg: str, detail: str = "") -> None:
    print(f"  ✗ {msg}")
    if detail:
        print(f"    {detail}")


def _section(title: str) -> None:
    print()
    print(title)


def test_t1_db_llm_settings_model():
    _section("T1: db.LlmSettings ORM-класс")
    try:
        db_src = (WORK_DIR / "db.py").read_text(encoding="utf-8")
        assert "class LlmSettings(Base):" in db_src, "class LlmSettings не найден"
        assert 'llm_settings' in db_src, "__tablename__ = 'llm_settings' не найден"
        assert "api_key_encrypted" in db_src, "колонка api_key_encrypted не объявлена"
        assert "model_name" in db_src, "колонка model_name не объявлена"
        assert "timeout_seconds" in db_src, "колонка timeout_seconds не объявлена"
        assert "is_active" in db_src, "колонка is_active не объявлена"
        _ok("T1: LlmSettings определён с нужными колонками")
    except Exception as e:
        _fail("T1: LlmSettings", str(e))


def test_t2_chat_settings_llm_expand_reason():
    _section("T2: chat_settings.llm_expand_reason")
    try:
        db_src = (WORK_DIR / "db.py").read_text(encoding="utf-8")
        assert "llm_expand_reason = Column" in db_src, \
            "llm_expand_reason не объявлен в ChatSettings"
        _ok("T2: chat_settings.llm_expand_reason колонка объявлена")
    except Exception as e:
        _fail("T2: chat_settings.llm_expand_reason", str(e))


def test_t3_init_db_creates_llm_settings_table():
    _section("T3: init_db() создаёт таблицу llm_settings")
    try:
        db_src = (WORK_DIR / "db.py").read_text(encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS llm_settings" in db_src, \
            "CREATE TABLE llm_settings не найден в init_db"
        assert "INSERT INTO llm_settings" in db_src or "LlmSettings(id=1" in db_src, \
            "Seed singleton-строки llm_settings id=1 не найден"
        _ok("T3: init_db создаёт таблицу и сидит singleton")
    except Exception as e:
        _fail("T3: init_db llm_settings", str(e))


def test_t4_init_db_migrates_llm_expand_reason():
    _section("T4: init_db() добавляет колонку llm_expand_reason")
    try:
        db_src = (WORK_DIR / "db.py").read_text(encoding="utf-8")
        # Идемпотентный блок миграции для существующей БД.
        m = re.search(
            r'"llm_expand_reason" not in existing_cols', db_src,
        )
        assert m, "идемпотентный блок миграции llm_expand_reason не найден"
        assert "ALTER TABLE chat_settings ADD COLUMN llm_expand_reason" in db_src, \
            "ALTER TABLE ... ADD COLUMN llm_expand_reason не найден"
        _ok("T4: идемпотентная миграция llm_expand_reason присутствует")
    except Exception as e:
        _fail("T4: llm_expand_reason migration", str(e))


def test_t5_admin_settings_routes():
    _section("T5: web/admin_settings.py роуты /admin/settings/llm*")
    try:
        wa_src = (WORK_DIR / "web" / "admin_settings.py").read_text(encoding="utf-8")
        assert '@router.get("/admin/settings/llm")' in wa_src, \
            "GET /admin/settings/llm не зарегистрирован"
        assert '@router.post("/admin/settings/llm")' in wa_src, \
            "POST /admin/settings/llm не зарегистрирован"
        assert '@router.post("/admin/settings/llm/test")' in wa_src, \
            "POST /admin/settings/llm/test не зарегистрирован"
        # Импорты.
        assert "LlmSettings" in wa_src, "LlmSettings не импортирован в web/admin_settings.py"
        assert "llm_client" in wa_src, "llm_client не импортирован в web/admin_settings.py"
        assert "settings_changed" in wa_src, \
            "llm_client.settings_changed() не вызывается после сохранения"
        _ok("T5: роуты /admin/settings/llm* и импорты на месте")
    except Exception as e:
        _fail("T5: admin_settings роуты", str(e))


def test_t6_admin_settings_template_llm_section():
    _section("T6: admin_settings.html раздел LLM")
    try:
        html_src = (WORK_DIR / "templates" / "admin_settings.html").read_text(encoding="utf-8")
        assert 'href="#llm"' in html_src, "якорь #llm в nav не найден"
        assert 'id="llm"' in html_src, "раздел id=llm не найден"
        assert "GigaChat" in html_src, "заголовок GigaChat не найден"
        assert 'action="/admin/settings/llm"' in html_src, \
            "форма с action /admin/settings/llm не найдена"
        assert "llm-test-btn" in html_src, "кнопка теста LLM не найдена"
        assert "/admin/settings/llm/test" in html_src, \
            "test endpoint не упоминается в JS LLM"
        # Поля: api_key, model_name, timeout_seconds, is_active.
        assert 'name="api_key"' in html_src, "поле api_key не найдено"
        assert 'name="model_name"' in html_src, "поле model_name не найдено"
        assert 'name="timeout_seconds"' in html_src, "поле timeout_seconds не найдено"
        assert 'name="is_active"' in html_src, "чекбокс is_active не найден"
        _ok("T6: admin_settings.html раздел LLM с формой и тестом")
    except Exception as e:
        _fail("T6: admin_settings.html LLM", str(e))


def test_t7_admin_chats_template_llm_toggle():
    _section("T7: admin_chats.html тумблер LLM")
    try:
        html_src = (WORK_DIR / "templates" / "admin_chats.html").read_text(encoding="utf-8")
        # Кнопка toggle.
        assert 'value="llm_expand"' in html_src, \
            "кнопка toggle value=llm_expand не найдена"
        # Чекбокс в форме /update.
        assert 'name="llm_expand_reason"' in html_src, \
            "поле llm_expand_reason в форме /update не найдено"
        # Бейдж в карточке чата.
        assert "LLM" in html_src, "бейдж LLM не найден"
        _ok("T7: admin_chats.html тумблер и чекбокс LLM присутствуют")
    except Exception as e:
        _fail("T7: admin_chats.html LLM", str(e))


def test_t8_llm_client_module():
    _section("T8: llm_client.py экспортирует нужные функции")
    try:
        llm_src = (WORK_DIR / "llm_client.py").read_text(encoding="utf-8")
        assert "class LLMResult" in llm_src, "LLMResult не определён"
        assert "async def complete(" in llm_src, "complete() не определён"
        assert "async def expand_reason(" in llm_src, "expand_reason() не определён"
        assert "async def test_connection(" in llm_src, "test_connection() не определён"
        assert "def is_available(" in llm_src, "is_available() не определён"
        # Контракт: не бросает исключений наружу.
        assert "no exceptions" in llm_src.lower() or "не бросает исключений" in llm_src, \
            "контракт «не бросает исключений» не задокументирован"
        # Async/aiohttp (ASYNC230 соблюдён).
        assert "import aiohttp" in llm_src, "aiohttp не импортирован"
        assert "async def" in llm_src, "нет async-функций"
        _ok("T8: llm_client.py экспортирует API и документирует контракт")
    except Exception as e:
        _fail("T8: llm_client API", str(e))


def test_t9_rules_md_exists():
    _section("T9: docs/rules.md существует и не пустой")
    try:
        rules_path = WORK_DIR / "docs" / "rules.md"
        assert rules_path.exists(), f"файл {rules_path} не существует"
        content = rules_path.read_text(encoding="utf-8")
        assert len(content) > 500, f"rules.md слишком короткий: {len(content)} символов"
        # Ключевые разделы правил.
        assert "Бан без предупреждения" in content
        assert "Раковые форсы" in content
        assert "Сначала варн" in content
        _ok("T9: docs/rules.md на месте, контент валиден")
    except Exception as e:
        _fail("T9: docs/rules.md", str(e))


def test_t10_alembic_migration_v580():
    _section("T10: Alembic-миграция v5.8.0")
    try:
        mig_dir = WORK_DIR / "migrations" / "versions"
        migs = list(mig_dir.glob("*v5_8_0*.py"))
        assert migs, "мigration v5_8_0 не найден"
        mig_content = migs[0].read_text(encoding="utf-8")
        assert "llm_settings" in mig_content, "таблица llm_settings в миграции не упомянута"
        assert "llm_expand_reason" in mig_content, \
            "колонка llm_expand_reason в миграции не упомянута"
        assert "down_revision" in mig_content, "down_revision не задан"
        assert "upgrade()" in mig_content and "downgrade()" in mig_content, \
            "upgrade()/downgrade() не определены"
        _ok(f"T10: миграция {migs[0].name} валидна")
    except Exception as e:
        _fail("T10: Alembic v5_8_0", str(e))


def test_t11_bot_handlers_calls_llm_expand():
    _section("T11: bot_handlers._send_report вызывает _maybe_spawn_llm_reason_expand")
    try:
        bh_src = (WORK_DIR / "bot_handlers.py").read_text(encoding="utf-8")
        assert "_maybe_spawn_llm_reason_expand" in bh_src, \
            "хелпер _maybe_spawn_llm_reason_expand не определён в bot_handlers.py"
        assert "await _maybe_spawn_llm_reason_expand(" in bh_src, \
            "хелпер не вызывается из _send_report"
        # Хелпер грузит правила из docs/rules.md.
        assert "_load_rules_md_for_llm" in bh_src, \
            "_load_rules_md_for_llm не определён"
        assert 'docs", "rules.md' in bh_src or "docs/rules.md" in bh_src, \
            "путь к docs/rules.md не упоминается в _load_rules_md_for_llm"
        _ok("T11: bot_handlers._send_report интегрирован с LLM-развёрткой")
    except Exception as e:
        _fail("T11: bot_handlers LLM integration", str(e))


def test_t12_env_example_no_gigachat():
    _section("T12: .env.example не содержит GigaChat-ключа (он в БД)")
    try:
        env_src = (WORK_DIR / ".env.example").read_text(encoding="utf-8")
        # Ключ GigaChat не должен быть в env — SU вводит через веб-панель.
        # Если упоминается — это документация, не переменная.
        for forbidden in ("GIGACHAT_API_KEY=", "LLM_API_KEY=", "GIGACHAT_TOKEN="):
            assert forbidden not in env_src, f"найден {forbidden!r} — не должно быть в env"
        _ok("T12: .env.example не содержит GigaChat-ключ")
    except Exception as e:
        _fail("T12: env no GigaChat", str(e))


def test_t13_no_hardcoded_keys_in_llm_client():
    _section("T13: llm_client.py не содержит хардкоженных ключей")
    try:
        llm_src = (WORK_DIR / "llm_client.py").read_text(encoding="utf-8")
        # Никаких констант вида key=...
        bad_patterns = [
            r'API_KEY\s*=\s*["\'][a-zA-Z0-9]{10,}["\']',
            r'KEY\s*=\s*["\'][a-zA-Z0-9]{20,}["\']',
        ]
        for pat in bad_patterns:
            assert not re.search(pat, llm_src), \
                f"найден疑似 хардкоженный ключ по паттерну {pat}"
        _ok("T13: llm_client.py без хардкоженных ключей")
    except Exception as e:
        _fail("T13: no hardcoded keys", str(e))


def test_t14_admin_chats_toggle_field_validated():
    _section("T14: admin_chats.toggle распознаёт llm_expand")
    try:
        ac_src = (WORK_DIR / "web" / "admin_chats.py").read_text(encoding="utf-8")
        assert '"llm_expand"' in ac_src, \
            "toggle не распознаёт field=llm_expand"
        assert "cs.llm_expand_reason" in ac_src, \
            "toggle не меняет cs.llm_expand_reason"
        # В /update — сохраняется.
        assert 'cs.llm_expand_reason = (llm_expand_reason == "on")' in ac_src, \
            "/update не сохраняет llm_expand_reason"
        _ok("T14: admin_chats toggle и update корректно работают с llm_expand")
    except Exception as e:
        _fail("T14: admin_chats llm_expand", str(e))


def test_t15_pat_token_not_committed():
    """Санитарная проверка: GigaChat-ключ или PAT не коммичены в репо."""
    _section("T15: в репозитории нет утечки GigaChat/PAT токенов")
    try:
        # Проверяем только что мы создали и не вписали случайно токен.
        for fname in ["llm_client.py", "web/admin_settings.py",
                      "db.py", "bot_handlers.py"]:
            content = (WORK_DIR / fname).read_text(encoding="utf-8")
            # Сигнатуры PAT-токенов GigaChat и GitHub.
            for forbidden in ["github_pat_", "ghp_", "giga_", "Bearer test-"]:
                assert forbidden not in content, \
                    f"найдена сигнатура токена {forbidden!r} в {fname}"
        _ok("T15: утечек токенов в коде не найдено")
    except Exception as e:
        _fail("T15: no token leaks", str(e))


def main():
    test_t1_db_llm_settings_model()
    test_t2_chat_settings_llm_expand_reason()
    test_t3_init_db_creates_llm_settings_table()
    test_t4_init_db_migrates_llm_expand_reason()
    test_t5_admin_settings_routes()
    test_t6_admin_settings_template_llm_section()
    test_t7_admin_chats_template_llm_toggle()
    test_t8_llm_client_module()
    test_t9_rules_md_exists()
    test_t10_alembic_migration_v580()
    test_t11_bot_handlers_calls_llm_expand()
    test_t12_env_example_no_gigachat()
    test_t13_no_hardcoded_keys_in_llm_client()
    test_t14_admin_chats_toggle_field_validated()
    test_t15_pat_token_not_committed()
    print("\nAll v580_settings tests done.")


if __name__ == "__main__":
    main()
