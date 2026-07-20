FROM python:3.12-slim

WORKDIR /app

# Создаём директорию для SQLite и даём права на запись
RUN mkdir -p /app/data && chmod 777 /app/data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE ${PORT}

CMD ["python", "bot.py"]
