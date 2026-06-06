FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

RUN mkdir -p data logs sessions

# По умолчанию запускаем sender в auto-режиме.
# Для review/fetch/score/generate переопредели command в docker compose.
CMD ["python", "-m", "src.sender.send_queue", "--mode", "auto"]
