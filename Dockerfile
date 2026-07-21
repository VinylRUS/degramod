FROM python:3.12-slim

# Bothost: каталог /app может быть bind-mount'ом с хоста при runtime.
# Поэтому WORKDIR выносим за пределы /app, а данные БД храним в /app/data.
WORKDIR /usr/src/app

# Создаём директорию для SQLite и даём права на запись
RUN mkdir -p /app/data && chmod 777 /app/data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Порт — только информационно; реальный порт задаётся через PORT env var
EXPOSE 3000

CMD ["python", "bot.py"]
