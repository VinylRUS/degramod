"""
logging_config.py — конфигурация логирования: stdout + файл + Grafana Cloud Loki.

v5.7.0. Вызывается ОДНОЙ из первых в bot.py, до любого импорта, который создаёт
`logger = logging.getLogger(...)`. Иначе root logger получил бы StreamHandler
от `basicConfig`, и handler'ы дублировались бы.

Три handler'а на root logger:

1. **StreamHandler** — всегда. Пишет в stdout, чтобы Bothost terminal и
   `agent /api/bots/logs` продолжали работать. Без этого Bothost панель
   «Логи работы» перестала бы показывать логи бота.

2. **TimedRotatingFileHandler** — всегда, если `/app/data/logs/` доступен.
   Ежедневная ротация, 7 дней хранения. Папка `/app/data` персистентна на
   Bothost, файл переживает рестарт контейнера. Offline fallback: если Loki
   недоступен, логи остаются в файле и их можно посмотреть через веб-терминал.

3. **LokiHandler** — только если задан `LOKI_URL`. Отправляет логи в
   Grafana Cloud Loki (или любой Loki-совместимый сервер). Импорт
   `logging_loki` guarded — если библиотека не установлена, пишем warning
   и пропускаем handler, бот стартует без него.

Секреты в логи не пишем: `BOT_TOKEN`, `WEB_PASSWORD`, `SESSION_SECRET` и
прочее берутся из env и в `logger.info(...)` не попадают. Если когда-нибудь
попадут — добавим `_sanitize` (см. roadmap v5.8.0).

Стиль:
- Формат unchanged: `"%(asctime)s │ %(name)-24s │ %(levelname)-7s │ %(message)s"`.
- Loki парсит plain text; в Grafana UI ищем через LogQL `|= "ERROR"` или
  `|= "Polling error"`.

Labels в Loki (low cardinality — это важно для производительности Loki):
- `app = "degramod"` — отличить от других ботов, если когда-нибудь заведём.
- `version = "v5.7.0"` — фильтровать по релизам.
- `env = "production"` — отличать от staging.

Чего НЕ тегаем: `chat_id`, `user_id`, `level` — это дало бы взрыв
кардинальности и убило бы Loki. Эти поля живут в тексте лога.
"""
from __future__ import annotations

import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# Локальный файл — offline fallback, всегда включён (если каталог доступен).
_LOG_DIR = Path(os.getenv("LOG_DIR", "/app/data/logs"))
_LOG_FILE = _LOG_DIR / "bot.log"
_RETENTION_DAYS = 7

# Формат unchanged из v5.6.1 — чтобы логи в Bothost terminal выглядели так же.
_FORMAT = "%(asctime)s │ %(name)-24s │ %(levelname)-7s │ %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def configure_logging(level: int = logging.INFO) -> None:
    """Настраивает root logger: stdout + файл (всегда) + Loki (если задан).

    Идемпотентна: повторный вызов чистит handler'ы, не дублирует.
    Нужно тестам, которые прогоняют configure_logging() несколько раз в
    одном процессе.
    """
    root = logging.getLogger()
    root.setLevel(level)
    # Чистим handler'ы от возможного повторного вызова (тесты, reload).
    root.handlers.clear()

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    # 1. StreamHandler — для Bothost terminal и agent /api/bots/logs.
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)

    # 2. TimedRotatingFileHandler — ежедневная ротация, 7 дней.
    # OSError не валит старт: если нет прав на /app/data/logs, fallback на
    # только StreamHandler. Лог предупреждения уйдёт в stdout.
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            _LOG_FILE,
            when="midnight",
            interval=1,
            backupCount=_RETENTION_DAYS,
            encoding="utf-8",
            # Локальное время, как в логах v5.6.1 — иначе сдвинутся таймстемпы
            # на 3 часа и в Bothost terminal будет каша из UTC и MSK.
            utc=False,
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as e:
        # StreamHandler уже стоит, логи будут видны в Bothost terminal.
        # Не используем root.warning — root может быть без handler'ов на этом
        # этапе, если file_handler — второй. Stream уже добавлен, должно дойти.
        root.warning("file logging disabled (cannot create %s): %s", _LOG_DIR, e)

    # 3. Loki handler — только если задан LOKI_URL. Иначе бот стартует без
    # удалённого логирования, только stdout + файл.
    loki_url = os.getenv("LOKI_URL", "").strip()
    if loki_url:
        _add_loki_handler(root)


def _add_loki_handler(root: logging.Logger) -> None:
    """Добавляет Loki handler, если библиотека установлена и URL задан.

    Любая ошибка (нет библиотеки, нет сети, неверный URL) — warning в лог,
    бот стартует без Loki. Это сознательное решение: удалённое логирование
    не должно валить прод, если оно само сломалось.
    """
    try:
        import logging_loki  # type: ignore[import-untyped]
    except ImportError:
        root.warning(
            "LOKI_URL задан, но пакет logging-loki не установлен — "
            "удалённое логирование отключено",
        )
        return

    env = os.getenv("LOKI_ENVIRONMENT", "production")
    # APP_VERSION_TAG — отдельная переменная, чтобы не тянуть web_app.py
    # (он импортирует fastapi, jinja2 и т.д. — тяжёлый для logging_config).
    # В bot.py после импорта web_app можно вызвать
    # logging_loki handler'у setTags — но это усложняет код. Проще:
    # бот задаёт APP_VERSION_TAG = APP_VERSION перед configure_logging().
    version = os.getenv("APP_VERSION_TAG", "unknown")

    loki_url = os.getenv("LOKI_URL", "").strip()
    user = os.getenv("LOKI_USER", "").strip()
    password = os.getenv("LOKI_PASSWORD", "")

    # Basic auth: Grafana Cloud использует user_id + API key.
    # Если задан только password — передаём пустого user'а, Loki handler
    # примет как ("", password).
    auth: tuple[str, str] | None
    if user and password:
        auth = (user, password)
    elif password:
        auth = ("", password)
    else:
        # Без auth — публичный Loki (бывает в self-hosted). Grafana Cloud
        # без auth запрос отклонит, но не падать же старт из-за этого.
        auth = None

    try:
        # python-logging-loki 0.3.1: LokiHandler(url, tags, auth, quiet).
        # timeout не поддерживается (библиотека синхронная на requests).
        # Если когда-нибудь переедем на async-клиент (aiohttp) — добавим.
        loki_handler = logging_loki.LokiHandler(
            url=loki_url,
            tags={"app": "degramod", "version": version, "env": env},
            auth=auth,
        )
        # Loki ожидает message как основную строку. Уровень и logger name
        # идут в tags автоматически (logging-loki умеет их извлекать), но
        # мы форматируем строку так, чтобы в Grafana UI было видно:
        # "2026-09-22 14:23:11 │ shadow_logger.main │ INFO │ Bot started"
        # Это даёт единый формат между stdout, файлом и Loki.
        loki_handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
        root.addHandler(loki_handler)
    except Exception as e:
        # Любая ошибка конфигурации Loki — warning, бот продолжает работать.
        root.warning(
            "Loki handler setup failed: %s — логи идут в stdout и файл", e,
        )
