FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 1000 bot \
    && useradd --uid 1000 --gid bot --create-home bot \
    && mkdir /data \
    && chown bot:bot /data

COPY --chown=bot:bot main.py database.py exchange_handler.py telegram_bot.py docker_runner.py config.json ./

USER bot
CMD ["python", "docker_runner.py"]
