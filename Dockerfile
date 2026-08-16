# v4.8.9: Python 3.14 bump (был 3.11-slim).
# Проверено локально: все зависимости (aiogram, fastapi, sqlalchemy, aiohttp,
# Pillow, rlottie-python, cryptography) ставятся и regression 46/46 проходит
# на Python 3.14.7. См. worklog v4.8.9 §C2.
FROM python:3.14-slim

WORKDIR /app

# Создаём директорию для SQLite и даём права на запись
RUN mkdir -p /app/data && chmod 777 /app/data

# v4.8.9: uv drop-in (вариант C) — pip ставит uv, uv ставит зависимости.
# uv в 10-100× быстрее pip на cold cache, образ собирается заметно быстрее.
# requirements.txt остаётся primary source of truth (см. 06_DO_NOT_TOUCH §16).
# --system = ставить в system site-packages (как pip), без venv.
COPY requirements.txt .
RUN pip install --no-cache-dir uv \
    && uv pip install --system --no-cache -r requirements.txt

COPY . .

# Приложение читает PORT из env и слушает 0.0.0.0
# Bothost автоматически устанавливает PORT — не задавай его вручную!
EXPOSE 3000

CMD ["python", "bot.py"]
