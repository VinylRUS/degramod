FROM python:3.11-slim

WORKDIR /app

# Создаём директорию для SQLite и даём права на запись
RUN mkdir -p /app/data && chmod 777 /app/data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Приложение читает PORT из env и слушает 0.0.0.0
# Bothost автоматически устанавливает PORT — не задавай его вручную!
EXPOSE 3000

CMD ["python", "bot.py"]
