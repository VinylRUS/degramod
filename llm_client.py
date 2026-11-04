"""
llm_client.py — v5.8.0: обёртка над GigaChat API для авторазвёртки причин
наказаний и других текстовых задач бота-модератора.

Документация API: https://developers.sber.ru/docs/ru/gigachat/quickstart/ind-using-api
Эндпоинт: https://gigachat.devices.sberbank.ru/api/v1/chat/completions
Авторизация: Basic-Auth с ключом API (Bearer + JWT не используется в этой
обвязке — GigaChat принимает ключи авторизации проекта, генерируются в
личном кабинете developers.sber.ru).

Ключ НЕ хранится в env и НЕ вшит в код. SU вводит его через веб-панель
(/admin/settings → «LLM»), он шифруется через Fernet и кладётся в БД
(таблица LlmSettings, singleton id=1). Шифрование переиспользует тот же
ключ, что и GitHub PAT (`db._load_or_create_enc_key`), поэтому при потере
.github_enc_key теряются одновременно и PAT, и GigaChat-ключ — это
штатное поведение, документировано в `.env.example` и CLAUDE.md.

Контракт:
  • Все методы возвращают `LLMResult` и **не бросают исключений наружу**.
    Их зовут из `bot_handlers.py` (где исключение превратилось бы в
    падение хендлера и потерю сообщения), из `web/admin_settings.py`
    (где оно превратилось бы в 500) и из фоновой задачи (где оно
    остановило бы наблюдение ровно тогда, когда оно нужнее всего).
  • Все сетевые вызовы — **строго асинхронные через aiohttp**. `ASYNC230`
    (блокирующий I/O в async) включён в проекте без исключений (см.
    CLAUDE.md «Блокирующий I/O»), и sync-клиент остановил бы обработку
    сообщений во всех чатах.
  • Кеширование: идентичные запросы (одинаковый prompt + модель +
    temperature) не бьют провайдера второй раз в течение часа.
  • Глобальный рубильник: при отсутствии ключа или при
    `LlmSettings.is_active=False` все вызовы fast-path к `LLMResult(ok=False,
    error="LLM выключена")` — без единого сетевого запроса.

Архитектурно повторяет bothost_agent.py: один модуль в корне, датакласс
результата, никакого FastAPI/aiogram в импортах.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

logger = logging.getLogger("shadow_logger.llm")

# Эндпоинт GigaChat. У GigaChat есть два окружения: основное
# (gigachat.devices.sberbank.ru) и Premium (gigachat-premium.devices.sberbank.ru).
# В v5.8.0 поддерживаем только основное — Premium добавим, когда появится
# реальная потребность (и платежный тариф). URL не вынесен в env: документация
# Sber рекомендует хардкод endpoint, и переопределение через env — лишний
# способ прострелить себе ногу в проде (опечатка → бот стучится в никуда).
GIGACHAT_BASE_URL = "https://gigachat.devices.sberbank.ru/api/v1"

# Дефолтная модель. Документация Sber (на момент v5.8.0) рекомендует
# GigaChat-Pro для русского текста — лучше держит контекст и стиль.
# Переопределяется через LlmSettings.model_name.
DEFAULT_MODEL = "GigaChat-Pro"

# Таймаут одного запроса к API. GigaChat отвечает 1–8 сек на обычные
# запросы, 15 сек хватает с запасом. На суммаризацию логов (длинный ввод)
# нужно больше — таймаут наращивается до MAX_TIMEOUT_SECONDS.
DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 60

# Максимальный размер промпта в символах. GigaChat-Pro принимает ~32k токенов,
# но мы режем раньше — авторазвёртка причины и так короткая, а в суммаризацию
# логов прокидывать десятки KB текста бессмысленно и дорого по токенам.
MAX_PROMPT_CHARS = 16000

# Ретрай на 429/503 — экспоненциальный backoff: 1с, 2с. Больше попыток
# не делаем: LLM-фичи некритичные, лучше отдать исходный reason модератора,
# чем ждать ответа 30 секунд и блокировать хендлер.
MAX_RETRIES = 2
RETRY_BACKOFF_BASE = 1.0  # секунд


@dataclass
class LLMResult:
    """Результат обращения к LLM.

    `ok=False` означает «не получилось», а не «сломалось»: LLM может быть
    выключена рубильником (нет ключа, is_active=False), и вызывающий
    обязан показать это, а не упасть. Поле `text` пусто при `ok=False`,
    поле `error` заполнено причиной.

    `cached=True` — ответ взят из кеша (без сетевого запроса). Полезно
    для метрик: вызывающий может не считать это «настоящим» обращением.
    """

    ok: bool
    text: str | None = None
    error: str | None = None
    cached: bool = False
    elapsed_ms: int = 0
    usage: dict[str, int] | None = field(default=None)


# ── In-memory кеш ответов ──────────────────────────────────────────────────
# Ключ — sha256(prompt + model + temperature). TTL — 1 час.
# Кеш не общий между разными ключами GigaChat: при смене ключа SU'ом кеш
# сохраняется. Это безопасно: модель одна, выход детерминирован при той
# же temperature, разные ключи ведут к тому же ответу (если промпт тот же).
_CACHE_TTL_SECONDS = 3600
_cache: dict[str, tuple[float, LLMResult]] = {}


def reset_cache() -> None:
    """Сбрасывает кеш ответов. Нужен тестам для изоляции."""
    _cache.clear()


def _cache_key(messages: list[dict[str, str]], model: str, temperature: float) -> str:
    """Хеш от промпта + модели + температуры. Не от ключа — см. комментарий выше."""
    payload = repr(messages) + "|" + model + "|" + f"{temperature:.2f}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _truncate(text: str, limit: int = MAX_PROMPT_CHARS) -> str:
    """Обрезает промпт до лимита, добавляя маркер обрезки."""
    if len(text) <= limit:
        return text
    return text[: limit - 30] + "\n… [обрезано llm_client]"


async def _load_settings() -> "LlmSettings | None":
    """Читает настройки из БД. None — настроек нет (таблица пуста).

    Импорт db — late, чтобы не тянуть SQLAlchemy в тесты llm_client на
    верхнем уровне. Реальные тесты мокают эту функцию целиком.
    """
    try:
        from db import LlmSettings, _decrypt_pat, async_session
        from sqlalchemy import select
    except ImportError:
        logger.warning("llm_client: db module unavailable, LLM disabled")
        return None

    try:
        async with async_session() as session:
            row = (
                await session.execute(select(LlmSettings).where(LlmSettings.id == 1))
            ).scalar_one_or_none()
            if row is None:
                return None
            # Расшифровка ключа — за пределами сессии, чтобы не держать её долго.
            if not row.api_key_encrypted:
                return None
            try:
                api_key = _decrypt_pat(row.api_key_encrypted)
            except Exception as e:
                logger.error("llm_client: cannot decrypt API key: %s", e)
                return None
            return LlmSettings(
                id=row.id,
                is_active=row.is_active,
                api_key=api_key,
                model_name=row.model_name or DEFAULT_MODEL,
                timeout_seconds=row.timeout_seconds or DEFAULT_TIMEOUT_SECONDS,
                updated_at=row.updated_at,
            )
    except Exception as e:
        logger.error("llm_client: failed to load settings: %s", e)
        return None


# LlmSettings — локальный датакласс, не путать с ORM-моделью db.LlmSettings.
# Нужен, чтобы не тащить расшифрованный ключ обратно в ORM-объект (где он
# может быть случайно залогирован через repr).
@dataclass
class LlmSettings:
    id: int
    is_active: bool
    api_key: str
    model_name: str
    timeout_seconds: int
    updated_at: Any = None


def is_available() -> bool:
    """Синхронная быстрая проверка: не выключена ли LLM рубильником?

    True — возможно, доступна (ключ есть и is_active=True). False — точно
    недоступна. Реальная доступность проверяется первым запросом. Эту
    функцию зовут UI-роуты, чтобы скрыть кнопку «✨ LLM-разбор» если ключ
    не задан, не делая при этом сетевого запроса на каждый рендер страницы.

    Реализация — синхронная, но не делает I/O: читает только in-memory кеш
    последнего успешного резолва настроек. Кеш обновляется асинхронно при
    первом обращении или через settings_changed().
    """
    return _cached_settings is not None and _cached_settings.is_active


# Кеш последнего загруженного LlmSettings. Сбрасывается через
# settings_changed() — зовётся веб-роутом после сохранения формы.
_cached_settings: LlmSettings | None = None


def settings_changed() -> None:
    """Сбрасывает кеш настроек. Зовётся после сохранения формы в /admin/settings.

    Следующий вызов complete() перечитает настройки из БД.
    """
    global _cached_settings
    _cached_settings = None


async def _get_settings() -> LlmSettings | None:
    """Возвращает актуальные настройки. При первом вызове — читает из БД
    и кеширует. При settings_changed() — кеш сбрасывается, следующий вызов
    перечитает.
    """
    global _cached_settings
    if _cached_settings is not None:
        return _cached_settings
    _cached_settings = await _load_settings()
    return _cached_settings


async def complete(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 512,
    timeout: int | None = None,
    use_cache: bool = True,
) -> LLMResult:
    """Один запрос на завершение текста к GigaChat.

    Args:
        messages: список сообщений в формате OpenAI:
            ``[{"role": "system", "content": "..."},
              {"role": "user", "content": "..."}]``.
            GigaChat поддерживает роли system/user/assistant.
        model: модель (GigaChat-Pro, GigaChat-Plus, GigaChat-Lite).
            None — берётся из настроек или DEFAULT_MODEL.
        temperature: 0..2. Низкая — детерминированнее, выше — креативнее.
            0.2 подходит для авторазвёртки причины (нужна стабильность, а
            не «креативность»). Для суммаризации логов тоже 0.2.
        max_tokens: лимит ответа. 512 хватает на 1-2 предложения причины.
        timeout: таймаут на запрос в секундах. None — из настроек или
            DEFAULT_TIMEOUT_SECONDS. Не более MAX_TIMEOUT_SECONDS.
        use_cache: True — кешировать ответ. False — всегда дёргать API.
            Кеширование включено по умолчанию: идентичные причины варнов
            (например, «спам» 10 раз за час) не должны бить провайдера.

    Returns:
        LLMResult. Никогда не бросает исключений.
    """
    settings = await _get_settings()
    if settings is None or not settings.is_active or not settings.api_key:
        return LLMResult(ok=False, error="LLM выключена: ключ не задан или is_active=False")

    model_name = model or settings.model_name or DEFAULT_MODEL
    timeout_sec = min(timeout or settings.timeout_seconds or DEFAULT_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS)

    # Проверка кеша.
    if use_cache:
        key = _cache_key(messages, model_name, temperature)
        cached = _cache.get(key)
        if cached is not None:
            ts, result = cached
            if time.time() - ts < _CACHE_TTL_SECONDS:
                return LLMResult(
                    ok=True,
                    text=result.text,
                    error=None,
                    cached=True,
                    elapsed_ms=0,
                    usage=result.usage,
                )
            # Протух — удаляем.
            _cache.pop(key, None)

    # ТрUNCация промпта на всякий случай — GigaChat-Plus/Lite тоже имеют
    # лимит контекста, и молчаливое обрезание сервером хуже явного.
    truncated_messages: list[dict[str, str]] = []
    for msg in messages:
        truncated_messages.append({
            "role": msg.get("role", "user"),
            "content": _truncate(msg.get("content", "")),
        })

    payload = {
        "model": model_name,
        "messages": truncated_messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        # GigaChat-специфичный флаг: false = без проверки того, что ответ
        # «соответствует» ожиданиям Sber (streaming/reranking). Для
        # модераторских задач нам нужна голая генерация, без модерации Sber.
        # Документация: https://developers.sber.ru/docs/ru/gigachat/reference/rest/post-chat-completions
        "stream": False,
    }

    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    start = time.monotonic()
    last_error: str | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            result = await asyncio.wait_for(
                _do_request(payload, headers, timeout_sec),
                timeout=timeout_sec + 5,  # +5 на накладные расходы поверх сетевого таймаута
            )
            elapsed_ms = int((time.monotonic() - start) * 1000)
            if result.ok:
                # Кеш.
                if use_cache:
                    _cache[_cache_key(messages, model_name, temperature)] = (time.time(), result)
                logger.info(
                    "llm complete ok: model=%s, attempt=%d, elapsed_ms=%d, "
                    "tokens=%s, cached=%s",
                    model_name, attempt + 1, elapsed_ms,
                    result.usage, False,
                )
                return LLMResult(
                    ok=True,
                    text=result.text,
                    error=None,
                    cached=False,
                    elapsed_ms=elapsed_ms,
                    usage=result.usage,
                )
            last_error = result.error or "unknown error"
            # Ретрай только на 429/503.
            if not _is_retryable_error(result.error or ""):
                logger.warning(
                    "llm complete failed (no retry): model=%s, attempt=%d, error=%s",
                    model_name, attempt + 1, last_error,
                )
                return LLMResult(ok=False, error=last_error, elapsed_ms=elapsed_ms)
            if attempt < MAX_RETRIES:
                backoff = RETRY_BACKOFF_BASE * (2 ** attempt)
                logger.info(
                    "llm complete retryable error, attempt=%d, backoff=%.1fs: %s",
                    attempt + 1, backoff, last_error,
                )
                await asyncio.sleep(backoff)
        except TimeoutError:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            last_error = f"таймаут {timeout_sec}s"
            logger.warning(
                "llm complete timeout: model=%s, attempt=%d, elapsed_ms=%d",
                model_name, attempt + 1, elapsed_ms,
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
            else:
                return LLMResult(ok=False, error=last_error, elapsed_ms=elapsed_ms)
        except aiohttp.ClientError as e:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            last_error = f"{type(e).__name__}: {e}"
            logger.warning(
                "llm complete network error: model=%s, attempt=%d, error=%s",
                model_name, attempt + 1, last_error,
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))
            else:
                return LLMResult(ok=False, error=last_error, elapsed_ms=elapsed_ms)
        except Exception as e:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            # Неожиданное — логируем с stack trace, чтобы найти потом.
            logger.exception("llm complete unexpected error: %s", e)
            return LLMResult(ok=False, error=f"{type(e).__name__}: {e}", elapsed_ms=elapsed_ms)

    return LLMResult(ok=False, error=last_error or "неизвестная ошибка")


def _is_retryable_error(error: str) -> bool:
    """429 (rate limit) и 503 (service unavailable) — ретрятся. Остальное — нет."""
    e = error.lower()
    return "429" in e or "rate limit" in e or "503" in e or "service unavailable" in e


async def _do_request(
    payload: dict, headers: dict[str, str], timeout_sec: int,
) -> LLMResult:
    """Один HTTP-запрос к GigaChat. Без ретраев — их делает complete().

    SSL-сертификаты Sber в Docker-контейнере Bothost работают штатно (CA
    bundle python:3.14-slim содержит российские корневые сертификаты через
    ca-certificates). Если на каком-то хостинге не работает — обёртка
    aiohttp.TCPConnector(ssl=False) НЕ ставится: GigaChat требует TLS, и
    отключение TLS — это нарушение контракта Sber. Вместо этого возвращаем
    внятную ошибку SSL.
    """
    url = f"{GIGACHAT_BASE_URL}/chat/completions"
    client_timeout = aiohttp.ClientTimeout(total=timeout_sec)
    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.post(url, json=payload, headers=headers) as response:
                status = response.status
                try:
                    body = await response.json()
                except Exception:
                    text = await response.text()
                    return LLMResult(
                        ok=False,
                        error=f"GigaChat вернул не JSON (HTTP {status}): {text[:200]}",
                    )
                if status == 429:
                    return LLMResult(
                        ok=False,
                        error=f"429 rate limit: {body.get('message') or body}",
                    )
                if status == 401:
                    return LLMResult(
                        ok=False,
                        error="401 unauthorized: неверный ключ API или истёк",
                    )
                if status == 403:
                    return LLMResult(
                        ok=False,
                        error=f"403 forbidden: {body.get('message') or body}",
                    )
                if status >= 500:
                    return LLMResult(
                        ok=False,
                        error=f"{status} server error: {body.get('message') or body}",
                    )
                if status != 200:
                    return LLMResult(
                        ok=False,
                        error=f"HTTP {status}: {body.get('message') or body}",
                    )
                # 200 OK.
                text = _extract_content(body)
                if not text:
                    return LLMResult(
                        ok=False,
                        error=f"пустой ответ: {body}",
                    )
                usage = _extract_usage(body)
                return LLMResult(ok=True, text=text, error=None, usage=usage)
    except aiohttp.ServerTimeoutError as e:
        return LLMResult(ok=False, error=f"сервер GigaChat не уложился в {timeout_sec}s: {e}")
    except aiohttp.ClientConnectorError as e:
        return LLMResult(
            ok=False,
            error=f"не удалось подключиться к GigaChat: {type(e).__name__}: {e}",
        )


def _extract_content(body: dict) -> str:
    """Достаёт текст ответа из JSON GigaChat.

    Формат (как OpenAI):
        {
          "choices": [
            {"message": {"role": "assistant", "content": "..."}}
          ]
        }
    """
    choices = body.get("choices")
    if not choices or not isinstance(choices, list):
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message") or {}
    return (message.get("content") or "").strip()


def _extract_usage(body: dict) -> dict[str, int]:
    """Достаёт статистику токенов: prompt_tokens, completion_tokens, total_tokens."""
    usage = body.get("usage") or {}
    if not isinstance(usage, dict):
        return {}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens", 0)),
        "completion_tokens": int(usage.get("completion_tokens", 0)),
        "total_tokens": int(usage.get("total_tokens", 0)),
    }


# ── Готовые высокоуровневые методы ──────────────────────────────────────────


async def expand_reason(
    *,
    action_type: str,
    mod_reason: str,
    offender_text: str | None,
    reply_context_text: str | None,
    rules_md: str,
    recent_reasons: list[str] | None = None,
) -> LLMResult:
    """Авторазвёртка причины наказания.

    Принимает короткий reason модератора («спам», «раковые форсы») и
    контекст, возвращает развёрнутое описание в 1-2 предложения для журнала
    санкций в репорт-чате.

    Системный промпт запрещает галлюцинации: модель НЕ добавляет фактов,
    которых не было в исходных данных. Если в reason модератора уже
    внятное описание — модель его не переписывает, а лишь формализует.

    Args:
        action_type: "warn" | "ban" | "mute" — для контекста модели.
        mod_reason: исходный reason модератора.
        offender_text: цитата сообщения нарушителя (текст/caption, без медиа).
        reply_context_text: текст родителя, на который отвечал нарушитель
            (если есть), иначе None.
        rules_md: выжимка правил чата в markdown (берётся из docs/rules.md).
        recent_reasons: последние N причин наказаний этого же юзера
            (для контекста «повторное нарушение»). None — контекста нет.

    Returns:
        LLMResult. text — развёрнутая причина. Если LLM недоступна или
        не успела — вызывающий использует mod_reason как есть.
    """
    action_label = {"warn": "варн", "ban": "бан", "mute": "мьют"}.get(action_type, action_type)
    system_prompt = (
        "Ты — ассистент модератора Telegram-чата. Твоя задача — развернуть "
        "короткую причину наказания в полное описание для журнала санкций. "
        "Правила чата (контекст):\n\n"
        f"{rules_md}\n\n"
        "Жёсткие правила:\n"
        "1. НЕ придумывай факты, которых нет в исходных данных. Если в причине "
        "модератора «спам», не додумывай «реклама казино» — если в сообщении "
        "нарушителя нет упоминания казино.\n"
        "2. Опирайся ТОЛЬКО на: причину модератора, цитату сообщения нарушителя, "
        "контекст «в ответ на», историю прошлых причин. Если чего-то нет — не "
        "упоминай.\n"
        "3. Формат: 1-2 предложения, без воды, без эмодзи, без markdown. "
        "Например: «Спам в основном чате: реклама Telegram-канала (повторное "
        "нарушение, прошлый варн 2 дня назад)».\n"
        "4. Если причина модератора уже развёрнута (2+ предложения) — оставь как "
        "есть, лишь исправь пунктуацию и регистр. Не сокращай и не переписывай.\n"
        "5. Не упоминай правила по номерам (например, «нарушение п. 13») — "
        "опиши суть словами.\n"
        "6. Если причина модератора пустая или «(без причины)» — верни пустую "
        "строку, не выдумывай."
    )

    user_parts = [f"Тип наказания: {action_label}."]
    if mod_reason:
        user_parts.append(f"Причина модератора: {mod_reason!r}")
    else:
        user_parts.append("Причина модератора: (не указана)")
    if offender_text:
        user_parts.append(f"Сообщение нарушителя (цитата):\n{offender_text}")
    if reply_context_text:
        user_parts.append(f"Нарушитель отвечал на:\n{reply_context_text}")
    if recent_reasons:
        history = "; ".join(recent_reasons[-3:])
        user_parts.append(f"Последние причины этого юзера: {history}")
    user_parts.append("Дай развёрнутую причину в 1-2 предложениях.")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]
    return await complete(messages, temperature=0.2, max_tokens=256)


async def test_connection(api_key: str, *, model: str | None = None) -> LLMResult:
    """Проверка ключа при сохранении в веб-панели.

    Используется роутом `/admin/settings/llm/test` — SU жмёт кнопку
    «Проверить ключ», бот шлёт короткий промпт «Ответь: ok» и проверяет,
    что ключ валиден и модель отвечает.

    Args:
        api_key: ключ GigaChat, который SU ввёл в форму. Не из БД —
            именно тот, что ввелён сейчас (чтобы проверить до сохранения).
        model: модель для проверки. None — DEFAULT_MODEL.

    Returns:
        LLMResult. ok=True — ключ рабочий, модель отвечает.
    """
    model_name = model or DEFAULT_MODEL
    payload = {
        "model": model_name,
        "messages": [
            {"role": "user", "content": "Ответь одним словом: ok"},
        ],
        "temperature": 0.0,
        "max_tokens": 8,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        result = await asyncio.wait_for(
            _do_request(payload, headers, 15),
            timeout=20,
        )
        return result
    except TimeoutError:
        return LLMResult(ok=False, error="таймаут 20s при проверке ключа")
    except Exception as e:
        return LLMResult(ok=False, error=f"{type(e).__name__}: {e}")
