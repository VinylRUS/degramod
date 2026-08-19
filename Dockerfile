FROM python:3.14.7-slim

# uv ставит зависимости из uv.lock — те же версии, что и локально.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Создаём директорию для SQLite и даём права на запись
RUN mkdir -p /app/data && chmod 777 /app/data

# Манифесты копируются отдельно от кода: слой с зависимостями пересобирается
# только когда меняется uv.lock, а не на каждую правку в bot_handlers.py.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev
# Smoke-проверка слоя зависимостей: если venv собрался неполным, сборка падает
# здесь с понятной ошибкой, а не в рантайме рестарт-лупом контейнера.
RUN uv run --no-sync python -c "import fastapi, aiogram, sqlalchemy, uvicorn, jinja2"

COPY . .

# Приложение читает PORT из env и слушает 0.0.0.0
# Bothost автоматически устанавливает PORT — не задавай его вручную!
EXPOSE 3000

# --frozen, а не --no-sync: если venv в образе оказался неполным (битый слой
# кеша, прерванная сборка), старт досинхронизирует недостающее по uv.lock и
# бот поднимется. С --no-sync такой контейнер уходил в рестарт-луп с
# ModuleNotFoundError. --frozen гарантирует, что uv.lock не будет изменён.
CMD ["uv", "run", "--frozen", "python", "bot.py"]
