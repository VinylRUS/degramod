"""
github_client.py — v4.8.5: клиент GitHub REST + GraphQL API для `!idea`.

Инкапсулирует ВСЕ сетевые вызовы к GitHub. bot_handlers.py и web_app.py
работают ТОЛЬКО через этот модуль — никаких прямых HTTP-запросов к
api.github.com в других файлах.

Функции:
  • create_issue(pat, owner, repo, title) → IssueRef
      Создаёт Issue через REST API `POST /repos/{owner}/{repo}/issues`.
      В Issue ТОЛЬКО заголовок = текст идеи. Без description/body.
      Возвращает IssueRef с URL, номером и GraphQL node ID.

  • add_issue_to_project(pat, project_node_id, issue_node_id) → str
      Добавляет Issue в GitHub Project v2 (колонка по умолчанию —
      «Предложено»). GraphQL mutation `addProjectV2ItemById`.
      Возвращает node ID созданной карточки в Project.

  • get_issue_node_id(pat, owner, repo, issue_number) → str
      Получает GraphQL node ID Issue по номеру (нужен для add_to_project).
      REST не отдаёт node_id напрямую в нужном формате.

  • test_connection(pat, owner, repo, project_node_id=None) → TestResult
      Проверка что PAT валиден, права на repo есть, и (опционально)
      project_node_id существует. Используется веб-панелью при
      сохранении настроек. Создаёт + сразу закрывает тестовый Issue —
      проверка прав на запись (а не только чтение).

  • get_project_node_id(pat, owner_login, project_number) → str | None
      Резолвит node ID Project v2 по логину владельца + номеру.
      Удобно при начальной настройке: SU вводит owner + number,
      node_id резолвится автоматически.

Все функции — async, используют aiohttp (уже в зависимостях бота).
Таймаут — 15 секунд на запрос (GitHub обычно отвечает <2 сек).

GraphQL endpoint: https://api.github.com/graphql
REST endpoint:    https://api.github.com/repos/{owner}/{repo}/...

Авторизация: `Authorization: Bearer <PAT>` (токены classic и fine-grained
работают одинаково). `X-Github-Next-Global-ID: 1` — для получения
GraphQL Global ID в новом формате (надёжнее, не зависит от numeric ID).

Подробности:
  • REST Issues API: https://docs.github.com/en/rest/issues/issues#create-an-issue
  • GraphQL addProjectV2ItemById: https://docs.github.com/en/graphql/reference/mutations#addprojectv2itembyid
  • Projects v2: https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project-using-actions
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import aiohttp

logger = logging.getLogger("shadow_logger")

# ── Эндпоинты ────────────────────────────────────────────────────────────────
_REST_BASE = "https://api.github.com"
_GRAPHQL_URL = "https://api.github.com/graphql"

# Таймаут на один запрос к GitHub.
_TIMEOUT = aiohttp.ClientTimeout(total=15)


# ── Датаклассы результата ────────────────────────────────────────────────────
@dataclass
class IssueRef:
    """Результат создания Issue."""
    number: int                # номер Issue в репо (например, 42)
    url: str                   # https://github.com/owner/repo/issues/42
    node_id: str               # GraphQL node ID (нужен для add_to_project)
    title: str                 # заголовок = текст идеи


@dataclass
class TestResult:
    """Результат test_connection."""
    ok: bool                   # True если PAT валиден + права на repo есть
    message: str               # человекочитаемый статус
    details: dict[str, Any] = field(default_factory=dict)
    # details может содержать: pat_scopes, repo_full_name, project_title, etc.


class GithubApiError(Exception):
    """Базовая ошибка для всех проблем с GitHub API."""
    def __init__(self, message: str, *, status: int | None = None,
                 body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


# ── Внутренние хелперы ───────────────────────────────────────────────────────
def _headers(pat: str) -> dict[str, str]:
    """HTTP-заголовки для запросов к GitHub."""
    return {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-Github-Next-Global-ID": "1",
        "User-Agent": "ded-vobzhak-bot/4.8.5",
    }


async def _graphql(pat: str, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    """Выполняет GraphQL-запрос к GitHub. Возвращает data либо кидает GithubApiError.

    GitHub GraphQL endpoint возвращает 200 даже при ошибках GraphQL —
    реальная ошибка лежит в `response["errors"]`. Парсим и кидаем.
    """
    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(
                _GRAPHQL_URL, headers=_headers(pat), json=payload,
            ) as resp:
                status = resp.status
                body = await resp.text()
                if status != 200:
                    raise GithubApiError(
                        f"GraphQL request failed (HTTP {status})",
                        status=status, body=body,
                    )
                import json
                data = json.loads(body)
                if "errors" in data and data["errors"]:
                    msgs = "; ".join(e.get("message", "?") for e in data["errors"])
                    raise GithubApiError(f"GraphQL errors: {msgs}", status=200, body=body)
                return data.get("data", {}) or {}
    except aiohttp.ClientError as e:
        raise GithubApiError(f"Network error: {e}") from e
    except TimeoutError as e:
        raise GithubApiError("Request timed out (15s)") from e


# ── Публичные функции ────────────────────────────────────────────────────────
async def create_issue(pat: str, owner: str, repo: str, title: str) -> IssueRef:
    """Создаёт Issue в репозитории. Заголовок = title, без body.

    Args:
        pat: GitHub Personal Access Token.
        owner: владелец репо (например, 'degradach').
        repo: имя репо (например, 'ded-vobzhak-ideas').
        title: заголовок Issue = текст идеи (уже обрезан до 200 символов).

    Returns:
        IssueRef с number, url, node_id, title.

    Raises:
        GithubApiError: при сетевой ошибке, 401/403/404/422, или если
            GitHub вернул невалидный JSON.
    """
    url = f"{_REST_BASE}/repos/{owner}/{repo}/issues"
    payload = {"title": title, "body": None}

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(
                url, headers=_headers(pat), json=payload,
            ) as resp:
                status = resp.status
                body = await resp.text()
                if status not in (200, 201):
                    # Парсим сообщение об ошибке из GitHub.
                    msg = f"GitHub create_issue failed (HTTP {status})"
                    try:
                        import json
                        err = json.loads(body)
                        if "message" in err:
                            msg = f"{msg}: {err['message']}"
                    except (ValueError, KeyError):
                        pass
                    raise GithubApiError(msg, status=status, body=body)

                import json
                data = json.loads(body)
                return IssueRef(
                    number=data["number"],
                    url=data["html_url"],
                    node_id=data["node_id"],
                    title=data["title"],
                )
    except aiohttp.ClientError as e:
        raise GithubApiError(f"Network error: {e}") from e
    except TimeoutError as e:
        raise GithubApiError("Request timed out (15s)") from e


async def get_issue_node_id(pat: str, owner: str, repo: str, issue_number: int) -> str:
    """Получает GraphQL node ID Issue по номеру.

    Нужен если Issue уже создан и его надо добавить в Project.
    В новом формате node ID начинается с 'I_' (раньше был 'MDU6SXNzdWU...').

    Args:
        pat: GitHub PAT.
        owner, repo: реквизиты репо.
        issue_number: номер Issue.

    Returns:
        GraphQL node ID (str).

    Raises:
        GithubApiError: если Issue не найден или запрос упал.
    """
    query = """
    query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        issue(number: $number) {
          id
        }
      }
    }
    """
    data = await _graphql(pat, query, {
        "owner": owner, "repo": repo, "number": issue_number,
    })
    issue = data.get("repository", {}).get("issue")
    if not issue or "id" not in issue:
        raise GithubApiError(
            f"Issue #{issue_number} not found in {owner}/{repo}",
            status=200,
        )
    return issue["id"]


async def add_issue_to_project(
    pat: str, project_node_id: str, issue_node_id: str,
) -> str:
    """Добавляет Issue в GitHub Project v2.

    Issue попадает в колонку по умолчанию (status: "Предложено" если
    Project настроен корректно — это настраивается в самом Project на
    GitHub, не через API).

    Args:
        pat: GitHub PAT.
        project_node_id: GraphQL node ID Project v2 (например, 'PVT_xxx').
        issue_node_id: GraphQL node ID Issue (например, 'I_xxx' или 'MDU6...').

    Returns:
        Node ID созданной карточки в Project (PVTI_xxx).

    Raises:
        GithubApiError: при невалидном project_node_id, отсутствии прав
            на Project, или сетевой ошибке.
    """
    mutation = """
    mutation($projectId: ID!, $contentId: ID!) {
      addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
        item {
          id
        }
      }
    }
    """
    data = await _graphql(pat, mutation, {
        "projectId": project_node_id, "contentId": issue_node_id,
    })
    item = data.get("addProjectV2ItemById", {}).get("item")
    if not item or "id" not in item:
        raise GithubApiError(
            "addProjectV2ItemById returned no item — likely invalid project_node_id "
            "or missing project permissions",
            status=200,
        )
    return item["id"]


async def get_project_node_id(
    pat: str, owner_login: str, project_number: int,
) -> str:
    """Резолвит node ID Project v2 по owner login + project number.

    Используется веб-панелью для авто-получения node_id при начальной
    настройке (SU вводит логин владельца и номер Project — node_id
    подтягивается автоматически, не нужно руками копировать из GraphQL
    explorer).

    Args:
        pat: GitHub PAT.
        owner_login: логин владельца Project (user или organization).
        project_number: номер Project (из URL: github.com/users/X/projects/N).

    Returns:
        GraphQL node ID Project v2 (PVT_xxx).

    Raises:
        GithubApiError: если Project не найден или нет прав.
    """
    query = """
    query($login: String!, $number: Int!) {
      user(login: $login) {
        projectV2(number: $number) { id title }
      }
      organization(login: $login) {
        projectV2(number: $number) { id title }
      }
    }
    """
    data = await _graphql(pat, query, {
        "login": owner_login, "number": project_number,
    })
    # Project может принадлежать либо user, либо organization —
    # проверяем оба.
    user_proj = (data.get("user") or {}).get("projectV2")
    org_proj = (data.get("organization") or {}).get("projectV2")
    proj = user_proj or org_proj
    if not proj or "id" not in proj:
        raise GithubApiError(
            f"Project #{project_number} not found for owner '{owner_login}'. "
            "Check that the owner login is correct, the project number matches, "
            "and the PAT has 'project' scope.",
            status=200,
        )
    return proj["id"]


async def test_connection(
    pat: str, owner: str, repo: str,
    project_node_id: str | None = None,
) -> TestResult:
    """Проверка подключения: PAT валиден, права на repo есть, project существует.

    Что проверяет:
      1. PAT валиден (GET /user).
      2. Repo доступен (GET /repos/{owner}/{repo}).
      3. Права на запись в repo — создаём + закрываем тестовый Issue.
      4. (Опционально) Project v2 существует и PAT имеет к нему доступ.

    Args:
        pat: GitHub PAT.
        owner, repo: реквизиты репо.
        project_node_id: если задан — проверяем что Project существует.

    Returns:
        TestResult с ok=True/False, message, details.
    """
    details: dict[str, Any] = {}

    # 1. PAT валиден?
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(
                f"{_REST_BASE}/user", headers=_headers(pat),
            ) as resp:
                if resp.status == 401:
                    return TestResult(False, "PAT невалиден (401 Unauthorized).", details)
                if resp.status == 403:
                    body = await resp.text()
                    return TestResult(
                        False,
                        "PAT валиден, но GitHub API rate limit или нет прав (403).",
                        {**details, "body": body[:300]},
                    )
                if resp.status != 200:
                    body = await resp.text()
                    return TestResult(
                        False,
                        f"GitHub API вернул HTTP {resp.status} при проверке PAT.",
                        {**details, "body": body[:300]},
                    )
                user_data = await resp.json()
                details["pat_user"] = user_data.get("login")
                # scopes лежат в X-OAuth-Scopes только для classic PAT.
                scopes_header = resp.headers.get("X-OAuth-Scopes", "")
                details["pat_scopes"] = scopes_header or "(fine-grained)"
    except aiohttp.ClientError as e:
        return TestResult(False, f"Сетевая ошибка при проверке PAT: {e}", details)
    except TimeoutError:
        return TestResult(False, "Таймаут при проверке PAT (15 сек).", details)

    # 2. Repo доступен?
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(
                f"{_REST_BASE}/repos/{owner}/{repo}", headers=_headers(pat),
            ) as resp:
                if resp.status == 404:
                    return TestResult(
                        False,
                        f"Репозиторий {owner}/{repo} не найден (404) или PAT не имеет доступа.",
                        details,
                    )
                if resp.status == 403:
                    return TestResult(
                        False,
                        f"Доступ к {owner}/{repo} запрещён (403). "
                        "Возможно, PAT не имеет scope 'repo'.",
                        details,
                    )
                if resp.status != 200:
                    body = await resp.text()
                    return TestResult(
                        False,
                        f"GitHub API вернул HTTP {resp.status} при проверке репо.",
                        {**details, "body": body[:300]},
                    )
                repo_data = await resp.json()
                details["repo_full_name"] = repo_data.get("full_name")
                details["repo_private"] = repo_data.get("private")
                # Права текущего пользователя на этот repo.
                perms = repo_data.get("permissions") or {}
                details["repo_can_push"] = perms.get("push", False)
                if not perms.get("push", False):
                    return TestResult(
                        False,
                        f"PAT не имеет прав на запись в {owner}/{repo}. "
                        "Выдайте PAT scope 'repo' (для classic) или доступ к репо "
                        "(для fine-grained).",
                        details,
                    )
    except aiohttp.ClientError as e:
        return TestResult(False, f"Сетевая ошибка при проверке репо: {e}", details)
    except TimeoutError:
        return TestResult(False, "Таймаут при проверке репо (15 сек).", details)

    # 3. Права на запись: создаём + закрываем тестовый Issue.
    test_title = "[bot test] ded-vobzhak connection check — можно удалить"
    try:
        issue_ref = await create_issue(pat, owner, repo, test_title)
        details["test_issue_number"] = issue_ref.number
        details["test_issue_url"] = issue_ref.url
        # Закрываем тестовый Issue.
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.patch(
                f"{_REST_BASE}/repos/{owner}/{repo}/issues/{issue_ref.number}",
                headers=_headers(pat),
                json={"state": "closed"},
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "test_connection: failed to close test issue #%s (HTTP %s)",
                        issue_ref.number, resp.status,
                    )
                    # Не блокируем — Issue создался, значит права на запись есть.
    except GithubApiError as e:
        return TestResult(
            False,
            f"Не удалось создать тестовый Issue в {owner}/{repo}: {e}",
            details,
        )

    # 4. Project существует (если project_node_id задан).
    if project_node_id:
        try:
            query = """
            query($id: ID!) {
              node(id: $id) {
                ... on ProjectV2 { id title }
              }
            }
            """
            data = await _graphql(pat, query, {"id": project_node_id})
            node = data.get("node")
            if not node or "id" not in node:
                return TestResult(
                    False,
                    f"Project с node_id '{project_node_id}' не найден или "
                    "PAT не имеет к нему доступа. Проверьте scope 'project'.",
                    details,
                )
            details["project_title"] = node.get("title")
        except GithubApiError as e:
            return TestResult(
                False,
                f"Не удалось проверить Project: {e}",
                details,
            )

    return TestResult(
        True,
        "Подключение успешно. PAT валиден, права на репо есть"
        + (", Project доступен." if project_node_id else "."),
        details,
    )
