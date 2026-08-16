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

COPY . .

# Приложение читает PORT из env и слушает 0.0.0.0
# Bothost автоматически устанавливает PORT — не задавай его вручную!
EXPOSE 3000

CMD ["uv", "run", "--no-sync", "python", "bot.py"]
