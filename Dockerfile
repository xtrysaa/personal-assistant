# Образ для деплоя на любом сервере с Docker (Oracle Cloud, Hetzner, домашний сервер и т.п.)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Almaty

WORKDIR /app

# Сначала только зависимости — так Docker переиспользует кэш слоёв
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# База данных должна переживать пересборку контейнера -> монтируй ./data как volume
VOLUME ["/app/data"]

CMD ["python", "run.py"]
