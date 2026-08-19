#!/usr/bin/env python3
"""
test_v485_idea.py — тесты v4.8.5 (идеи через !idea → GitHub Issues + Project).

Проверяет:
  T1:  Модель IdeaLog существует в db.py
  T2:  Модель GithubSettings существует в db.py
  T3:  Миграции CREATE TABLE IF NOT EXISTS idea_log, github_settings в init_db
  T4:  Шифрование PAT: _encrypt_pat / _decrypt_pat round-trip
  T5:  Шифрование PAT: разные PAT — разные шифр-тексты
  T6:  github_client.py — функции существуют и корректно сигнатурированы
  T7:  bot_handlers.py — обработчики cmd_idea_dm, cmd_idea_modchat
  T8:  bot_handlers.py — _IDEA_MAX_LEN = 200
  T9:  bot_handlers.py — стелс: _resolve_sender_web_user возвращает None для постороннего
  T10: bot_handlers.py — /help (full и moderator) содержит !idea
  T11: web_app.py — APP_VERSION начинается с v4.8.5
  T12: web/admin_settings.py — endpoints /admin/settings/github (GET/POST) + /admin/settings/github/test
  T13: admin_settings.html — раздел GitHub Projects присутствует
  T14: base.html — changelog v4.8.5
  T15: requirements.txt — cryptography
  T16: db.py — seed singleton github_settings (id=1) в init_db
  T17: github_client.py — TestResult, IssueRef, GithubApiError датаклассы/исключения
  T18: bot_handlers.py — _is_modchat_chat функция существует
  T19: bot_handlers.py — _send_idea_alert_to_su функция существует
  T20: bot_handlers.py — _process_idea_submission функция существует

Запуск:
    uv run pytest tests/test_v485_idea.py
"""
from _paths import _P  # noqa: E402  (корень вычисляется от __file__)

import asyncio
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path

# ── Пути ────────────────────────────────────────────────────────────────────
WORK_DIR = Path(_P())
sys.path.insert(0, str(WORK_DIR))

# Устанавливаем временный DB_PATH ДО любого импорта db.
_tmpdir = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmpdir, "test_v485.db")
# Также используем временный ключ шифрования PAT — чтобы не засорять
# окружение .github_enc_key файлом. Это валидный Fernet-ключ (32 urlsafe-base64 байта),
# сгенерированный через Fernet.generate_key().
os.environ["GITHUB_IDEA_ENC_KEY"] = "_yWQdScCAPvmkhzajSRZjkuT6eOUexYGKSbs5dl3a6s="

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def _ok(name: str, detail: str = "") -> None:
    global PASS
    PASS += 1
    print(f"  ✓ {name}{(' — ' + detail) if detail else ''}")


def _fail(name: str, detail: str) -> None:
    global FAIL
    FAIL += 1
    ERRORS.append(f"{name}: {detail}")
    print(f"  ✗ {name} — {detail}")


def _section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ═══════════════════════════════════════════════════════════════════════════
# T1-T3: db.py — модели и миграции
# ═══════════════════════════════════════════════════════════════════════════

def test_db_models():
    _section("T1-T3: db.py — модели и миграции")

    # T1: IdeaLog модель
    try:
        import db
        assert hasattr(db, "IdeaLog"), "IdeaLog не найден в db"
        il = db.IdeaLog
        assert il.__tablename__ == "idea_log", f"tablename={il.__tablename__}"
        cols = {c.name for c in il.__table__.columns}
        expected = {"id", "tg_user_id", "tg_username", "tg_display_name",
                    "source", "source_chat_id", "idea_text",
                    "github_issue_url", "github_issue_number",
                    "github_project_item_id", "error_message",
                    "bot_version", "created_at"}
        missing = expected - cols
        assert not missing, f"отсутствуют колонки: {missing}"
        _ok("T1: IdeaLog модель", f"columns={sorted(cols)}")
    except Exception as e:
        _fail("T1: IdeaLog модель", str(e))

    # T2: GithubSettings модель
    try:
        import db
        assert hasattr(db, "GithubSettings"), "GithubSettings не найден в db"
        gs = db.GithubSettings
        assert gs.__tablename__ == "github_settings", f"tablename={gs.__tablename__}"
        cols = {c.name for c in gs.__table__.columns}
        expected = {"id", "pat_encrypted", "repo_owner", "repo_name",
                    "project_node_id", "project_number", "project_owner_login",
                    "is_active", "updated_at", "updated_by"}
        missing = expected - cols
        assert not missing, f"отсутствуют колонки: {missing}"
        _ok("T2: GithubSettings модель", f"columns={sorted(cols)}")
    except Exception as e:
        _fail("T2: GithubSettings модель", str(e))

    # T3: Миграции CREATE TABLE IF NOT EXISTS
    try:
        db_src = (WORK_DIR / "db.py").read_text()
        assert "CREATE TABLE IF NOT EXISTS idea_log" in db_src, \
            "CREATE TABLE IF NOT EXISTS idea_log не найдено"
        assert "CREATE TABLE IF NOT EXISTS github_settings" in db_src, \
            "CREATE TABLE IF NOT EXISTS github_settings не найдено"
        assert "v4.8.5" in db_src, "комментарий v4.8.5 не найден в db.py"
        _ok("T3: Миграции в init_db")
    except Exception as e:
        _fail("T3: Миграции в init_db", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# T4-T5: Шифрование PAT
# ═══════════════════════════════════════════════════════════════════════════

def test_pat_encryption():
    _section("T4-T5: Шифрование PAT")

    # T4: round-trip — зашифровали, расшифровали, получили то же
    try:
        import db
        original = "ghp_abcdef1234567890xyz_TEST_TOKEN"
        enc = db._encrypt_pat(original)
        assert enc != original, "шифр-текст не должен совпадать с оригиналом"
        dec = db._decrypt_pat(enc)
        assert dec == original, f"round-trip失败: {dec} != {original}"
        _ok("T4: PAT round-trip", f"enc_len={len(enc)}, dec_match=True")
    except Exception as e:
        _fail("T4: PAT round-trip", str(e))

    # T5: разные PAT → разные шифр-тексты (Fernet использует случайный IV)
    try:
        import db
        pat1 = "ghp_token_one_12345"
        pat2 = "ghp_token_two_67890"
        enc1 = db._encrypt_pat(pat1)
        enc2 = db._encrypt_pat(pat2)
        assert enc1 != enc2, "разные PAT должны давать разные шифр-тексты"
        # Тот же PAT зашифрованный повторно — тоже разный шифр-текст (IV случайный).
        enc1_again = db._encrypt_pat(pat1)
        assert enc1 != enc1_again, \
            "Fernet должен использовать случайный IV — шифр-тексты должны различаться"
        # Но оба расшифровываются в один PAT.
        assert db._decrypt_pat(enc1) == pat1
        assert db._decrypt_pat(enc1_again) == pat1
        _ok("T5: разные PAT → разные шифр-тексты (random IV)")
    except Exception as e:
        _fail("T5: разные PAT → разные шифр-тексты", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# T6, T17: github_client.py
# ═══════════════════════════════════════════════════════════════════════════

def test_github_client():
    _section("T6, T17: github_client.py")

    # T6: Функции существуют
    try:
        import github_client
        for fname in ("create_issue", "add_issue_to_project",
                      "get_issue_node_id", "get_project_node_id",
                      "test_connection"):
            assert hasattr(github_client, fname), f"нет функции {fname}"
        _ok("T6: github_client функции",
            f"{', '.join(['create_issue','add_issue_to_project','get_project_node_id','get_project_node_id','test_connection'])}")
    except Exception as e:
        _fail("T6: github_client функции", str(e))

    # T17: Датаклассы и исключения
    try:
        import github_client
        # IssueRef — датакласс.
        ir = github_client.IssueRef(number=42, url="https://x/y/issues/42",
                                    node_id="I_x", title="test")
        assert ir.number == 42
        assert ir.url == "https://x/y/issues/42"
        # TestResult — датакласс.
        tr = github_client.TestResult(ok=True, message="ok")
        assert tr.ok is True
        assert tr.message == "ok"
        assert tr.details == {}  # default_factory
        # GithubApiError — исключение.
        try:
            raise github_client.GithubApiError("test", status=404, body="not found")
        except github_client.GithubApiError as e:
            assert e.status == 404
            assert "test" in str(e)
        _ok("T17: IssueRef, TestResult, GithubApiError")
    except Exception as e:
        _fail("T17: IssueRef, TestResult, GithubApiError", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# T7-T10, T18-T20: bot_handlers.py
# ═══════════════════════════════════════════════════════════════════════════

def test_bot_handlers():
    _section("T7-T10, T18-T20: bot_handlers.py")

    # T7: Обработчики cmd_idea_dm, cmd_idea_modchat
    try:
        bh_src = (WORK_DIR / "bot_handlers.py").read_text()
        assert "async def cmd_idea_dm" in bh_src, "cmd_idea_dm не найден"
        assert "async def cmd_idea_modchat" in bh_src, "cmd_idea_modchat не найден"
        # Зарегистрированы в router. Принимаем любой prefix= (v4.8.5: без prefix,
        # v4.8.5.2+: prefix="!/" — багфикс, что !idea не ловился).
        assert re.search(r'Command\s*\(\s*[\'"]idea[\'"]', bh_src), \
            'Command("idea") не найден'
        assert 'F.chat.type == "private"' in bh_src, "DM filter не найден"
        assert 'F.chat.type != "private"' in bh_src, "group filter не найден"
        _ok("T7: cmd_idea_dm, cmd_idea_modchat")
    except Exception as e:
        _fail("T7: cmd_idea_dm, cmd_idea_modchat", str(e))

    # T8: _IDEA_MAX_LEN = 200
    try:
        bh_src = (WORK_DIR / "bot_handlers.py").read_text()
        m = re.search(r"_IDEA_MAX_LEN\s*=\s*(\d+)", bh_src)
        assert m, "_IDEA_MAX_LEN не найден"
        assert int(m.group(1)) == 200, f"_IDEA_MAX_LEN = {m.group(1)}, ожидалось 200"
        # Проверка лимита в handler'е.
        assert "Идея слишком длинная" in bh_src, "нет сообщения о слишком длинной идее"
        _ok("T8: _IDEA_MAX_LEN = 200")
    except Exception as e:
        _fail("T8: _IDEA_MAX_LEN = 200", str(e))

    # T9: Стелс — _resolve_sender_web_user возвращает None для постороннего
    try:
        async def _t9():
            import db
            await db.init_db()
            import bot_handlers
            # Юзера 999999999 нет в web_users — должен вернуть None.
            result = await bot_handlers._resolve_sender_web_user(999999999)
            assert result is None, f"ожидался None, получен {result}"
        asyncio.run(_t9())
        _ok("T9: стелс — посторонний получает None")
    except Exception as e:
        _fail("T9: стелс — посторонний получает None", str(e))

    # T10: /help содержит !idea (full + moderator)
    try:
        bh_src = (WORK_DIR / "bot_handlers.py").read_text()
        # Точно выделяем тело каждой функции (от "def _build_help_full_rich"
        # до следующего "def _build_help_moderator_rich" или конца файла).
        def _extract_func_body(src: str, func_name: str) -> str:
            """Возвращает тело функции от 'def <func_name>' до следующего
            top-level 'def ' или 'async def ' или конца файла.
            """
            pat = re.compile(rf"^(async\s+)?def\s+{re.escape(func_name)}\b", re.MULTILINE)
            m = pat.search(src)
            if not m:
                return ""
            start = m.start()
            # Ищем следующий top-level def/async def после start.
            rest = src[m.end():]
            next_pat = re.compile(r"\n(?:async\s+)?def\s+", re.MULTILINE)
            m2 = next_pat.search(rest)
            if m2:
                end = m.end() + m2.start()
            else:
                end = len(src)
            return src[start:end]

        full_body = _extract_func_body(bh_src, "_build_help_full_rich")
        mod_body = _extract_func_body(bh_src, "_build_help_moderator_rich")
        assert full_body, "не удалось извлечь тело _build_help_full_rich"
        assert mod_body, "не удалось извлечь тело _build_help_moderator_rich"
        assert "!idea" in full_body, "!idea отсутствует в full help"
        assert "!idea" in mod_body, "!idea отсутствует в moderator help"
        # Описание содержит упоминание GitHub.
        assert "GitHub" in full_body, "GitHub не упомянут в full help для !idea"
        _ok("T10: !idea в /help (full + moderator)")
    except Exception as e:
        _fail("T10: !idea в /help", str(e))

    # T18: _is_modchat_chat функция
    try:
        bh_src = (WORK_DIR / "bot_handlers.py").read_text()
        assert "async def _is_modchat_chat" in bh_src, "_is_modchat_chat не найдена"
        # Использует _get_mod_chat_id из modchat.py.
        assert "_get_mod_chat_id" in bh_src, "_get_mod_chat_id не используется"
        _ok("T18: _is_modchat_chat функция")
    except Exception as e:
        _fail("T18: _is_modchat_chat функция", str(e))

    # T19: _send_idea_alert_to_su функция
    try:
        bh_src = (WORK_DIR / "bot_handlers.py").read_text()
        assert "async def _send_idea_alert_to_su" in bh_src, \
            "_send_idea_alert_to_su не найдена"
        # Использует ADMIN_IDS для алерта SU.
        assert "ADMIN_IDS" in bh_src, "ADMIN_IDS не используется"
        # Отправляет через bot.send_message.
        assert "bot.send_message" in bh_src, "bot.send_message не используется"
        # Формат текста — "Новая идея от".
        assert "Новая идея от" in bh_src, "нет текста 'Новая идея от'"
        _ok("T19: _send_idea_alert_to_su функция")
    except Exception as e:
        _fail("T19: _send_idea_alert_to_su функция", str(e))

    # T20: _process_idea_submission функция
    try:
        bh_src = (WORK_DIR / "bot_handlers.py").read_text()
        assert "async def _process_idea_submission" in bh_src, \
            "_process_idea_submission не найдена"
        # Логирует в IdeaLog.
        assert "IdeaLog(" in bh_src, "IdeaLog не создаётся в _process_idea_submission"
        # Использует create_issue из github_client.
        assert "create_issue" in bh_src, "create_issue не вызывается"
        # Возвращает "Спасибо за идею. Передал."
        assert "Спасибо за идею. Передал." in bh_src, \
            "нет ответа 'Спасибо за идею. Передал.'"
        _ok("T20: _process_idea_submission функция")
    except Exception as e:
        _fail("T20: _process_idea_submission функция", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# T11-T12: web_app.py
# ═══════════════════════════════════════════════════════════════════════════

def test_web_app():
    _section("T11-T12: web_app.py")

    # T11: APP_VERSION должен быть v4.8.5+ (v4.8.5, v4.8.5.1, v4.8.6, v4.9.0 и т.д.)
    import re as _re
    try:
        wa_src = (WORK_DIR / "web_app.py").read_text()
        m = _re.search(r'APP_VERSION\s*=\s*"(v\d+\.\d+\.\d+[^"]*)"', wa_src)
        assert m, f"APP_VERSION не найден: {m!r}"
        ver = m.group(1)
        # Парсим major.minor.patch
        vm = _re.match(r'v(\d+)\.(\d+)\.(\d+)', ver)
        assert vm, f"APP_VERSION не парсится: {ver}"
        major, minor, patch = int(vm.group(1)), int(vm.group(2)), int(vm.group(3))
        # Принимаем v4.8.5+ (включая v4.8.6, v4.9.0 и т.д.)
        assert (major, minor, patch) >= (4, 8, 5), \
            f"APP_VERSION слишком старая: {ver} (нужно >= v4.8.5)"
        _ok(f"T11: APP_VERSION = {ver}")
    except Exception as e:
        _fail("T11: APP_VERSION v4.8.5+", str(e))

    # T12: Endpoints
    # v4.9.0 (Task 8): /admin/settings/github* переехал из web_app.py в
    # web/admin_settings.py, декоратор @app. стал @router.
    try:
        wa_src = (WORK_DIR / "web" / "admin_settings.py").read_text()
        assert '"/admin/settings/github"' in wa_src, \
            "endpoint /admin/settings/github не найден"
        assert '"/admin/settings/github/test"' in wa_src, \
            "endpoint /admin/settings/github/test не найден"
        # GET и POST методы для основного endpoint.
        assert "@router.get(\"/admin/settings/github\")" in wa_src, \
            "GET /admin/settings/github не зарегистрирован"
        assert "@router.post(\"/admin/settings/github\")" in wa_src, \
            "POST /admin/settings/github не зарегистрирован"
        # POST для test endpoint.
        assert "@router.post(\"/admin/settings/github/test\")" in wa_src, \
            "POST /admin/settings/github/test не зарегистрирован"
        # Импорты из db.
        assert "IdeaLog" in wa_src, "IdeaLog не импортирован в web/admin_settings.py"
        assert "GithubSettings" in wa_src, "GithubSettings не импортирован в web/admin_settings.py"
        assert "_encrypt_pat" in wa_src, "_encrypt_pat не импортирован"
        assert "_decrypt_pat" in wa_src, "_decrypt_pat не импортирован"
        _ok("T12: Endpoints /admin/settings/github (GET/POST) + /test")
    except Exception as e:
        _fail("T12: Endpoints /admin/settings/github", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# T13: admin_settings.html
# ═══════════════════════════════════════════════════════════════════════════

def test_admin_settings_template():
    _section("T13: admin_settings.html")

    try:
        html_src = (WORK_DIR / "templates" / "admin_settings.html").read_text()
        # Якорь в nav.
        assert 'href="#github"' in html_src, "якорь #github в nav не найден"
        # Раздел.
        assert 'id="github"' in html_src, "раздел id=github не найден"
        assert "GitHub Projects" in html_src, "заголовок GitHub Projects не найден"
        # Форма с правильным action.
        assert 'action="/admin/settings/github"' in html_src, \
            "форма с action /admin/settings/github не найдена"
        # Кнопка теста.
        assert 'github-test-btn' in html_src, "кнопка теста не найдена"
        assert "/admin/settings/github/test" in html_src, \
            "test endpoint не упоминается в JS"
        # Поля PAT, repo_owner, repo_name.
        assert 'name="pat"' in html_src, "поле pat не найдено"
        assert 'name="repo_owner"' in html_src, "поле repo_owner не найдено"
        assert 'name="repo_name"' in html_src, "поле repo_name не найдено"
        assert 'name="project_node_id"' in html_src, "поле project_node_id не найдено"
        assert 'name="is_active"' in html_src, "чекбокс is_active не найден"
        _ok("T13: admin_settings.html раздел GitHub Projects")
    except Exception as e:
        _fail("T13: admin_settings.html раздел GitHub Projects", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# T14: base.html changelog
# ═══════════════════════════════════════════════════════════════════════════

def test_base_html_changelog():
    _section("T14: base.html changelog")

    try:
        html_src = (WORK_DIR / "templates" / "base.html").read_text()
        assert "v4.8.5" in html_src, "v4.8.5 не упомянута в base.html"
        assert "14 августа 2026" in html_src, "дата 14 августа 2026 не найдена"
        assert "!idea" in html_src, "команда !idea не упомянута в changelog"
        assert "GitHub Issue" in html_src or "GitHub Issues" in html_src, \
            "GitHub Issues не упомянуты в changelog"
        assert "idea_log" in html_src, "таблица idea_log не упомянута"
        assert "github_settings" in html_src, "таблица github_settings не упомянута"
        # v4.8.5 идёт ПЕРЕД v4.8.4 (новые сверху).
        idx_485 = html_src.find("v4.8.5")
        idx_484 = html_src.find("v4.8.4")
        assert idx_485 != -1 and idx_484 != -1, "обе версии должны быть в changelog"
        assert idx_485 < idx_484, "v4.8.5 должен идти ПЕРЕД v4.8.4"
        _ok("T14: changelog v4.8.5 в base.html")
    except Exception as e:
        _fail("T14: changelog v4.8.5 в base.html", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# T15: requirements.txt
# ═══════════════════════════════════════════════════════════════════════════

def test_requirements():
    _section("T15: requirements.txt")

    try:
        req_src = (WORK_DIR / "requirements.txt").read_text()
        assert "cryptography" in req_src, "cryptography не найден в requirements.txt"
        # Версия >= 42.0.0.
        m = re.search(r"cryptography\s*>=\s*(\d+)", req_src)
        assert m, "cryptography без версии >= N"
        assert int(m.group(1)) >= 42, f"cryptography версия {m.group(1)} < 42"
        _ok("T15: cryptography в requirements.txt")
    except Exception as e:
        _fail("T15: cryptography в requirements.txt", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# T16: db.py — seed singleton github_settings (id=1)
# ═══════════════════════════════════════════════════════════════════════════

def test_db_seed():
    _section("T16: db.py — seed singleton github_settings")

    try:
        async def _t16():
            import db
            await db.init_db()
            from sqlalchemy import select
            async with db.async_session() as session:
                gs = (await session.execute(
                    select(db.GithubSettings).where(db.GithubSettings.id == 1)
                )).scalar_one_or_none()
                assert gs is not None, "singleton github_settings(id=1) не создан при init_db"
                assert gs.is_active is False, "is_active должен быть False по умолчанию"
        asyncio.run(_t16())
        _ok("T16: seed github_settings(id=1) при init_db")
    except Exception as e:
        _fail("T16: seed github_settings(id=1) при init_db", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# Главная функция
# ═══════════════════════════════════════════════════════════════════════════

def main():
    print()
    print("╔" + "═" * 60 + "╗")
    print("║  Тесты v4.8.5 — идеи через !idea → GitHub Issues         ║")
    print("╚" + "═" * 60 + "╝")

    try:
        test_db_models()
    except Exception:
        print("FATAL in test_db_models:")
        traceback.print_exc()

    try:
        test_pat_encryption()
    except Exception:
        print("FATAL in test_pat_encryption:")
        traceback.print_exc()

    try:
        test_github_client()
    except Exception:
        print("FATAL in test_github_client:")
        traceback.print_exc()

    try:
        test_bot_handlers()
    except Exception:
        print("FATAL in test_bot_handlers:")
        traceback.print_exc()

    try:
        test_web_app()
    except Exception:
        print("FATAL in test_web_app:")
        traceback.print_exc()

    try:
        test_admin_settings_template()
    except Exception:
        print("FATAL in test_admin_settings_template:")
        traceback.print_exc()

    try:
        test_base_html_changelog()
    except Exception:
        print("FATAL in test_base_html_changelog:")
        traceback.print_exc()

    try:
        test_requirements()
    except Exception:
        print("FATAL in test_requirements:")
        traceback.print_exc()

    try:
        test_db_seed()
    except Exception:
        print("FATAL in test_db_seed:")
        traceback.print_exc()

    # ── Итог ────────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  ИТОГ: {PASS} passed, {FAIL} failed")
    print(f"{'='*60}")
    if ERRORS:
        print("\nОшибки:")
        for e in ERRORS:
            print(f"  • {e}")
        sys.exit(1)
    else:
        print("\n✓ Все тесты прошли.")
        sys.exit(0)


if __name__ == "__main__":
    main()
